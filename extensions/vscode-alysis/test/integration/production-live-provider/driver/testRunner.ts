import assert from "node:assert/strict";
import { writeFileSync } from "node:fs";
import { isAbsolute, relative, resolve } from "node:path";

import * as vscode from "vscode";

import type { AlysisExtensionApi } from "../../../../src/extension";
import type { InstalledLiveProviderDriverApi } from "./extension";
import {
  restrictLiveQaEnvironment,
  safeErrorEvidence,
  takeEnvironmentSecret
} from "../../liveQaSecurity";

const LIVE_API_KEY_ENVIRONMENT = "ALYSIS_LIVE_API_KEY";
const QA_ONCE_ENVIRONMENT = "ALYSIS_INSTALLED_PRODUCTION_QA_ONCE";
const PROFILE_NONCE_ENVIRONMENT = "ALYSIS_INSTALLED_LIVE_PROFILE_NONCE";
const DRIVER_EXTENSION_ID = "alysisai.alysis-installed-live-provider-driver";

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
  health?: { status?: unknown };
}

export async function run(): Promise<void> {
  const resultPath = requiredEnvironment("ALYSIS_INSTALLED_LIVE_RESULT");
  try {
    await runPhase(resultPath);
  } catch (error) {
    const redactedError = safeErrorEvidence(error);
    writeFileSync(
      resultPath,
      JSON.stringify({ status: "failed", error: redactedError }, null, 2) + "\n",
      { encoding: "utf8", mode: 0o600 }
    );
    throw new Error(
      `Installed production live-provider phase failed (${redactedError.name}, length=${redactedError.length}, sha256=${redactedError.sha256}).`
    );
  }
}

