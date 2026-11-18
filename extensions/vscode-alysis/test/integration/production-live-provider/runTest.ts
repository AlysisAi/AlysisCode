import { spawnSync } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import {
  accessSync,
  constants,
  copyFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  statSync,
  writeFileSync
} from "node:fs";
import { tmpdir } from "node:os";
import { basename, join, resolve } from "node:path";

import { downloadAndUnzipVSCode } from "@vscode/test-electron";

import {
  allowlistedQaRuntimeEnvironment,
  assertSecretAbsentFromTrees,
  liveExtensionHostEnvironment,
  restrictLiveQaEnvironment,
  safeErrorEvidence,
  takeEnvironmentSecret,
  validateLiveQaEvidenceIdentifier,
  validateLiveQaProviderEndpoint
} from "../liveQaSecurity";
import { resolveSafeVsCodeCliInvocation } from "../vsCodeCliInvocation";
import { runVsCodeExtensionHostTests } from "../vsCodeExtensionHostInvocation";

const LIVE_API_KEY_ENVIRONMENT = "ALYSIS_LIVE_API_KEY";
const QA_ONCE_ENVIRONMENT = "ALYSIS_INSTALLED_PRODUCTION_QA_ONCE";
const PROFILE_NONCE_ENVIRONMENT = "ALYSIS_INSTALLED_LIVE_PROFILE_NONCE";

function requiredEnvironment(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) {
    throw new Error(`${name} is required for installed production live-provider QA.`);
  }
  return value;
}

function verifyFile(path: string, label: string): void {
  const stat = statSync(path);
  if (!stat.isFile() || stat.size <= 0) {
    throw new Error(`${label} is not a non-empty file.`);
  }
}

function verifyExecutable(path: string): void {
  verifyFile(path, "VS Code executable");
  if (process.platform !== "win32") {
    accessSync(path, constants.X_OK);
  }
}

function runCodeCli(vscodeExecutablePath: string, args: readonly string[]): string {
  const invocation = resolveSafeVsCodeCliInvocation(vscodeExecutablePath);
  const result = spawnSync(invocation.command, [...invocation.prefixArgs, ...args], {
    encoding: "utf8",
    maxBuffer: 8 * 1024 * 1024,
    shell: false,
    windowsHide: true,
    env: {
      ...allowlistedQaRuntimeEnvironment(process.env),
      ...invocation.environment
    }
  });
  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    const stderr = safeErrorEvidence(new Error(String(result.stderr ?? "")));
    throw new Error(
      `VS Code CLI failed with ${String(result.status)} (stderr length=${stderr.length}, sha256=${stderr.sha256}).`
    );
  }
  return String(result.stdout ?? "");
}

