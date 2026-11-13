import { statSync } from "node:fs";
import path from "node:path";

import {
  CliRunner,
  looksLikeMissingSubcommand,
  redactForDisplay
} from "./CliDiscovery";
import { ExecutableResolverOptions, resolveExecutablePath } from "./ExecutableResolver";
import { BridgeHealthError, parseHealthJson } from "./AlysisProtocol";

export type CliCandidateSource = "setting" | "path" | "virtualenv" | "python-scripts" | "common-dir";

export interface CliCandidate {
  cliPath: string;
  source: CliCandidateSource;
  label: string;
}

export interface DiscoverCliOptions {
  env?: NodeJS.ProcessEnv;
  platform?: NodeJS.Platform;
  homeDir?: string;
  /** Current value of the alysis.cliPath setting (may be empty). */
  cliPathSetting?: string;
  /** Extra directories to probe — e.g. the active Python interpreter's bin/Scripts dir. */
  extraDirs?: readonly string[];
  /** Injectable existence probe (defaults to a real fs stat). */
  fileExists?: (candidate: string) => boolean;
  /** Forwarded to resolveExecutablePath for the setting + PATH probes (injectable in tests). */
  executableResolver?: ExecutableResolverOptions;
}

/**
 * Best-effort discovery of `alysis` executables: the configured setting, PATH, the active
 * virtualenv, caller-supplied Python script dirs, and common install locations. Pure given its
 * injected env/fs probes, so it is unit-testable without touching the real filesystem. Candidates
 * are de-duplicated (case-insensitively on Windows) and returned in priority order.
 */
export function discoverCliCandidates(options: DiscoverCliOptions = {}): CliCandidate[] {
  const platform = options.platform ?? process.platform;
  const env = options.env ?? process.env;
  const home = options.homeDir ?? env.HOME ?? env.USERPROFILE ?? "";
  const pathApi = platform === "win32" ? path.win32 : path.posix;
  const exeName = platform === "win32" ? "alysis.exe" : "alysis";
  const binSubdir = platform === "win32" ? "Scripts" : "bin";
  const exists = options.fileExists ?? defaultFileExists;

  const candidates: CliCandidate[] = [];
  const seen = new Set<string>();
  const add = (cliPath: string, source: CliCandidateSource, label: string): void => {
    if (!cliPath) {
      return;
    }
    const key = platform === "win32" ? cliPath.toLowerCase() : cliPath;
    if (seen.has(key)) {
      return;
    }
    seen.add(key);
    candidates.push({ cliPath, source, label });
  };

  // 1. The configured setting, if it resolves to a real executable.
  const setting = (options.cliPathSetting ?? "").trim();
  if (setting.length > 0) {
    const resolved = resolveExecutablePath(setting, options.executableResolver);
    if (resolved.resolvedPath) {
      add(resolved.resolvedPath, "setting", "Configured alysis.cliPath");
    }
  }

  // 2. Resolve the canonical CLI on PATH.
  const onPath = resolveExecutablePath("alysis", options.executableResolver);
  if (onPath.resolvedPath) {
    add(onPath.resolvedPath, "path", "Found on PATH");
  }

  // 3. The active virtualenv's bin/Scripts dir.
  const virtualEnv = (env.VIRTUAL_ENV ?? "").trim();
  if (virtualEnv.length > 0) {
    probeDir(pathApi.join(virtualEnv, binSubdir), "virtualenv", "Active virtualenv");
  }

  // 4. Caller-supplied Python script dirs (e.g. the active interpreter's dir).
  for (const dir of options.extraDirs ?? []) {
    probeDir(dir, "python-scripts", "Python interpreter directory");
  }

  // 5. Common install locations.
  for (const dir of commonInstallDirs(platform, home, env)) {
    probeDir(dir, "common-dir", shortLabel(dir, home, platform));
  }

  return candidates;

  function probeDir(dir: string, source: CliCandidateSource, label: string): void {
    if (!dir) {
      return;
    }
    const candidate = pathApi.join(dir, exeName);
    if (exists(candidate)) {
      add(candidate, source, label);
      return;
    }
  }
}

export interface CliValidationResult {
  ok: boolean;
  cliPath: string;
  version?: string;
  protocolVersion?: string;
  alysisVersion?: string;
  code?: string;
  message?: string;
}

/**
 * Validate a chosen `alysis` executable by running `--version` then `ide-bridge health` through
 * the injected runner. Never throws and never persists — the caller saves the path only when
 * `ok === true`. All error text is redacted before it is surfaced.
 */
