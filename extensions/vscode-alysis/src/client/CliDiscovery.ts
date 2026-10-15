import { execFile } from "node:child_process";
import { promisify } from "node:util";

import { BridgeHealth, BridgeHealthError, AlysisMode, AlysisTransport, parseHealthJson } from "./AlysisProtocol";
import {
  HostEnvironmentNotice,
  HostNetworkSettings,
  ResolvedHostNetworkSettings,
  resolveHostNetworkSettings
} from "./hostEnvironment";
import {
  ExecutableResolverOptions,
  isAbsolutePathLike,
  isPathInsideAnyWorkspace,
  isRelativePathLike,
  resolveExecutablePath,
  unsupportedShimReason
} from "./ExecutableResolver";

const execFileAsync = promisify(execFile);

const COMMAND_TIMEOUT_MS = 15000;
const MAX_STDIO_BYTES = 1024 * 1024;
const PROBE_CREDENTIAL_ENV_KEY =
  /(?:^|_)(?:api_?key|access_?key|secret|token|password|passwd|credentials?|authorization)(?:_|$)/i;

export const SENSITIVE_ALYSIS_SETTINGS = [
  "cliPath",
  "defaultMode",
  "defaultModel",
  "baseUrl",
  "provider",
  "transport",
  "sandboxProfile",
  "forgeExecuteMaxSteps",
  "forgeExecuteNoLog",
  "autoStartBridge",
  "enableForge",
  "extraCaCerts"
] as const;

type SensitiveSetting = (typeof SENSITIVE_ALYSIS_SETTINGS)[number];

export type ConfigValueSource =
  | "default"
  | "global"
  | "globalLanguage"
  | "workspace"
  | "workspaceFolder"
  | "workspaceLanguage"
  | "workspaceFolderLanguage"
  | "unknown";

export interface ConfigInspect<T> {
  defaultValue?: T;
  globalValue?: T;
  workspaceValue?: T;
  workspaceFolderValue?: T;
  defaultLanguageValue?: T;
  globalLanguageValue?: T;
  workspaceLanguageValue?: T;
  workspaceFolderLanguageValue?: T;
}

export interface AlysisConfig {
  cliPath: string;
  defaultMode: AlysisMode;
  defaultModel: string;
  baseUrl: string;
  provider: string;
  transport: AlysisTransport;
  sandboxProfile: "default" | "strict" | "warn" | "off";
  forgeExecuteMaxSteps?: number;
  forgeExecuteNoLog: boolean;
  showStatusBar: boolean;
  autoStartBridge: boolean;
  enableForge: boolean;
  /** Validated `alysis.extraCaCerts`; empty when unset or rejected. */
  extraCaCerts?: string;
  /**
   * Validated VS Code `http.*` proxy settings plus `extraCaCerts`, translated into the CLI's launch
   * environment by the bridge client. Supplied by the extension host; absent in the standalone parser.
   */
  hostNetwork?: ResolvedHostNetworkSettings;
  security?: AlysisConfigSecurity;
  /**
   * Runtime provenance is attached by the extension's production startup coordinator after VS Code
   * settings have been parsed. The standalone config parser intentionally remains environment-free.
   */
  runtimeSelection?: AlysisRuntimeSelection;
}

export interface AlysisRuntimeSelection {
  origin: "managed" | "development-override" | "development-path" | "unavailable";
  production: boolean;
  validating?: boolean;
  executablePath?: string;
  artifactVersion?: string;
  cliVersion?: string;
  protocolVersion?: string;
  message: string;
}

export interface AlysisConfigSecurity {
  isWorkspaceTrusted: boolean;
  ignoredWorkspaceSettings: SafeConfigNotice[];
  cliPath: ResolvedCliPath;
  workspaceRoots: readonly string[];
}

export interface ResolvedCliPath {
  value: string;
  resolvedExecutablePath?: string | null;
  source: ConfigValueSource;
  trusted: boolean;
  executionAllowed: boolean;
  apiKeyForwardingAllowed: boolean;
  reason?: string;
}

export interface ConfigReader {
  get<T>(section: string, defaultValue: T): T;
  inspect?<T>(section: string): ConfigInspect<T> | undefined;
}

export interface SafeConfigNotice {
  code: "workspace_setting_ignored";
  setting: SensitiveSetting;
  message: string;
}