async function main(): Promise<void> {
  const startedAt = new Date().toISOString();
  const vsixPath = resolve(requiredEnvironment("ALYSIS_PRODUCTION_VSIX_PATH"));
  const expectedTarget = requiredEnvironment("ALYSIS_EXPECTED_PLATFORM_TARGET");
  const outputPath = resolve(requiredEnvironment("ALYSIS_INSTALLED_LIVE_QA_OUTPUT"));
  const releaseTag = requiredEnvironment("ALYSIS_RELEASE_TAG");
  const sourceSha = requiredEnvironment("ALYSIS_SOURCE_SHA");
  const candidateRunId = requiredEnvironment("ALYSIS_CANDIDATE_RUN_ID");
  const workflowRunId = requiredEnvironment("ALYSIS_WORKFLOW_RUN_ID");
  const workflowRunAttempt = requiredEnvironment("ALYSIS_WORKFLOW_RUN_ATTEMPT");
  const productionDogfoodSha256 = requiredEnvironment(
    "ALYSIS_PRODUCTION_DOGFOOD_SHA256"
  );
  const provider = validateLiveQaEvidenceIdentifier(
    requiredEnvironment("ALYSIS_LIVE_QA_PROVIDER"),
    "Live QA provider",
    128
  );
  const model = validateLiveQaEvidenceIdentifier(
    requiredEnvironment("ALYSIS_LIVE_QA_MODEL"),
    "Live QA model",
    256
  );
  const baseUrl = requiredEnvironment("ALYSIS_LIVE_QA_BASE_URL");
  const approvedOrigin = requiredEnvironment("ALYSIS_LIVE_QA_APPROVED_ORIGIN");
  const providerOriginPolicySha256 = requiredEnvironment(
    "ALYSIS_LIVE_QA_ORIGIN_POLICY_SHA256"
  );
  const nativeSignatureCheck = requiredEnvironment("ALYSIS_NATIVE_SIGNATURE_CHECK");
  const releaseSignatureCheck = requiredEnvironment("ALYSIS_RELEASE_SIGNATURE_CHECK");
  const providerEndpoint = await validateLiveQaProviderEndpoint(baseUrl, approvedOrigin);
  let qaSecret = takeEnvironmentSecret(LIVE_API_KEY_ENVIRONMENT);
  restrictLiveQaEnvironment(process.env, qaSecret);

  if (
    !/^v\d+\.\d+\.\d+$/.test(releaseTag) ||
    !/^[0-9a-f]{40}$/.test(sourceSha) ||
    !/^[0-9a-f]{64}$/.test(productionDogfoodSha256) ||
    !/^[0-9a-f]{64}$/.test(providerOriginPolicySha256)
  ) {
    throw new Error("Release identity is invalid.");
  }
  if (
    !/^[1-9]\d*$/.test(candidateRunId) ||
    !/^[1-9]\d*$/.test(workflowRunId) ||
    !/^[1-9]\d*$/.test(workflowRunAttempt)
  ) {
    throw new Error("Workflow run identity is invalid.");
  }
  verifyFile(vsixPath, "Target VSIX");
  if (!basename(vsixPath).includes(expectedTarget)) {
    throw new Error("Target VSIX filename does not identify the expected platform.");
  }

  const tempRoot = mkdtempSync(join(tmpdir(), "alysis-installed-live-"));
  const workspacePath = join(tempRoot, "workspace");
  const userDataDir = join(tempRoot, "user-data");
  const extensionsDir = join(tempRoot, "extensions");
  const driverRoot = join(tempRoot, "release-driver");
  const marker = `ALYSIS_INSTALLED_LIVE_${randomBytes(12).toString("hex").toUpperCase()}`;
  const profileNonce = randomBytes(32).toString("hex");
  const resultPaths = {
    chat: join(tempRoot, "live-result-chat.json"),
    restart: join(tempRoot, "live-result-restart.json")
  } as const;
  try {
    for (const directory of [workspacePath, userDataDir, extensionsDir, driverRoot]) {
      mkdirSync(directory, { recursive: true });
    }
    if (readdirSync(extensionsDir).length !== 0) {
      throw new Error("Installed live QA extensions directory was not empty before install.");
    }
    const settingsDirectory = join(workspacePath, ".vscode");
    mkdirSync(settingsDirectory, { recursive: true });
    const workspaceSettings: Record<string, unknown> = {
      "alysis.cliPath": "",
      "alysis.autoStartBridge": false,
      "alysis.defaultMode": "readonly",
      "alysis.defaultModel": model,
      "alysis.baseUrl": providerEndpoint.baseUrl,
      "alysis.provider": provider
    };
    writeFileSync(
      join(settingsDirectory, "settings.json"),
      JSON.stringify(workspaceSettings, null, 2),
      { encoding: "utf8", mode: 0o600 }
    );

    const compiledDriverRoot = resolve(__dirname, "driver");
    copyFileSync(join(compiledDriverRoot, "extension.js"), join(driverRoot, "extension.js"));
    copyFileSync(join(compiledDriverRoot, "testRunner.js"), join(driverRoot, "testRunner.js"));
    writeFileSync(
      join(driverRoot, "package.json"),
      JSON.stringify(
        {
          name: "alysis-installed-live-provider-driver",
          displayName: "Alysis Code Installed Live Provider Driver",
          publisher: "alysisai",
          version: "0.0.1",
          private: true,
          engines: { vscode: "^1.90.0" },
          activationEvents: ["*"],
          main: "./extension.js"
        },
        null,
        2
      ),
      { encoding: "utf8", mode: 0o600 }
    );

    const requestedVersion = process.env.VSCODE_TEST_VERSION?.trim() || "1.90.0";
    const vscodeExecutablePath = process.env.VSCODE_TEST_EXECUTABLE_PATH?.trim()
      ? resolve(process.env.VSCODE_TEST_EXECUTABLE_PATH)
      : await downloadAndUnzipVSCode(requestedVersion);
    verifyExecutable(vscodeExecutablePath);
    runCodeCli(vscodeExecutablePath, [
      "--install-extension",
      vsixPath,
      "--force",
      "--extensions-dir",
      extensionsDir,
      "--user-data-dir",
      userDataDir
    ]);
    const installed = runCodeCli(vscodeExecutablePath, [
      "--list-extensions",
      "--show-versions",
      "--extensions-dir",
      extensionsDir,
      "--user-data-dir",
      userDataDir
    ])
      .split(/\r?\n/)
      .map((value) => value.trim().toLowerCase())
      .filter(Boolean);
    if (installed.length !== 1 || !/^alysisai\.vscode-alysis@\S+$/.test(installed[0] ?? "")) {
      throw new Error("Installed extension inventory was not exact.");
    }

    const phaseEvidence: Record<string, Record<string, unknown>> = {};
    for (const phase of ["chat", "restart"] as const) {
      delete process.env.ELECTRON_RUN_AS_NODE;
      const childEnvironment =
        phase === "chat"
          ? liveExtensionHostEnvironment(process.env, LIVE_API_KEY_ENVIRONMENT, qaSecret)
          : allowlistedQaRuntimeEnvironment(process.env, qaSecret);
      childEnvironment[QA_ONCE_ENVIRONMENT] = "1";
      Object.assign(childEnvironment, {
        ALYSIS_INSTALLED_LIVE_PHASE: phase,
        ALYSIS_INSTALLED_LIVE_RESULT: resultPaths[phase],
        ALYSIS_INSTALLED_LIVE_MARKER: marker,
        [PROFILE_NONCE_ENVIRONMENT]: profileNonce,
        ALYSIS_EXPECTED_EXTENSIONS_DIR: extensionsDir,
        ALYSIS_EXPECTED_PLATFORM_TARGET: expectedTarget,
        ALYSIS_NATIVE_SIGNATURE_CHECK: nativeSignatureCheck,
        ALYSIS_RELEASE_SIGNATURE_CHECK: releaseSignatureCheck
      });
      await runVsCodeExtensionHostTests({
        extensionDevelopmentPath: driverRoot,
        extensionTestsPath: join(driverRoot, "testRunner.js"),
        vscodeExecutablePath,
        version: requestedVersion,
        launchArgs: [
          workspacePath,
          "--user-data-dir",
          userDataDir,
          "--extensions-dir",
          extensionsDir,
          "--skip-welcome",
          "--skip-release-notes"
        ],
        extensionTestsEnv: childEnvironment
      });
      if (!existsSync(resultPaths[phase])) {
        throw new Error("Installed live QA driver did not write phase evidence.");
      }
      const evidence = JSON.parse(readFileSync(resultPaths[phase], "utf8")) as Record<
        string,
        unknown
      >;
      if (evidence.status !== "passed") {
        throw new Error("Installed live QA phase did not pass.");
      }
      if (JSON.stringify(evidence).includes(profileNonce)) {
        throw new Error("Installed live QA retained the profile witness nonce.");
      }
      phaseEvidence[phase] = evidence;
    }

    const chat = phaseEvidence.chat;
    const restart = phaseEvidence.restart;
    if (!chat || !restart) {
      throw new Error("Installed live QA did not complete both Extension Host phases.");
    }
    for (const field of [
      "extension_version",
      "managed_artifact_version",
      "managed_cli_version",
      "managed_protocol_version",
      "managed_cli_sha256",
      "release_tag",
      "source_repository",
      "source_sha",
      "platform_target"
    ]) {
      if (chat[field] !== restart[field]) {
        throw new Error(`Installed live QA restart changed immutable field ${field}.`);
      }
    }
    if (chat.release_tag !== releaseTag || chat.source_sha !== sourceSha) {
      throw new Error("Installed runtime release identity did not match the dispatched source.");
    }
    if (chat.profile_witness !== "written" || restart.profile_witness !== "observed") {
      throw new Error("Installed live QA did not prove persisted profile reuse across restart.");
    }
    assertSecretAbsentFromTrees([tempRoot], qaSecret);
    const response = chat.response as Record<string, unknown> | undefined;
    const summary = {
      schema_name: "installed-production-vsix-live-provider",
      schema_version: 2,
      status: "passed",
      mode: "installed-production-vsix-live-provider",
      started_at: startedAt,
      completed_at: new Date().toISOString(),
      release_tag: releaseTag,
      source_sha: sourceSha,
      candidate_run_id: candidateRunId,
      workflow_run_id: Number(workflowRunId),
      workflow_run_attempt: Number(workflowRunAttempt),
      production_dogfood_sha256: productionDogfoodSha256,
      platform_target: expectedTarget,
      vsix_sha256: createHash("sha256").update(readFileSync(vsixPath)).digest("hex"),
      extension_version: chat.extension_version,
      vscode_version: chat.vscode_version,
      host_platform: chat.host_platform,
      host_arch: chat.host_arch,
      extension_mode: chat.extension_mode,
      runtime_origin: chat.runtime_origin,
      runtime_production: chat.runtime_production,
      managed_artifact_version: chat.managed_artifact_version,
      managed_cli_version: chat.managed_cli_version,
      managed_protocol_version: chat.managed_protocol_version,
      managed_cli_sha256: chat.managed_cli_sha256,
      source_repository: chat.source_repository,
      provider,
      model,
      provider_origin: providerEndpoint.origin,
      provider_origin_policy_sha256: providerOriginPolicySha256,
      readonly_mode: chat.readonly_mode,
      response: {
        length: response?.length,
        sha256: response?.sha256,
        marker_matched: response?.marker_matched,
        elapsed_ms: response?.elapsed_ms
      },
      receipts: {
        clean_install: "passed",
        native_signature: "passed",
        release_attestations: "passed",
        managed_manifest_signature: "passed",
        managed_runtime_health: "passed",
        provider_secret_storage: "passed",
        provider_secret_cleared: "passed",
        provider_origin_policy: "passed",
        restart_profile_reused: "passed",
        restart_secret_absent: "passed",
        exact_secret_artifact_scan: "passed"
      },
      extension_host_launches: 2,
      retained_response_content: false
    };
    writeFileSync(outputPath, JSON.stringify(summary, null, 2) + "\n", {
      encoding: "utf8",
      mode: 0o600
    });
    assertSecretAbsentFromTrees([outputPath], qaSecret);
    process.stdout.write(
      `${JSON.stringify({ status: "passed", target: expectedTarget, evidence: outputPath })}\n`
    );
  } finally {
    try {
      if (qaSecret) {
        assertSecretAbsentFromTrees([tempRoot], qaSecret);
      }
    } finally {
      qaSecret = "";
      rmSync(tempRoot, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
    }
  }
}

main().catch((error) => {
  const evidence = safeErrorEvidence(error);
  process.stderr.write(
    `${JSON.stringify({ status: "failed", error: evidence })}\n`
  );
  process.exit(1);
});
