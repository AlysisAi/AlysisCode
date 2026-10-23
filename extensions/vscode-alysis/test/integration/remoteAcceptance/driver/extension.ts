import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync, unlinkSync, writeFileSync } from "node:fs";
import { hostname } from "node:os";
import { join } from "node:path";

import * as vscode from "vscode";

const TARGET_EXTENSION_ID = "alysisai.vscode-alysis";
const CONFIG_NAME = ".alysis-remote-acceptance-config.json";
const RESULT_NAME = ".alysis-remote-acceptance-result.json";
const TRUST_REQUEST_NAME = ".alysis-remote-acceptance-trust-request.json";

interface DriverConfig {
  schema_name: "vscode-remote-acceptance-driver-config";
  schema_version: 2;
  environment: "wsl" | "remote_ssh";
  release_tag: string;
  source_sha: string;
  candidate_run_id: number;
  workflow_run_id: number;
  workflow_run_attempt: number;
  acceptance_id: string;
  started_at: string;
  expected_extension_version: string;
  expected_vsix_name: string;
  expected_vsix_sha256: string;
  expected_managed_artifact_version: string;
  expected_managed_cli_sha256: string;
  expected_vscode_version: string;
  expected_vscode_commit: string;
  expected_vscode_executable_sha256: string;
  expected_remote_name: "wsl" | "ssh-remote";
  expected_runner_role: "real-wsl" | "real-remote-ssh";
  expected_prerequisite_id: string;
  expected_prerequisite_version: string;
  expected_prerequisite_sha256: string;
  identity_salt: string;
  expected_authority: string;
  expected_authority_sha256: string;
  expected_hostname_sha256: string;
  expected_workspace_path: string;
  expected_workspace_path_sha256: string;
}

interface RuntimeEvidence {
  origin?: unknown;
  production?: unknown;
  artifactVersion?: unknown;
  cliVersion?: unknown;
  protocolVersion?: unknown;
  target?: unknown;
  sha256?: unknown;
  releaseTag?: unknown;
  sourceRepository?: unknown;
  sourceCommit?: unknown;
  releaseSignatureVerified?: unknown;
  extensionMode?: unknown;
  cliPathOverride?: unknown;
  health?: {
    status?: unknown;
    protocolVersion?: unknown;
    alysisVersion?: unknown;
  };
}

interface PhaseIdentity {
  artifactVersion: string;
  cliVersion: string;
  protocolVersion: string;
  sha256: string;
  extensionVersion: string;
}

interface RemoteHostPolicy {
  isRemoteExtensionHost(remoteName: string | undefined): boolean;
  mcpOAuthRemoteUnavailableReason(remoteName: string | undefined): string | undefined;
}

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  try {
    await run(context);
  } catch (error) {
    const workspace = vscode.workspace.workspaceFolders?.[0];
    if (workspace?.uri.scheme === "vscode-remote") {
      const failurePath = join(workspace.uri.fsPath, RESULT_NAME);
      const message = error instanceof Error ? error.message : String(error);
      writeFileSync(
        failurePath,
        JSON.stringify({ schema_name: "vscode-remote-installed-vsix-acceptance", status: "failed", reason: message }, null, 2) + "\n",
        { encoding: "utf8", mode: 0o600 }
      );
    }
    throw error;
  }
}

export function deactivate(): void {
  // The persisted phase checkpoint deliberately survives the first Extension Host reload.
}

