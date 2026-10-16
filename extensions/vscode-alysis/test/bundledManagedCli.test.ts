import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { installBundledManagedCli } from "../src/runtime/BundledManagedCli";
import {
  InstalledRuntime,
  ManagedCliError,
  ManagedCliManifest,
  resolveManagedCliTarget
} from "../src/runtime/ManagedCliRuntime";

test("bundled managed CLI loader selects the current platform and delegates verified paths", async (t) => {
  const root = await temporaryDirectory(t);
  const content = Buffer.from("managed-cli");
  const target = resolveManagedCliTarget();
  const executable = target.startsWith("win32-") ? "alysis.exe" : "alysis";
  const manifest = releaseManifest(target, executable, content);
  await writeFile(path.join(root, executable), content);
  await writeFile(path.join(root, "manifest.json"), JSON.stringify(manifest));
  let observed: { manifest: ManagedCliManifest; executablePath: string } | undefined;
  const expected = installed(path.join(root, executable), target);

  const result = await installBundledManagedCli(
    root,
    {
      installBundled: async (parsed, executablePath) => {
        observed = { manifest: parsed, executablePath };
        return expected;
      }
    },
    ["github.com"]
  );

  assert.equal(result, expected);
  assert.equal(observed?.manifest.artifacts[0].target, target);
  assert.equal(observed?.executablePath, path.join(root, executable));
});

test("bundled managed CLI loader fails closed for malformed, oversized, and untrusted manifests", async (t) => {
  const root = await temporaryDirectory(t);
  const never = { installBundled: async () => { throw new Error("must not install"); } };

  await writeFile(path.join(root, "manifest.json"), "{not-json}");
  await assert.rejects(
    () => installBundledManagedCli(root, never, ["github.com"]),
    hasCode("BUNDLED_RUNTIME_INVALID")
  );

  await writeFile(path.join(root, "manifest.json"), Buffer.alloc(1024 * 1024 + 1));
  await assert.rejects(
    () => installBundledManagedCli(root, never, ["github.com"]),
    hasCode("BUNDLED_RUNTIME_INVALID")
  );

  const target = resolveManagedCliTarget();
  const executable = target.startsWith("win32-") ? "alysis.exe" : "alysis";
  await writeFile(path.join(root, "manifest.json"), JSON.stringify(releaseManifest(target, executable, Buffer.from("x"), "evil.example")));
  await assert.rejects(
    () => installBundledManagedCli(root, never, ["github.com"]),
    hasCode("UNSAFE_URL")
  );
});

function releaseManifest(
  target: ReturnType<typeof resolveManagedCliTarget>,
  executable: string,
  content: Buffer,
  host = "github.com"
): ManagedCliManifest {
  const nativeSignature = target.startsWith("win32-")
    ? { evidenceSha256: "c".repeat(64), policy: "authenticode" as const, signerIdentity: `sha256:${"d".repeat(64)}` }
    : target.startsWith("darwin-")
      ? {
        evidenceSha256: "c".repeat(64),
        policy: "apple-developer-id-notarized" as const,
        signerIdentity: "Developer ID Application: Test (TEAMID1234)"
      }
      : { evidenceSha256: "c".repeat(64), policy: "not-applicable" as const, signerIdentity: "not-applicable" };
  return {
    schemaVersion: 3,
    release: {
      tag: "v1.0.0",
      sourceRepository: "https://github.com/AlysisAi/AlysisCode",
      sourceCommit: "a".repeat(40)
    },
    artifactVersion: "runtime-1.0.0",
    cliVersion: "1.0.0",
    compatibility: {
      extension: { min: "0.1.0", max: "0.2.0" },
      protocol: { min: "1", max: "1" },
      cli: { min: "1.0.0", max: "1.0.0" }
    },
    signingKeyId: "test-release-key",
    provenance: {
      issuer: "https://token.actions.githubusercontent.com",
      builderId: "https://github.com/AlysisAi/AlysisCode/.github/workflows/managed-cli-vsix-release.yml@refs/tags/v1.0.0"
    },
    artifacts: [{
      target,
      url: `https://${host}/AlysisAi/AlysisCode/releases/download/v1.0.0/${target}`,
      sha256: createHash("sha256").update(content).digest("hex"),
      size: content.length,
      executable,
      sbomSha256: "b".repeat(64),
      nativeSignature,
      signature: "A".repeat(88)
    }]
  };
}

function installed(
  executablePath: string,
  target: ReturnType<typeof resolveManagedCliTarget>
): InstalledRuntime {
  return {
    artifactVersion: "runtime-1.0.0",
    cliVersion: "1.0.0",
    protocolVersion: "1",
    target,
    executablePath,
    sha256: "a".repeat(64),
    size: 1,
    signatureVerified: true
  };
}

function hasCode(code: string): (error: unknown) => boolean {
  return (error) => error instanceof ManagedCliError && error.code === code;
}

async function temporaryDirectory(t: { after(callback: () => Promise<void>): void }): Promise<string> {
  const root = await realpath(await mkdtemp(path.join(tmpdir(), "alysis-bundle-test-")));
  await mkdir(root, { recursive: true });
  t.after(async () => rm(root, { recursive: true, force: true }));
  return root;
}