async function runPhase(resultPath: string): Promise<void> {
  const phase = requiredEnvironment("ALYSIS_INSTALLED_LIVE_PHASE");
  const expectedTarget = requiredEnvironment("ALYSIS_EXPECTED_PLATFORM_TARGET");
  const expectedExtensionsDirectory = resolve(
    requiredEnvironment("ALYSIS_EXPECTED_EXTENSIONS_DIR")
  );
  const expectedMarker = requiredEnvironment("ALYSIS_INSTALLED_LIVE_MARKER");
  const profileNonce = requiredEnvironment(PROFILE_NONCE_ENVIRONMENT);
  assert.ok(phase === "chat" || phase === "restart", "Live-provider phase is invalid.");
  assert.equal(requiredEnvironment("ALYSIS_NATIVE_SIGNATURE_CHECK"), "passed");
  assert.equal(requiredEnvironment("ALYSIS_RELEASE_SIGNATURE_CHECK"), "passed");

  let apiKey = phase === "chat" ? takeEnvironmentSecret(LIVE_API_KEY_ENVIRONMENT) : "";
  restrictLiveQaEnvironment(process.env, apiKey || undefined);

  const driverExtension = vscode.extensions.getExtension(DRIVER_EXTENSION_ID) as
    | vscode.Extension<InstalledLiveProviderDriverApi>
    | undefined;
  assert.ok(driverExtension, "The installed live-provider driver was not discoverable.");
  const driver = await driverExtension.activate();
  let profileWitness: "written" | "observed";
  if (phase === "chat") {
    await driver.writeProfileWitness(profileNonce);
    assert.equal(driver.readProfileWitness(), profileNonce, "Profile witness write did not persist.");
    profileWitness = "written";
  } else {
    assert.equal(
      driver.readProfileWitness(),
      profileNonce,
      "The second Extension Host did not observe the persisted profile witness."
    );
    profileWitness = "observed";
  }

  const extension = vscode.extensions.getExtension("alysisai.vscode-alysis") as
    | vscode.Extension<AlysisExtensionApi | undefined>
    | undefined;
  assert.ok(extension, "The installed Alysis Code extension was not discoverable.");
  assert.equal(extension.isActive, false, "The target extension activated before secret consumption.");
  const installedRelativePath = relative(expectedExtensionsDirectory, resolve(extension.extensionPath));
  assert.equal(
    installedRelativePath.length > 0 &&
      !installedRelativePath.startsWith("..") &&
      !isAbsolute(installedRelativePath),
    true,
    "Alysis Code did not load from the isolated installed extensions directory."
  );

  const exports = await extension.activate();
  assert.equal(process.env[QA_ONCE_ENVIRONMENT], undefined, "The QA flag was not consumed.");
  assert.ok(exports?.installedProductionQa, "The one-shot installed-production QA API is absent.");
  assert.equal(exports.test, undefined, "ExtensionMode.Test API leaked into a production host.");
  const qa = exports.installedProductionQa;
  const runtime = await vscode.commands.executeCommand<RuntimeEvidence>(
    "alysis.runtimeEvidence"
  );
  validateRuntimeEvidence(runtime, expectedTarget);

  let response:
    | {
        responseLength: number;
        responseSha256: string;
        markerMatched: boolean;
        elapsedMs: number;
      }
    | undefined;
  let secretStored = false;
  let secretCleared = false;
  try {
    if (phase === "chat") {
      await qa.storeProviderSecret(apiKey);
      secretStored = await qa.hasProviderSecret();
      assert.equal(secretStored, true, "SecretStorage did not report a configured provider key.");
      response = await qa.runReadonlyChat(
        `Without reading files, calling tools, or changing anything, reply exactly with ${expectedMarker}.`,
        expectedMarker
      );
      assert.equal(response.markerMatched, true, "The real provider did not return the exact marker.");
    } else {
      assert.equal(
        await qa.hasProviderSecret(),
        false,
        "The provider secret remained in the reused profile after restart."
      );
      secretCleared = true;
    }
  } finally {
    await qa.clearProviderSecret();
    secretCleared = !(await qa.hasProviderSecret());
    apiKey = "";
  }

  writeFileSync(
    resultPath,
    JSON.stringify(
      {
        status: "passed",
        phase,
        extension_version: String(extension.packageJSON.version),
        vscode_version: vscode.version,
        host_platform: process.platform,
        host_arch: process.arch,
        remote_name: vscode.env.remoteName ?? "",
        workspace_trusted: vscode.workspace.isTrusted,
        workspace_scheme: vscode.workspace.workspaceFolders?.[0]?.uri.scheme ?? "",
        extension_mode: runtime.extensionMode,
        runtime_origin: runtime.origin,
        runtime_production: runtime.production,
        managed_artifact_version: runtime.artifactVersion,
        managed_cli_version: runtime.cliVersion,
        managed_protocol_version: runtime.protocolVersion,
        managed_cli_sha256: runtime.sha256,
        release_tag: runtime.releaseTag,
        source_repository: runtime.sourceRepository,
        source_sha: runtime.sourceCommit,
        platform_target: runtime.target,
        managed_manifest_signature_check: "passed",
        native_signature_check: "passed",
        package_install_check: "passed",
        bridge_health: "passed",
        readonly_mode: vscode.workspace
          .getConfiguration("alysis")
          .get<string>("defaultMode") === "readonly",
        secret_storage_set: phase === "chat" ? secretStored : null,
        secret_storage_cleared: secretCleared,
        profile_witness: profileWitness,
        response: response
          ? {
              length: response.responseLength,
              sha256: response.responseSha256,
              marker_matched: response.markerMatched,
              elapsed_ms: response.elapsedMs
            }
          : null
      },
      null,
      2
    ) + "\n",
    { encoding: "utf8", mode: 0o600 }
  );
}

function validateRuntimeEvidence(evidence: RuntimeEvidence | undefined, expectedTarget: string): void {
  assert.ok(evidence && typeof evidence === "object", "Runtime evidence was unavailable.");
  assert.equal(evidence.extensionMode, "production");
  assert.equal(evidence.origin, "managed");
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
}

function requiredEnvironment(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) {
    throw new Error(`${name} is required for installed production live-provider QA.`);
  }
  return value;
}