async function run(context: vscode.ExtensionContext): Promise<void> {
  const workspaceFolders = vscode.workspace.workspaceFolders;
  assert.equal(workspaceFolders?.length, 1, "Remote acceptance requires exactly one workspace.");
  const workspace = workspaceFolders![0];
  assert.equal(workspace.uri.scheme, "vscode-remote", "Workspace must use a real remote URI.");
  const config = readConfig(join(workspace.uri.fsPath, CONFIG_NAME));
  const workspaceTrustEnabled = vscode.workspace
    .getConfiguration("security.workspace.trust")
    .get<boolean>("enabled");
  assert.equal(workspaceTrustEnabled, true, "Remote acceptance must keep Workspace Trust enabled.");
  const trustWitnessKey = `remoteAcceptanceTrust:${config.acceptance_id}`;
  const trustWitness = context.globalState.get<boolean>(trustWitnessKey) === true;
  if (!vscode.workspace.isTrusted) {
    assert.equal(trustWitness, false, "A prior trust witness cannot coexist with Restricted Mode.");
    await grantExactWorkspaceTrust(workspace.uri.fsPath);
    await context.globalState.update(trustWitnessKey, true);
  } else {
    assert.equal(
      trustWitness,
      true,
      "Remote acceptance must begin in Restricted Mode and use the pinned Trust-editor action."
    );
  }
  assert.equal(vscode.workspace.isTrusted, true, "Remote acceptance requires a trusted workspace.");
  assert.equal(process.platform, "linux", "Remote Extension Host must run on Linux.");
  assert.equal(process.arch, "x64", "Remote Extension Host must run the linux-x64 candidate.");

  assert.equal(vscode.env.remoteName, config.expected_remote_name, "Unexpected VS Code remote host type.");
  assert.equal(workspace.uri.authority, config.expected_authority, "Unexpected remote URI authority.");
  assert.equal(workspace.uri.fsPath, config.expected_workspace_path, "Unexpected remote workspace path.");
  assert.equal(vscode.version, config.expected_vscode_version, "Unexpected VS Code version.");
  assert.equal(
    saltedHash(config.identity_salt, workspace.uri.authority),
    config.expected_authority_sha256,
    "Remote authority hash did not match preflight."
  );
  assert.equal(
    saltedHash(config.identity_salt, hostname()),
    config.expected_hostname_sha256,
    "Remote hostname changed between preflight and Extension Host activation."
  );
  assert.equal(
    saltedHash(config.identity_salt, workspace.uri.fsPath),
    config.expected_workspace_path_sha256,
    "Remote workspace path hash did not match preflight."
  );

  const target = vscode.extensions.getExtension(TARGET_EXTENSION_ID);
  assert.ok(target, "The target VSIX is not installed in the remote Extension Host.");
  assert.equal(target.packageJSON.version, config.expected_extension_version);
  await target.activate();
  const runtime = await vscode.commands.executeCommand<RuntimeEvidence>("alysis.runtimeEvidence");
  assertRuntime(runtime, config);
  assertOAuthFailClosed(target.extensionPath, config.expected_remote_name);
  const commands = await vscode.commands.getCommands(true);
  assert.ok(
    commands.includes("alysis.backend.mcp.auth.login.start"),
    "Packaged backend OAuth command is missing."
  );

  const identity: PhaseIdentity = {
    artifactVersion: String(runtime.artifactVersion),
    cliVersion: String(runtime.cliVersion),
    protocolVersion: String(runtime.protocolVersion),
    sha256: String(runtime.sha256),
    extensionVersion: String(target.packageJSON.version)
  };
  const checkpointKey = `remoteAcceptance:${config.acceptance_id}`;
  const previous = context.globalState.get<PhaseIdentity>(checkpointKey);
  if (!previous) {
    await context.globalState.update(checkpointKey, identity);
    setTimeout(() => {
      void vscode.commands.executeCommand("workbench.action.reloadWindow");
    }, 250);
    return;
  }

  assert.deepEqual(identity, previous, "Managed runtime identity changed across Extension Host reload.");
  await context.globalState.update(checkpointKey, undefined);
  await context.globalState.update(trustWitnessKey, undefined);
  const report = {
    schema_name: "vscode-remote-installed-vsix-acceptance",
    schema_version: 2,
    status: "passed",
    environment: config.environment,
    release_tag: config.release_tag,
    source_sha: config.source_sha,
    candidate_run_id: config.candidate_run_id,
    workflow_run_id: config.workflow_run_id,
    workflow_run_attempt: config.workflow_run_attempt,
    acceptance_id: config.acceptance_id,
    started_at: config.started_at,
    completed_at: new Date().toISOString(),
    extension: {
      id: TARGET_EXTENSION_ID,
      version: config.expected_extension_version,
      mode: runtime.extensionMode,
      vsix_name: config.expected_vsix_name,
      vsix_sha256: config.expected_vsix_sha256,
      installed_path_sha256: saltedHash(config.identity_salt, target.extensionPath)
    },
    vscode: {
      version: vscode.version,
      commit: config.expected_vscode_commit,
      executable_sha256: config.expected_vscode_executable_sha256
    },
    prerequisite: {
      id: config.expected_prerequisite_id,
      version: config.expected_prerequisite_version,
      sha256: config.expected_prerequisite_sha256
    },
    host: {
      platform: process.platform,
      arch: process.arch,
      remote_name: vscode.env.remoteName,
      runner_role: config.expected_runner_role,
      authority_sha256: config.expected_authority_sha256,
      hostname_sha256: config.expected_hostname_sha256,
      workspace_scheme: workspace.uri.scheme,
      workspace_path_sha256: config.expected_workspace_path_sha256,
      workspace_trusted: vscode.workspace.isTrusted,
      workspace_trust_enabled: workspaceTrustEnabled,
      workspace_trust_grant_method: "workspace_trust_editor_ctrl_enter",
      workspace_trust_initial_state: "restricted",
      workspace_trust_parent_grant: false
    },
    runtime: {
      origin: runtime.origin,
      production: runtime.production,
      target: runtime.target,
      artifact_version: runtime.artifactVersion,
      cli_version: runtime.cliVersion,
      protocol_version: runtime.protocolVersion,
      sha256: runtime.sha256,
      release_tag: runtime.releaseTag,
      source_repository: runtime.sourceRepository,
      source_sha: runtime.sourceCommit,
      signature_verified: runtime.releaseSignatureVerified
    },
    reload: {
      phases: 2,
      profile_state_reused: true,
      runtime_identity_stable: true
    },
    checks: {
      bridge_health: "passed",
      clean_install: "passed",
      extension_reload_recovery: "passed",
      managed_runtime: "passed",
      oauth_fail_closed: "passed",
      remote_identity: "passed",
      secret_leak_check: "passed",
      workspace_trust_enforced: "passed"
    }
  };
  assertNoSensitiveValues(report);
  writeFileSync(
    join(workspace.uri.fsPath, RESULT_NAME),
    JSON.stringify(report, null, 2) + "\n",
    { encoding: "utf8", mode: 0o600 }
  );
  setTimeout(() => {
    void vscode.commands.executeCommand("workbench.action.closeWindow");
  }, 250);
}