export interface AlysisConfigOptions {
  isWorkspaceTrusted?: boolean;
  workspaceRoots?: readonly string[];
  executableResolver?: ExecutableResolverOptions;
  onNotice?: (notice: SafeConfigNotice) => void;
  /** Raw VS Code `http.*` settings; `extraCaCerts` is read from the `alysis` section here. */
  hostNetwork?: Omit<HostNetworkSettings, "extraCaCerts">;
  onHostNetworkNotice?: (notice: HostEnvironmentNotice) => void;
  /** Injected for tests; defaults to a regular-file check. */
  fileExists?: (filePath: string) => boolean;
}

export interface CliCommandResult {
  stdout: string;
  stderr: string;
  exitCode: number;
}

export interface CliRunnerOptions {
  env?: NodeJS.ProcessEnv;
}

export type CliRunner = (
  command: string,
  args: readonly string[],
  options?: CliRunnerOptions
) => Promise<CliCommandResult>;

export interface CliDetectionSuccess {
  ok: true;
  cliPath: string;
  versionOutput: string;
  health: BridgeHealth;
}

export interface CliDetectionFailure {
  ok: false;
  cliPath: string;
  code: string;
  message: string;
  details?: Record<string, unknown>;
}

export type CliDetectionResult = CliDetectionSuccess | CliDetectionFailure;

export function getAlysisConfig(
  config: ConfigReader,
  options: AlysisConfigOptions = {}
): AlysisConfig {
  const isWorkspaceTrusted = options.isWorkspaceTrusted ?? true;
  const notices: SafeConfigNotice[] = [];
  const read = <T>(key: SensitiveSetting, defaultValue: T): SettingRead<T> =>
    readSensitiveSetting(config, key, defaultValue, isWorkspaceTrusted, (notice) => {
      notices.push(notice);
      options.onNotice?.(notice);
    });

  const cliPathRead = read("cliPath", "");
  const defaultMode = normalizeMode(read("defaultMode", "review").value);
  const transport = read("transport", "stdio").value === "stdio" ? "stdio" : "stdio";
  const sandboxProfile = normalizeSandboxProfile(read("sandboxProfile", "default").value);
  const forgeExecuteMaxSteps = normalizePositiveIntegerOverride(read("forgeExecuteMaxSteps", 0).value);
  const provider = String(read("provider", "").value ?? "").trim();
  const parsedConfig: AlysisConfig = {
    cliPath: String(cliPathRead.value ?? "").trim(),
    defaultMode,
    defaultModel: String(read("defaultModel", "").value ?? "").trim(),
    baseUrl: normalizeBaseUrl(read("baseUrl", "").value),
    provider,
    transport,
    sandboxProfile,
    forgeExecuteMaxSteps,
    forgeExecuteNoLog: normalizeBoolean(read("forgeExecuteNoLog", false).value, false),
    showStatusBar: normalizeBoolean(config.get("showStatusBar", true), true),
    autoStartBridge: normalizeBoolean(read("autoStartBridge", false).value, false),
    enableForge: normalizeBoolean(read("enableForge", true).value, true)
  };
  // extraCaCerts is read unconditionally so the untrusted-workspace guard applies to it like every
  // other sensitive setting; the http.* half is whatever the host supplied (nothing, standalone).
  const resolvedNetwork = resolveHostNetworkSettings(
    { ...(options.hostNetwork ?? {}), extraCaCerts: String(read("extraCaCerts", "").value ?? "") },
    { fileExists: options.fileExists }
  );
  parsedConfig.hostNetwork = resolvedNetwork.settings;
  parsedConfig.extraCaCerts = resolvedNetwork.settings.extraCaCerts;
  for (const notice of resolvedNetwork.notices) {
    options.onHostNetworkNotice?.(notice);
  }
  parsedConfig.security = {
    isWorkspaceTrusted,
    ignoredWorkspaceSettings: notices,
    workspaceRoots: options.workspaceRoots ?? [],
    cliPath: resolveCliPathSecurity(parsedConfig.cliPath, cliPathRead, {
      isWorkspaceTrusted,
      workspaceRoots: options.workspaceRoots ?? [],
      executableResolver: options.executableResolver
    })
  };
  return parsedConfig;
}

export function resolveCliPath(config: AlysisConfig): string {
  return config.security?.cliPath.value ?? (config.cliPath.length > 0 ? config.cliPath : "alysis");
}

