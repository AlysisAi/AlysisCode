import assert from "node:assert/strict";
import { writeFileSync } from "node:fs";
import { isAbsolute, relative, resolve } from "node:path";

import * as vscode from "vscode";

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
    reason?: unknown;
    protocolVersion?: unknown;
    alysisVersion?: unknown;
  };
}

export async function run(): Promise<void> {
  const expectedTarget = requiredEnvironment("ALYSIS_EXPECTED_PLATFORM_TARGET");
  const expectedExtensionsDirectory = resolve(
    requiredEnvironment("ALYSIS_EXPECTED_EXTENSIONS_DIR")
  );
  const resultPath = requiredEnvironment("ALYSIS_PRODUCTION_DOGFOOD_RESULT");
  assert.equal(
    requiredEnvironment("ALYSIS_NATIVE_SIGNATURE_CHECK"),
    "passed",
    "The platform-native signature must be verified before production dogfood."
  );
  const releaseSignatureCheck = requiredEnvironment("ALYSIS_RELEASE_SIGNATURE_CHECK");
  assert.equal(
    releaseSignatureCheck,
    "passed",
    "The final VSIX provenance and SBOM attestations must be verified before production dogfood."
  );

  const extension = vscode.extensions.getExtension("alysisai.vscode-alysis");
  assert.ok(extension, "The target VSIX must be installed in the isolated extensions directory.");
  const installedRelativePath = relative(expectedExtensionsDirectory, resolve(extension.extensionPath));
  assert.equal(
    installedRelativePath.length > 0 &&
      !installedRelativePath.startsWith("..") &&
      !isAbsolute(installedRelativePath),
    true,
    "Alysis Code must load from the isolated installed extensions directory, not extensionDevelopmentPath."
  );
  await extension.activate();

  const evidence = await vscode.commands.executeCommand<RuntimeEvidence>(
    "alysis.runtimeEvidence"
  );
  assert.ok(evidence && typeof evidence === "object", "Runtime evidence command returned no object.");
  assert.equal(evidence.extensionMode, "production");
  assert.equal(evidence.origin, "managed", String(evidence.health?.reason ?? "Managed runtime unavailable."));
  assert.equal(evidence.production, true);
  assert.equal(evidence.target, expectedTarget);
  assert.equal(evidence.cliPathOverride, "");
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

  writeFileSync(
    resultPath,
    JSON.stringify(
      {
        status: "passed",
        schema_name: "installed-production-vsix",
        schema_version: 1,
        clean_install: true,
        extension_version: String(extension.packageJSON.version),
        vscode_version: vscode.version,
        host_platform: process.platform,
        host_arch: process.arch,
        remote_name: vscode.env.remoteName ?? "",
        workspace_trusted: vscode.workspace.isTrusted,
        workspace_scheme: vscode.workspace.workspaceFolders?.[0]?.uri.scheme ?? "",
        workspace_authority: vscode.workspace.workspaceFolders?.[0]?.uri.authority ?? "",
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
        release_signature_check: releaseSignatureCheck,
        managed_manifest_signature_check: "passed",
        native_signature_check: "passed",
        package_install_check: "passed",
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
    throw new Error(`${name} is required for installed production VSIX dogfood.`);
  }
  return value;
}