function readConfig(path: string): DriverConfig {
  const parsed: unknown = JSON.parse(readFileSync(path, "utf8"));
  assert.ok(parsed && typeof parsed === "object" && !Array.isArray(parsed));
  const config = parsed as DriverConfig;
  assert.equal(config.schema_name, "vscode-remote-acceptance-driver-config");
  assert.equal(config.schema_version, 2);
  assert.match(config.acceptance_id, /^[0-9]+-[0-9]+-(?:wsl|remote_ssh)$/);
  assert.match(config.source_sha, /^[0-9a-f]{40}$/);
  assert.match(config.expected_vsix_sha256, /^[0-9a-f]{64}$/);
  assert.match(config.expected_managed_cli_sha256, /^[0-9a-f]{64}$/);
  assert.match(config.identity_salt, /^[0-9a-f]{64}$/);
  assert.match(config.expected_authority_sha256, /^[0-9a-f]{64}$/);
  assert.match(config.expected_hostname_sha256, /^[0-9a-f]{64}$/);
  assert.match(config.expected_workspace_path_sha256, /^[0-9a-f]{64}$/);
  assert.match(config.expected_vscode_executable_sha256, /^[0-9a-f]{64}$/);
  assert.match(config.expected_prerequisite_sha256, /^[0-9a-f]{64}$/);
  return config;
}

