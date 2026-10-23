export const UNTRUSTED_PRODUCTION_CHECKS = [
  "empty_profile",
  "workspace_trust_enabled",
  "workspace_actually_untrusted",
  "local_file_workspace",
  "packaged_extension_production_mode",
  "malicious_workspace_cli_present",
  "malicious_workspace_cli_ignored",
  "malicious_workspace_cli_not_executed",
  "signed_managed_runtime_selected",
  "managed_runtime_health",
  "exact_installed_inventory"
] as const;

export const APPROVED_UNTRUSTED_VSCODE_VERSION = "1.90.0";

export interface UntrustedExtensionHostEvidence {
  status: unknown;
  extension_version: unknown;
  vscode_version: unknown;
  host_platform: unknown;
  host_arch: unknown;
  remote_name: unknown;
  workspace_trusted: unknown;
  workspace_scheme: unknown;
  workspace_authority: unknown;
  workspace_cli_override_scope: unknown;
  workspace_cli_override_present: unknown;
  workspace_cli_override_ignored: unknown;
  malicious_cli_executed: unknown;
  extension_mode: unknown;
  runtime_origin: unknown;
  runtime_production: unknown;
  managed_artifact_version: unknown;
  managed_cli_version: unknown;
  managed_protocol_version: unknown;
  managed_cli_sha256: unknown;
  release_tag: unknown;
  source_repository: unknown;
  source_sha: unknown;
  platform_target: unknown;
  cli_path_override: unknown;
  release_signature_verified: unknown;
  managed_manifest_signature_check: unknown;
  native_signature_check: unknown;
  release_attestation_check: unknown;
  bridge_health: unknown;
}

export interface UntrustedSummaryInput {
  startedAt: string;
  completedAt: string;
  releaseTag: string;
  sourceSha: string;
  candidateRunId: number;
  workflowRunId: number;
  workflowRunAttempt: number;
  expectedTarget: string;
  vsixName: string;
  vsixSha256: string;
  installedInventory: readonly string[];
  evidence: UntrustedExtensionHostEvidence;
}

export function parseInstalledInventory(output: string): string[] {
  return output
    .split(/\r?\n/)
    .map((value) => value.trim().toLowerCase())
    .filter(Boolean)
    .sort();
}

export function requireExactAlysisInventory(
  output: string,
  expectedVersion?: string
): string[] {
  const inventory = parseInstalledInventory(output);
  const expected = expectedVersion
    ? `alysisai.vscode-alysis@${expectedVersion}`
    : undefined;
  if (
    inventory.length !== 1 ||
    (expected
      ? inventory[0] !== expected
      : !/^alysisai\.vscode-alysis@\d+\.\d+\.\d+(?:[-+][0-9a-z.-]+)?$/.test(
          inventory[0] ?? ""
        ))
  ) {
    throw new Error("Installed extension inventory was not exact.");
  }
  return inventory;
}