export async function validateCliExecutable(
  runner: CliRunner,
  cliPath: string,
  env?: NodeJS.ProcessEnv
): Promise<CliValidationResult> {
  try {
    const version = await runner(cliPath, ["--version"], { env });
    if (version.exitCode !== 0) {
      return {
        ok: false,
        cliPath,
        code: "cli_nonzero",
        message: redactForDisplay(`"${cliPath}" exited with code ${version.exitCode} while checking --version.`)
      };
    }

    const health = await runner(cliPath, ["ide-bridge", "health"], { env });
    if (health.exitCode !== 0) {
      const missing = looksLikeMissingSubcommand(`${health.stdout}\n${health.stderr}`, "ide-bridge", health.exitCode);
      return {
        ok: false,
        cliPath,
        code: missing ? "ide_bridge_missing" : "health_nonzero",
        message: missing
          ? "This binary runs but does not support `alysis ide-bridge health`. It may be too old for this extension."
          : redactForDisplay(`Alysis Code bridge health exited with code ${health.exitCode}: ${health.stderr || health.stdout}`)
      };
    }

    const parsed = parseHealthJson(health.stdout);
    return {
      ok: true,
      cliPath,
      version: (version.stdout || version.stderr).trim(),
      protocolVersion: parsed.protocol_version,
      alysisVersion: parsed.alysis_version
    };
  } catch (error) {
    // An exit-0 health probe with a malformed/unhealthy payload throws a BridgeHealthError with a
    // specific code (unsupported_protocol_version, incompatible_bridge, invalid_json, …) — carry it
    // through so "CLI too old / unsupported protocol" reads distinctly from a generic spawn failure.
    if (error instanceof BridgeHealthError) {
      return { ok: false, cliPath, code: error.code, message: redactForDisplay(error.message) };
    }
    const code = isNodeErrorWithCode(error, "ENOENT") ? "cli_missing" : "cli_error";
    const message = error instanceof Error ? error.message : String(error);
    return { ok: false, cliPath, code, message: redactForDisplay(message) };
  }
}

/**
 * Derive the bin/Scripts directory that shares a venv with the configured Python interpreter. Pure
 * + testable; guards empty / bare-name / non-absolute interpreter values.
 */
export function pythonScriptDirsFromInterpreter(
  interpreterPath: string,
  platform: NodeJS.Platform = process.platform
): string[] {
  const value = (interpreterPath ?? "").trim();
  if (value.length === 0 || value === "python" || value === "python3") {
    return [];
  }
  const pathApi = platform === "win32" ? path.win32 : path.posix;
  if (!pathApi.isAbsolute(value)) {
    return [];
  }
  return [pathApi.dirname(value)];
}

function commonInstallDirs(platform: NodeJS.Platform, home: string, env: NodeJS.ProcessEnv): string[] {
  const pathApi = platform === "win32" ? path.win32 : path.posix;
  if (platform === "win32") {
    const appData = env.APPDATA ?? (home ? pathApi.join(home, "AppData", "Roaming") : "");
    const localAppData = env.LOCALAPPDATA ?? (home ? pathApi.join(home, "AppData", "Local") : "");
    return [
      home ? pathApi.join(home, ".local", "bin") : "",
      appData ? pathApi.join(appData, "Python", "Scripts") : "",
      localAppData ? pathApi.join(localAppData, "Programs", "Python", "Scripts") : "",
      home ? pathApi.join(home, "pipx", "venvs", "alysis-code", "Scripts") : ""
    ].filter((dir) => dir.length > 0);
  }
  return [
    home ? pathApi.join(home, ".local", "bin") : "",
    "/usr/local/bin",
    "/opt/homebrew/bin",
    home ? pathApi.join(home, "bin") : "",
    home ? pathApi.join(home, ".local", "pipx", "venvs", "alysis-code", "bin") : ""
  ].filter((dir) => dir.length > 0);
}

function shortLabel(dir: string, home: string, platform: NodeJS.Platform): string {
  const sep = platform === "win32" ? "\\" : "/";
  if (home && (dir === home || dir.startsWith(home + sep))) {
    return "~" + dir.slice(home.length);
  }
  return dir;
}

function defaultFileExists(candidate: string): boolean {
  try {
    return statSync(candidate).isFile();
  } catch {
    return false;
  }
}

function isNodeErrorWithCode(error: unknown, code: string): boolean {
  return error instanceof Error && "code" in error && (error as NodeJS.ErrnoException).code === code;
}
