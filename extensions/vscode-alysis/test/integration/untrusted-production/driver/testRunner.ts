import assert from "node:assert/strict";
import { existsSync, writeFileSync } from "node:fs";
import { isAbsolute, relative, resolve } from "node:path";

import * as vscode from "vscode";

import { APPROVED_UNTRUSTED_VSCODE_VERSION } from "../contract";

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

export async function run(): Promise<void> {
  const expectedTarget = requiredEnvironment("ALYSIS_EXPECTED_PLATFORM_TARGET");
  const expectedExtensionsDirectory = resolve(
    requiredEnvironment("ALYSIS_EXPECTED_EXTENSIONS_DIR")
  );
  const resultPath = requiredEnvironment("ALYSIS_UNTRUSTED_PRODUCTION_RESULT");
  const maliciousCliPath = requiredEnvironment("ALYSIS_MALICIOUS_WORKSPACE_CLI");
  const maliciousSentinel = requiredEnvironment("ALYSIS_MALICIOUS_CLI_SENTINEL");
  assert.equal(
    requiredEnvironment("ALYSIS_EXPECTED_VSCODE_VERSION"),
    APPROVED_UNTRUSTED_VSCODE_VERSION,
    "The untrusted-workspace gate must use the approved VS Code compatibility build."
  );
  assert.equal(
    vscode.version,
    APPROVED_UNTRUSTED_VSCODE_VERSION,
    "The running Extension Host is not the approved VS Code compatibility build."
  );
  assert.equal(
    requiredEnvironment("ALYSIS_NATIVE_SIGNATURE_CHECK"),
    "passed",
    "Platform-native runtime evidence must pass before this gate."
  );
  assert.equal(
    requiredEnvironment("ALYSIS_RELEASE_SIGNATURE_CHECK"),
    "passed",
    "Candidate provenance and SBOM attestations must pass before this gate."
  );

  assert.equal(vscode.workspace.isTrusted, false, "The live workspace must actually be untrusted.");
  assert.equal(vscode.env.remoteName, undefined, "This gate requires a local Extension Host.");
  const workspaceFolder = vscode.workspace.workspaceFolders?.[0];
  assert.ok(workspaceFolder, "The untrusted local workspace folder must be open.");
  assert.equal(workspaceFolder.uri.scheme, "file");
  assert.equal(workspaceFolder.uri.authority, "");

  const inspected = vscode.workspace
    .getConfiguration("alysis", workspaceFolder.uri)
    .inspect<string>("cliPath");
  assert.ok(inspected, "alysis.cliPath must be inspectable.");
  const workspaceValues = [
    ["workspace", inspected.workspaceValue],
    ["workspace-folder", inspected.workspaceFolderValue]
  ] as const;
  const matchingScopes = workspaceValues.filter(([, value]) => value === maliciousCliPath);
  assert.equal(
    matchingScopes.length,
    1,
    "The hostile workspace-local alysis.cliPath setting must be present exactly once."
  );
  assert.equal(existsSync(maliciousSentinel), false, "The hostile CLI ran before activation.");

  const extension = vscode.extensions.getExtension("alysisai.vscode-alysis");
  assert.ok(extension, "The target VSIX must be installed in the isolated extensions directory.");
  const installedRelativePath = relative(expectedExtensionsDirectory, resolve(extension.extensionPath));
  assert.equal(
    installedRelativePath.length > 0 &&
      !installedRelativePath.startsWith("..") &&
      !isAbsolute(installedRelativePath),
    true,
    "Alysis Code must load from the isolated installed directory, not extensionDevelopmentPath."
  );
  await extension.activate();

  const evidence = await vscode.commands.executeCommand<RuntimeEvidence>(
    "alysis.runtimeEvidence"
  );
  assert.ok(evidence && typeof evidence === "object", "Runtime evidence command returned no object.");
  assert.equal(
    evidence.extensionMode,
    "production",
    "The installed extension's ExtensionContext must report ExtensionMode.Production."
  );
  assert.equal(evidence.origin, "managed");
  assert.equal(evidence.production, true);
  assert.equal(evidence.target, expectedTarget);
  assert.equal(
    evidence.cliPathOverride,
    "",
    "The hostile workspace-local CLI override was not ignored."
  );
  assert.match(String(evidence.artifactVersion ?? ""), /\S/);
  assert.match(String(evidence.cliVersion ?? ""), /^\d+(?:\.\d+){2}/);
  assert.match(String(evidence.protocolVersion ?? ""), /\S/);
  assert.match(String(evidence.sha256 ?? ""), /^[0-9a-f]{64}$/);
  assert.match(String(evidence.releaseTag ?? ""), /^v\d+\.\d+\.\d+$/);
  assert.equal(evidence.sourceRepository, "https://github.com/AlysisAi/AlysisCode");
  assert.match(String(evidence.sourceCommit ?? ""), /^[0-9a-f]{40}$/);
  assert.equal(evidence.releaseSignatureVerified, true);
  assert.equal(evidence.health?.status, "passed");
  assert.equal(evidence.health?.protocolVersion, evidence.protocolVersion);
  assert.equal(evidence.health?.alysisVersion, evidence.cliVersion);
  assert.equal(
    existsSync(maliciousSentinel),
    false,
    "The hostile workspace-local CLI was executed."
  );

  writeFileSync(
    resultPath,
    JSON.stringify(
      {
        status: "passed",
        extension_version: String(extension.packageJSON.version),
        vscode_version: vscode.version,
        host_platform: process.platform,
        host_arch: process.arch,
        remote_name: vscode.env.remoteName ?? "",
        workspace_trusted: vscode.workspace.isTrusted,
        workspace_scheme: workspaceFolder.uri.scheme,
        workspace_authority: workspaceFolder.uri.authority,
        workspace_cli_override_scope: matchingScopes[0]?.[0],
        workspace_cli_override_present: true,
        workspace_cli_override_ignored: evidence.cliPathOverride === "",
        malicious_cli_executed: existsSync(maliciousSentinel),
        extension_mode: evidence.extensionMode,
        runtime_origin: evidence.origin,
        runtime_production: evidence.production,
        managed_artifact_version: evidence.artifactVersion,
        managed_cli_version: evidence.cliVersion,
        managed_protocol_version: evidence.protocolVersion,
        managed_cli_sha256: evidence.sha256,
        release_tag: evidence.releaseTag,
        source_repository: evidence.sourceRepository,
        source_sha: evidence.sourceCommit,
        platform_target: evidence.target,
        cli_path_override: evidence.cliPathOverride,
        release_signature_verified: evidence.releaseSignatureVerified,
        managed_manifest_signature_check: "passed",
        native_signature_check: "passed",
        release_attestation_check: "passed",
        bridge_health: "passed"
      },
      null,
      2
    ) + "\n",
    { encoding: "utf8", mode: 0o600 }
  );
}

function requiredEnvironment(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) {
    throw new Error(`${name} is required for untrusted-production Extension Host QA.`);
  }
  return value;
}