export function resolveCliExecutionPath(config: AlysisConfig): string {
  return config.security?.cliPath.resolvedExecutablePath || resolveCliPath(config);
}

export interface CliExecutionGuard {
  allowed: boolean;
  cliPath: string;
  reason?: string;
}

export function evaluateCliExecution(config: AlysisConfig): CliExecutionGuard {
  const cliPath = resolveCliPath(config);
  const security = config.security?.cliPath;
  if (security && !security.executionAllowed) {
    return {
      allowed: false,
      cliPath,
      reason: security.reason ?? "Alysis Code CLI execution is blocked by Workspace Trust."
    };
  }
  return { allowed: true, cliPath };
}

export function canForwardApiKey(config: AlysisConfig): boolean {
  const security = config.security?.cliPath;
  return security ? security.apiKeyForwardingAllowed : true;
}

export function cliCommandEnvironment(config: AlysisConfig): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = { ...process.env };
  if (config.sandboxProfile !== "default") {
    for (const section of ["SHELL", "VERIFY"]) {
      const key = `ALYSIS_${section}_SANDBOX_MODE`;
      const legacyKey = `SYLLIPTOR_${section}_SANDBOX_MODE`;
      if (env[key] === undefined && env[legacyKey] === undefined) env[key] = config.sandboxProfile;
    }
  }
  // Match the bridge: force UTF-8 stdio so locale code pages (e.g. Windows Greek cp1253) can't
  // crash the Python CLI on Unicode output during health/version/doctor probes.
  env.PYTHONUTF8 = "1";
  env.PYTHONIOENCODING = "utf-8";
  // Version, health, executable-validation, and sandbox-doctor probes never need provider
  // credentials. Do not expose an inherited API key to a binary merely because it is being
  // inspected. The authenticated long-lived bridge has its own trust-gated environment builder.
  deleteCredentialEnvironmentVariables(env);
  return env;
}

/** Remove credential-bearing variables in-place before spawning a process that must not receive secrets. */
export function deleteCredentialEnvironmentVariables(env: NodeJS.ProcessEnv): void {
  for (const key of Object.keys(env)) {
    if (PROBE_CREDENTIAL_ENV_KEY.test(key)) {
      delete env[key];
    }
  }
}

export class CliDiscovery {
  public constructor(private readonly runner: CliRunner = defaultCliRunner) {}

  public async detect(config: AlysisConfig): Promise<CliDetectionResult> {
    const guard = evaluateCliExecution(config);
    if (!guard.allowed) {
      return {
        ok: false,
        cliPath: guard.cliPath,
        code: "cli_untrusted",
        message: guard.reason ?? "Alysis Code CLI execution is blocked by Workspace Trust."
      };
    }
    const cliPath = resolveCliExecutionPath(config);
    const shimReason = unsupportedShimReason(cliPath);
    if (shimReason) {
      // Launching a .cmd/.bat wrapper fails with a bare "spawn EINVAL" (the CVE-2024-27980 fix), so
      // classify it as "cannot launch this CLI" — the same Locate-CLI recovery as a missing binary —
      // and name the wrapper plus the setting that fixes it.
      return { ok: false, cliPath, code: "cli_error", message: shimReason };
    }
    try {
      const runnerOptions = { env: cliCommandEnvironment(config) };
      const version = await this.runner(cliPath, ["--version"], runnerOptions);
      if (version.exitCode !== 0) {
        return {
          ok: false,
          cliPath,
          code: "cli_nonzero",
          message: `Alysis Code CLI exited with code ${version.exitCode} while checking --version.`
        };
      }

      const health = await this.health(cliPath, runnerOptions.env);
      return {
        ok: true,
        cliPath,
        versionOutput: redactForDisplay(version.stdout.trim() || version.stderr.trim()),
        health
      };
    } catch (error) {
      return this.detectionFailure(cliPath, error);
    }
  }

  public async health(cliPath: string, env?: NodeJS.ProcessEnv): Promise<BridgeHealth> {
    const result = await this.runner(cliPath, ["ide-bridge", "health"], { env });
    if (result.exitCode !== 0) {
      const stderr = redactForDisplay(result.stderr || result.stdout);
      const code = looksLikeMissingSubcommand(`${result.stdout}\n${result.stderr}`, "ide-bridge", result.exitCode)
        ? "ide_bridge_missing"
        : "health_nonzero";
      throw new BridgeHealthError(
        code,
        code === "ide_bridge_missing"
          ? "The installed Alysis Code CLI does not support `alysis ide-bridge health`. Upgrade alysis-code."
          : `Alysis Code bridge health exited with code ${result.exitCode}.`,
        { stderr }
      );
    }
    return parseHealthJson(result.stdout);
  }

