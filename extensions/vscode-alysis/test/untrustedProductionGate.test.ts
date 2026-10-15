import assert from "node:assert/strict";
import test from "node:test";

import {
  APPROVED_UNTRUSTED_VSCODE_VERSION,
  buildUntrustedProductionSummary,
  parseInstalledInventory,
  requireExactAlysisInventory,
  UntrustedExtensionHostEvidence,
  UNTRUSTED_PRODUCTION_CHECKS
} from "./integration/untrusted-production/contract";

test("installed inventory parser is normalized and exact", () => {
  assert.deepEqual(
    parseInstalledInventory("\nAlysisAi.VSCode-Alysis@0.1.1\r\n"),
    ["alysisai.vscode-alysis@0.1.1"]
  );
  assert.deepEqual(
    requireExactAlysisInventory("alysisai.vscode-alysis@0.1.1\n", "0.1.1"),
    ["alysisai.vscode-alysis@0.1.1"]
  );
  assert.throws(
    () =>
      requireExactAlysisInventory(
        "alysisai.vscode-alysis@0.1.1\nother.publisher@2.0.0\n"
      ),
    /not exact/
  );
});

test("safe untrusted summary proves the live trust and managed-runtime boundaries", () => {
  assert.equal(APPROVED_UNTRUSTED_VSCODE_VERSION, "1.90.0");
  const summary = buildUntrustedProductionSummary(validInput());

  assert.equal(summary.schema_version, 2);
  assert.equal(summary.workflow_run_id, 456);
  assert.equal(summary.workflow_run_attempt, 2);
  assert.equal(summary.workspace_trusted, false);
  assert.equal(summary.workspace_trust_mode, "enabled");
  assert.equal(summary.trust_database_seeded, false);
  assert.equal(summary.extension_mode, "production");
  assert.equal(summary.runtime_origin, "managed");
  assert.equal(summary.workspace_cli_override_present, true);
  assert.equal(summary.workspace_cli_override_ignored, true);
  assert.equal(summary.malicious_cli_executed, false);
  assert.deepEqual(
    summary.checks,
    Object.fromEntries(UNTRUSTED_PRODUCTION_CHECKS.map((check) => [check, "passed"]))
  );
  assert.equal(JSON.stringify(summary).includes("C:\\"), false);
  assert.equal(JSON.stringify(summary).includes("/tmp/"), false);
});

test("untrusted summary fails closed on a trusted workspace or executed hostile CLI", () => {
  const trusted = validInput();
  trusted.evidence.workspace_trusted = true;
  assert.throws(() => buildUntrustedProductionSummary(trusted), /Workspace Trust state/);

  const executed = validInput();
  executed.evidence.malicious_cli_executed = true;
  assert.throws(() => buildUntrustedProductionSummary(executed), /execution sentinel/);

  const wrongVsCode = validInput();
  wrongVsCode.evidence.vscode_version = "1.130.0";
  assert.throws(
    () => buildUntrustedProductionSummary(wrongVsCode),
    /Approved VS Code compatibility version/
  );

  const invalidAttempt = validInput();
  invalidAttempt.workflowRunAttempt = 0;
  assert.throws(
    () => buildUntrustedProductionSummary(invalidAttempt),
    /workflowRunAttempt must be a positive safe integer/
  );
});

function validInput(): Parameters<typeof buildUntrustedProductionSummary>[0] {
  const evidence: UntrustedExtensionHostEvidence = {
    status: "passed",
    extension_version: "0.1.1",
    vscode_version: APPROVED_UNTRUSTED_VSCODE_VERSION,
    host_platform: "linux",
    host_arch: "x64",
    remote_name: "",
    workspace_trusted: false,
    workspace_scheme: "file",
    workspace_authority: "",
    workspace_cli_override_scope: "workspace-folder",
    workspace_cli_override_present: true,
    workspace_cli_override_ignored: true,
    malicious_cli_executed: false,
    extension_mode: "production",
    runtime_origin: "managed",
    runtime_production: true,
    managed_artifact_version: "cli-0.1.1-linux-x64",
    managed_cli_version: "0.1.1",
    managed_protocol_version: "1",
    managed_cli_sha256: "b".repeat(64),
    release_tag: "v0.1.1",
    source_repository: "https://github.com/AlysisAi/AlysisCode",
    source_sha: "a".repeat(40),
    platform_target: "linux-x64",
    cli_path_override: "",
    release_signature_verified: true,
    managed_manifest_signature_check: "passed",
    native_signature_check: "passed",
    release_attestation_check: "passed",
    bridge_health: "passed"
  };
  return {
    startedAt: "2026-07-30T10:00:00.000Z",
    completedAt: "2026-07-30T10:01:00.000Z",
    releaseTag: "v0.1.1",
    sourceSha: "a".repeat(40),
    candidateRunId: 123,
    workflowRunId: 456,
    workflowRunAttempt: 2,
    expectedTarget: "linux-x64",
    vsixName: "vscode-alysis-linux-x64.vsix",
    vsixSha256: "c".repeat(64),
    installedInventory: ["alysisai.vscode-alysis@0.1.1"],
    evidence
  };
}