function assertRuntime(runtime: RuntimeEvidence | undefined, config: DriverConfig): asserts runtime is RuntimeEvidence {
  assert.ok(runtime && typeof runtime === "object", "Runtime evidence command returned no object.");
  assert.equal(runtime.extensionMode, "production");
  assert.equal(runtime.origin, "managed");
  assert.equal(runtime.production, true);
  assert.equal(runtime.target, "linux-x64");
  assert.equal(runtime.artifactVersion, config.expected_managed_artifact_version);
  assert.equal(runtime.sha256, config.expected_managed_cli_sha256);
  assert.equal(runtime.releaseTag, config.release_tag);
  assert.equal(runtime.sourceRepository, "https://github.com/AlysisAi/AlysisCode");
  assert.equal(runtime.sourceCommit, config.source_sha);
  assert.equal(runtime.releaseSignatureVerified, true);
  assert.equal(runtime.cliPathOverride, "");
  assert.match(String(runtime.cliVersion ?? ""), /^\d+(?:\.\d+){2}/);
  assert.match(String(runtime.protocolVersion ?? ""), /\S/);
  assert.equal(runtime.health?.status, "passed");
  assert.equal(runtime.health?.protocolVersion, runtime.protocolVersion);
  assert.equal(runtime.health?.alysisVersion, runtime.cliVersion);
}

function assertOAuthFailClosed(extensionPath: string, remoteName: string): void {
  const modulePath = join(extensionPath, "out", "src", "security", "remoteHost.js");
  const policy = require(modulePath) as RemoteHostPolicy;
  assert.equal(policy.isRemoteExtensionHost(remoteName), true);
  const reason = policy.mcpOAuthRemoteUnavailableReason(remoteName);
  assert.match(
    reason ?? "",
    /OAuth login is unavailable in a remote VS Code extension host/,
    "The packaged OAuth policy did not fail closed for the real remote host."
  );
}

async function grantExactWorkspaceTrust(workspacePath: string): Promise<void> {
  const markerPath = join(workspacePath, TRUST_REQUEST_NAME);
  let timeout: NodeJS.Timeout | undefined;
  let listener: vscode.Disposable | undefined;
  try {
    const granted = new Promise<void>((resolve, reject) => {
      listener = vscode.workspace.onDidGrantWorkspaceTrust(() => resolve());
      timeout = setTimeout(
        () => reject(new Error("Workspace Trust was not granted through the editor before timeout.")),
        120_000
      );
    });
    await vscode.commands.executeCommand("workbench.trust.manage");
    writeFileSync(
      markerPath,
      JSON.stringify({ schema_name: "vscode-remote-workspace-trust-request", status: "ready" }) + "\n",
      { encoding: "utf8", mode: 0o600 }
    );
    await granted;
  } finally {
    listener?.dispose();
    if (timeout) {
      clearTimeout(timeout);
    }
    try {
      unlinkSync(markerPath);
    } catch {
      // The marker may not exist if opening the Trust editor failed.
    }
  }
  assert.equal(
    vscode.workspace.isTrusted,
    true,
    "The Trust-editor action did not trust the exact current workspace."
  );
}

function saltedHash(salt: string, value: string): string {
  return createHash("sha256").update(`${salt}\0${value}`, "utf8").digest("hex");
}

function assertNoSensitiveValues(value: unknown): void {
  if (Array.isArray(value)) {
    value.forEach(assertNoSensitiveValues);
    return;
  }
  if (typeof value === "string") {
    assert.doesNotMatch(value.toLowerCase(), /(?:api.?key|authorization|bearer |private key|password=)/);
    return;
  }
  if (!value || typeof value !== "object") {
    return;
  }
  for (const nested of Object.values(value)) {
    assertNoSensitiveValues(nested);
  }
}