  public async runDoctor(cliPath: string, config?: AlysisConfig): Promise<CliCommandResult> {
    return this.runner(cliPath, ["sandbox", "doctor", "--smoke"], {
      env: config ? cliCommandEnvironment(config) : process.env
    });
  }

  private detectionFailure(cliPath: string, error: unknown): CliDetectionFailure {
    if (error instanceof BridgeHealthError) {
      return {
        ok: false,
        cliPath,
        code: error.code,
        message: redactForDisplay(error.message),
        details: error.details
          ? redactDeep(error.details) as Record<string, unknown>
          : undefined
      };
    }
    if (isNodeError(error) && error.code === "ENOENT") {
      return {
        ok: false,
        cliPath,
        code: "cli_missing",
        message: `Alysis Code CLI was not found at "${cliPath}". Configure alysis.cliPath or install alysis-code.`
      };
    }
    return {
      ok: false,
      cliPath,
      code: "cli_error",
      message: redactForDisplay(error instanceof Error ? error.message : String(error))
    };
  }
}

export async function defaultCliRunner(
  command: string,
  args: readonly string[],
  options: CliRunnerOptions = {}
): Promise<CliCommandResult> {
  try {
    const result = await execFileAsync(command, [...args], {
      env: options.env,
      timeout: COMMAND_TIMEOUT_MS,
      maxBuffer: MAX_STDIO_BYTES,
      windowsHide: true
    });
    return {
      stdout: result.stdout,
      stderr: result.stderr,
      exitCode: 0
    };
  } catch (error) {
    if (isNodeError(error) && error.code === "ENOENT") {
      throw error;
    }
    if (isExecFileError(error)) {
      return {
        stdout: typeof error.stdout === "string" ? error.stdout : "",
        stderr: typeof error.stderr === "string" ? error.stderr : error.message,
        exitCode: typeof error.code === "number" ? error.code : 1
      };
    }
    throw error;
  }
}

export function redactForDisplay(value: string): string {
  return value
    .replace(/(authorization\s*[:=]\s*)(?:bearer\s+)?[A-Za-z0-9._~+/=-]{8,}/gi, "$1<redacted>")
    .replace(/\bbearer\s+[A-Za-z0-9._~+/=-]{8,}/gi, "Bearer <redacted>")
    .replace(
      /(\b(?:api[_-]?key|access[_-]?key|client[_-]?secret|private[_-]?key|password|passwd|token|secret|credential|cookie)["']?\s*[:=]\s*["']?)[^\s"',;&}]{6,}/gi,
      "$1<redacted>"
    )
    .replace(/\bsk-[A-Za-z0-9_-]{12,}\b/g, "<redacted>");
}

// A string value under one of these keys is redacted wholesale even if it doesn't match the
// bearer/sk- patterns — defense-in-depth for a credential that the bridge passes through as a bare
// token (e.g. {"password": "..."}). The bridge already scrubs secrets, so this is belt-and-braces.
const SENSITIVE_VALUE_KEY = /(?:token|secret|password|passwd|authorization|api[_-]?key|access[_-]?key|client[_-]?secret|private[_-]?key|credential|cookie)/i;

// Structure-preserving redaction: maps redactForDisplay over every string (keys and values) of an
// arbitrary structured payload, so a result can be rendered as kv/table DOM while staying as redacted
// as the existing whole-JSON-string redaction (stronger-or-equal). Non-strings pass through unchanged.
export function redactDeep(value: unknown): unknown {
  if (typeof value === "string") {
    return redactForDisplay(value);
  }
  if (Array.isArray(value)) {
    return value.map((entry) => redactDeep(entry));
  }
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [key, entry] of Object.entries(value)) {
      out[redactForDisplay(key)] =
        SENSITIVE_VALUE_KEY.test(key) && typeof entry === "string" ? "<redacted>" : redactDeep(entry);
    }
    return out;
  }
  return value;
}

function normalizeMode(value: string): AlysisMode {
  return value === "readonly" || value === "auto" ? value : "review";
}