export function buildUntrustedProductionSummary(
  input: UntrustedSummaryInput
): Record<string, unknown> {
  for (const [field, value] of [
    ["candidateRunId", input.candidateRunId],
    ["workflowRunId", input.workflowRunId],
    ["workflowRunAttempt", input.workflowRunAttempt]
  ] as const) {
    if (!Number.isSafeInteger(value) || value < 1) {
      throw new Error(`${field} must be a positive safe integer.`);
    }
  }
  const evidence = input.evidence;
  requireEqual(evidence.status, "passed", "Extension Host evidence status");
  requireEqual(evidence.workspace_trusted, false, "Workspace Trust state");
  requireEqual(evidence.workspace_scheme, "file", "Workspace URI scheme");
  requireEqual(evidence.workspace_authority, "", "Workspace URI authority");
  requireEqual(evidence.remote_name, "", "Remote host name");
  requireEqual(
    evidence.workspace_cli_override_present,
    true,
    "Malicious workspace CLI setting presence"
  );
  requireEqual(
    evidence.workspace_cli_override_ignored,
    true,
    "Malicious workspace CLI setting isolation"
  );
  requireEqual(evidence.malicious_cli_executed, false, "Malicious CLI execution sentinel");
  requireEqual(evidence.extension_mode, "production", "Packaged extension mode");
  requireEqual(evidence.runtime_origin, "managed", "Runtime origin");
  requireEqual(evidence.runtime_production, true, "Runtime production status");
  requireEqual(evidence.platform_target, input.expectedTarget, "Managed runtime target");
  requireEqual(evidence.cli_path_override, "", "Effective CLI override");
  requireEqual(evidence.release_tag, input.releaseTag, "Runtime release tag");
  requireEqual(evidence.source_sha, input.sourceSha, "Runtime source SHA");
  requireEqual(
    evidence.source_repository,
    "https://github.com/AlysisAi/AlysisCode",
    "Runtime source repository"
  );
  requireEqual(evidence.release_signature_verified, true, "Managed release signature");
  requireEqual(
    evidence.vscode_version,
    APPROVED_UNTRUSTED_VSCODE_VERSION,
    "Approved VS Code compatibility version"
  );
  for (const [field, value] of [
    ["managed_manifest_signature_check", evidence.managed_manifest_signature_check],
    ["native_signature_check", evidence.native_signature_check],
    ["release_attestation_check", evidence.release_attestation_check],
    ["bridge_health", evidence.bridge_health]
  ] as const) {
    requireEqual(value, "passed", field);
  }

  const extensionVersion = requiredVersion(evidence.extension_version, "extension_version");
  const installedInventory = [...input.installedInventory];
  if (
    installedInventory.length !== 1 ||
    installedInventory[0] !== `alysisai.vscode-alysis@${extensionVersion}`
  ) {
    throw new Error("Retained installed extension inventory was not exact.");
  }

  return {
    schema_name: "installed-production-vsix-untrusted-workspace",
    schema_version: 2,
    status: "passed",
    mode: "installed-production-vsix-untrusted-workspace",
    started_at: input.startedAt,
    completed_at: input.completedAt,
    release_tag: input.releaseTag,
    source_repository: "https://github.com/AlysisAi/AlysisCode",
    source_sha: input.sourceSha,
    candidate_run_id: input.candidateRunId,
    workflow_run_id: input.workflowRunId,
    workflow_run_attempt: input.workflowRunAttempt,
    platform_target: input.expectedTarget,
    vsix: input.vsixName,
    vsix_sha256: requiredSha256(input.vsixSha256, "vsix_sha256"),
    extension_version: extensionVersion,
    vscode_version: APPROVED_UNTRUSTED_VSCODE_VERSION,
    host_platform: requiredString(evidence.host_platform, "host_platform"),
    host_arch: requiredString(evidence.host_arch, "host_arch"),
    remote_name: "",
    workspace_trusted: false,
    workspace_scheme: "file",
    workspace_authority: "",
    workspace_trust_mode: "enabled",
    trust_database_seeded: false,
    extension_mode: "production",
    runtime_origin: "managed",
    runtime_production: true,
    managed_artifact_version: requiredString(
      evidence.managed_artifact_version,
      "managed_artifact_version"
    ),
    managed_cli_version: requiredVersion(evidence.managed_cli_version, "managed_cli_version"),
    managed_protocol_version: requiredString(
      evidence.managed_protocol_version,
      "managed_protocol_version"
    ),
    managed_cli_sha256: requiredSha256(evidence.managed_cli_sha256, "managed_cli_sha256"),
    release_signature_verified: true,
    workspace_cli_override_scope: requiredOverrideScope(
      evidence.workspace_cli_override_scope
    ),
    workspace_cli_override_present: true,
    workspace_cli_override_ignored: true,
    cli_path_override: "",
    malicious_cli_executed: false,
    installed_inventory: installedInventory,
    checks: Object.fromEntries(UNTRUSTED_PRODUCTION_CHECKS.map((check) => [check, "passed"]))
  };
}

function requiredOverrideScope(value: unknown): string {
  if (value !== "workspace" && value !== "workspace-folder") {
    throw new Error("workspace_cli_override_scope is invalid.");
  }
  return value;
}

function requiredVersion(value: unknown, label: string): string {
  const text = requiredString(value, label);
  if (!/^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/.test(text)) {
    throw new Error(`${label} is not a semantic version.`);
  }
  return text;
}

function requiredSha256(value: unknown, label: string): string {
  const text = requiredString(value, label);
  if (!/^[0-9a-f]{64}$/.test(text)) {
    throw new Error(`${label} is not a lowercase SHA-256 digest.`);
  }
  return text;
}

function requiredString(value: unknown, label: string): string {
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(`${label} is required.`);
  }
  return value;
}

function requireEqual(actual: unknown, expected: unknown, label: string): void {
  if (actual !== expected) {
    throw new Error(`${label} did not match the untrusted-production contract.`);
  }
}