// argparse and click both exit with the POSIX usage-error status for an unknown subcommand.
const USAGE_ERROR_EXIT_CODE = 2;
// Locale-independent tokens a CLI can emit for the same condition; matched before any English text.
const MISSING_SUBCOMMAND_MARKERS = ["unknown_command", "no_such_command", "unrecognized_command"];
const MISSING_SUBCOMMAND_PHRASES = [
  "no such command",
  "unknown command",
  "invalid choice",
  "unrecognized arguments",
  "unknown option"
];

/**
 * Classify "this CLI is too old to have the subcommand" apart from "the subcommand failed". English
 * phrase matching alone loses that distinction on a localized Python install (argparse messages are
 * translated), so a machine-readable marker or the usage-error exit code decides it instead. Both
 * fallbacks still require the offending subcommand to appear in the output — argparse and click echo
 * the rejected argument verbatim in every locale.
 */
export function looksLikeMissingSubcommand(
  output: string,
  subcommand: string,
  exitCode?: number | null
): boolean {
  const normalized = output.toLowerCase();
  const command = subcommand.toLowerCase();
  if (MISSING_SUBCOMMAND_MARKERS.some((marker) => normalized.includes(marker))) {
    return true;
  }
  if (!normalized.includes(command)) {
    return false;
  }
  if (MISSING_SUBCOMMAND_PHRASES.some((phrase) => normalized.includes(phrase))) {
    return true;
  }
  return exitCode === USAGE_ERROR_EXIT_CODE;
}

function normalizeSandboxProfile(value: string): AlysisConfig["sandboxProfile"] {
  if (value === "strict" || value === "warn" || value === "off") {
    return value;
  }
  return "default";
}

function normalizeBaseUrl(value: unknown): string {
  const normalized = String(value ?? "").trim();
  if (!normalized) {
    return "";
  }
  if (/\s/.test(normalized)) {
    return "";
  }
  try {
    const parsed = new URL(normalized);
    if (
      (parsed.protocol !== "http:" && parsed.protocol !== "https:")
      || !parsed.host
      || parsed.username.length > 0
      || parsed.password.length > 0
      || parsed.search.length > 0
      || parsed.hash.length > 0
    ) {
      return "";
    }
    // The stored API key is forwarded to this host. Plain http is only safe when the
    // bytes never leave the machine (Ollama, LM Studio, a local gateway); anywhere
    // else it would put the credential on the wire in clear text.
    if (parsed.protocol === "http:" && !isLoopbackHostname(parsed.hostname)) {
      return "";
    }
  } catch {
    return "";
  }
  return normalized;
}

/** `new URL()` keeps IPv6 hostnames in brackets, so `[::1]` is matched literally. */
function isLoopbackHostname(hostname: string): boolean {
  const host = hostname.trim().toLowerCase();
  return host === "localhost"
    || host === "::1"
    || host === "[::1]"
    || /^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(host);
}

function normalizeBoolean(value: unknown, defaultValue: boolean): boolean {
  return typeof value === "boolean" ? value : defaultValue;
}

function normalizePositiveIntegerOverride(value: unknown): number | undefined {
  const parsed = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return undefined;
  }
  const integer = Math.floor(parsed);
  return Number.isSafeInteger(integer) ? integer : undefined;
}

interface SettingRead<T> {
  value: T;
  source: ConfigValueSource;
  ignoredWorkspaceValue: boolean;
}

function readSensitiveSetting<T>(
  reader: ConfigReader,
  key: SensitiveSetting,
  defaultValue: T,
  isWorkspaceTrusted: boolean,
  onNotice: (notice: SafeConfigNotice) => void
): SettingRead<T> {
  if (isWorkspaceTrusted) {
    return {
      value: reader.get(key, defaultValue),
      source: configSource(reader.inspect ? reader.inspect<T>(key) : undefined, "unknown"),
      ignoredWorkspaceValue: false
    };
  }

  const inspected = reader.inspect ? reader.inspect<T>(key) : undefined;
  const ignoredWorkspaceValue = hasWorkspaceScopedValue(inspected);
  if (ignoredWorkspaceValue) {
    onNotice({
      code: "workspace_setting_ignored",
      setting: key,
      message:
        key === "cliPath"
          ? "Workspace-defined Alysis Code CLI path is ignored until Workspace Trust is granted."
          : `Workspace-defined Alysis Code setting "${key}" is ignored until Workspace Trust is granted.`
    });
  }

  if (!inspected) {
    return { value: defaultValue, source: "default", ignoredWorkspaceValue };
  }

  if (inspected.globalLanguageValue !== undefined) {
    return { value: inspected.globalLanguageValue, source: "globalLanguage", ignoredWorkspaceValue };
  }
  if (inspected.globalValue !== undefined) {
    return { value: inspected.globalValue, source: "global", ignoredWorkspaceValue };
  }
  if (inspected.defaultLanguageValue !== undefined) {
    return { value: inspected.defaultLanguageValue, source: "default", ignoredWorkspaceValue };
  }
  if (inspected.defaultValue !== undefined) {
    return { value: inspected.defaultValue, source: "default", ignoredWorkspaceValue };
  }
  return { value: defaultValue, source: "default", ignoredWorkspaceValue };
}

function configSource<T>(
  inspected: ConfigInspect<T> | undefined,
  fallback: ConfigValueSource
): ConfigValueSource {
  if (!inspected) {
    return fallback;
  }
  if (inspected.workspaceFolderLanguageValue !== undefined) {
    return "workspaceFolderLanguage";
  }
  if (inspected.workspaceFolderValue !== undefined) {
    return "workspaceFolder";
  }
  if (inspected.workspaceLanguageValue !== undefined) {
    return "workspaceLanguage";
  }
  if (inspected.workspaceValue !== undefined) {
    return "workspace";
  }
  if (inspected.globalLanguageValue !== undefined) {
    return "globalLanguage";
  }
  if (inspected.globalValue !== undefined) {
    return "global";
  }
  return "default";
}

function hasWorkspaceScopedValue<T>(inspected: ConfigInspect<T> | undefined): boolean {
  return Boolean(
    inspected &&
      (inspected.workspaceValue !== undefined ||
        inspected.workspaceFolderValue !== undefined ||
        inspected.workspaceLanguageValue !== undefined ||
        inspected.workspaceFolderLanguageValue !== undefined)
  );
}

function resolveCliPathSecurity(
  cliPath: string,
  read: SettingRead<string>,
  options: {
    isWorkspaceTrusted: boolean;
    workspaceRoots: readonly string[];
    executableResolver?: ExecutableResolverOptions;
  }
): ResolvedCliPath {
  const value = cliPath.length > 0 ? cliPath : "alysis";
  const platform = options.executableResolver?.platform ?? process.platform;
  const executable = resolveExecutablePath(value, options.executableResolver);
  if (options.isWorkspaceTrusted) {
    return {
      value,
      resolvedExecutablePath: executable.resolvedPath,
      source: read.source,
      trusted: true,
      executionAllowed: true,
      apiKeyForwardingAllowed: true
    };
  }

  const blocked = (reason: string): ResolvedCliPath => ({
    value,
    resolvedExecutablePath: executable.resolvedPath,
    source: read.source,
    trusted: false,
    executionAllowed: false,
    apiKeyForwardingAllowed: false,
    reason
  });

  if (read.ignoredWorkspaceValue) {
    return blocked("Workspace-defined Alysis Code CLI path is ignored until Workspace Trust is granted.");
  }

  if (cliPath.length > 0 && isRelativePathLike(cliPath, platform)) {
    return blocked("Workspace-relative Alysis Code CLI paths require Workspace Trust.");
  }

  if (
    cliPath.length > 0 &&
    isAbsolutePathLike(cliPath, platform) &&
    isPathInsideAnyWorkspace(cliPath, options.workspaceRoots, platform)
  ) {
    return blocked("Workspace-local Alysis Code CLI paths require Workspace Trust.");
  }

  if (executable.resolvedPath !== null && isPathInsideAnyWorkspace(executable.resolvedPath, options.workspaceRoots, platform)) {
    return blocked("PATH-resolved Alysis Code CLI paths inside the workspace require Workspace Trust.");
  }

  return {
    value,
    resolvedExecutablePath: executable.resolvedPath,
    source: read.source,
    trusted: true,
    executionAllowed: true,
    apiKeyForwardingAllowed: executable.resolvedPath !== null
  };
}

function isNodeError(error: unknown): error is NodeJS.ErrnoException {
  return error instanceof Error && "code" in error;
}

function isExecFileError(error: unknown): error is NodeJS.ErrnoException & { stdout?: string; stderr?: string } {
  return error instanceof Error && ("stdout" in error || "stderr" in error || "code" in error);
}
