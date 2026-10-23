import assert from "node:assert/strict";
import { createHash, generateKeyPairSync, sign } from "node:crypto";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { createManagedCliReleaseSignatureVerifier, managedCliReleaseAttestation } from "../src/runtime/ManagedCliReleaseSecurity";
import { ManagedCliManifest, managedCliSignedRecord, SignatureVerifier } from "../src/runtime/ManagedCliRuntime";

const { validateBundle, parseArgs } = require("../../scripts/package-release.js") as {
  validateBundle(root: string, target: string, verifier?: SignatureVerifier): Promise<unknown>;
  parseArgs(args: string[]): { target: string; passthrough: string[] };
};

function fixture(target: string) {
  const root = mkdtempSync(path.join(tmpdir(), "alysis-release-guard-"));
  const bundle = path.join(root, "resources", "managed-cli");
  mkdirSync(bundle, { recursive: true });
  const bytes = Buffer.from("signed-runtime-fixture");
  const key = generateKeyPairSync("ec", { namedCurve: "prime256v1" });
  const record = JSON.parse(readFileSync(path.resolve(__dirname, "../../test/fixtures/managedCliAttestationV3.json"), "utf8")).record;
  const source = "https://github.com/AlysisAi/AlysisCode";
  record.release.sourceRepository = source;
  record.provenance.builderId = `${source}/.github/workflows/managed-cli-vsix-release.yml@refs/tags/v1.2.3`;
  record.artifact.target = target;
  record.artifact.executable = `alysis-${target}${target.startsWith("win32-") ? ".exe" : ""}`;
  record.artifact.url = `${source}/releases/download/v1.2.3/${record.artifact.executable}`;
  record.artifact.sha256 = createHash("sha256").update(bytes).digest("hex");
  record.artifact.size = bytes.length;
  if (target.startsWith("win32-")) {
    record.artifact.nativeSignature.policy = "authenticode";
    record.artifact.nativeSignature.signerIdentity = `sha256:${"a".repeat(64)}`;
  } else if (target.startsWith("darwin-")) {
    record.artifact.nativeSignature.policy = "apple-developer-id-notarized";
    record.artifact.nativeSignature.signerIdentity = "Developer ID Application: Alysis AI (ABCDEFGHIJ)";
  }
  const { artifact, ...rest } = record;
  const manifest: ManagedCliManifest = { ...rest, artifacts: [artifact] };
  artifact.signature = sign("sha256", managedCliReleaseAttestation(managedCliSignedRecord(manifest, artifact)), key.privateKey).toString("base64");
  writeFileSync(path.join(root, "package.json"), JSON.stringify({ version: "0.1.1", preview: false }));
  writeFileSync(path.join(bundle, artifact.executable), bytes);
  const save = () => writeFileSync(path.join(bundle, "manifest.json"), JSON.stringify(manifest));
  save();
  const verifier = createManagedCliReleaseSignatureVerifier([{
    keyId: record.signingKeyId, status: "active",
    publicKeyPem: key.publicKey.export({ type: "spki", format: "pem" }).toString()
  }]);
  return { root, bundle, manifest, save, verifier, executable: path.join(bundle, artifact.executable) };
}

for (const target of ["win32-x64", "win32-arm64", "darwin-x64", "darwin-arm64", "linux-x64", "linux-arm64"]) {
  test(`release guard accepts real artifact signatures for ${target}`, async (t) => {
    const f = fixture(target);
    t.after(() => rmSync(f.root, { recursive: true, force: true }));
    await validateBundle(f.root, target, f.verifier);
    // The CLI never accepts a caller-supplied trust key: fixtures fail against production trust.
    await assert.rejects(validateBundle(f.root, target), /invalid or untrusted/);
  });
}

test("release guard rejects byte substitution, missing signatures, and extra runtimes", async (t) => {
  const f = fixture("linux-x64");
  t.after(() => rmSync(f.root, { recursive: true, force: true }));
  writeFileSync(f.executable, "tampered-runtime-byte");
  await assert.rejects(validateBundle(f.root, "linux-x64", f.verifier), /SHA-256/);
  delete f.manifest.artifacts[0].signature;
  f.save();
  await assert.rejects(validateBundle(f.root, "linux-x64", f.verifier), /no release signature/);
});

test("release guard rejects incompatible bundles, wrong targets, and duplicate artifacts", async (t) => {
  const f = fixture("linux-x64");
  t.after(() => rmSync(f.root, { recursive: true, force: true }));
  await assert.rejects(validateBundle(f.root, "linux-arm64", f.verifier), /no artifact/);
  writeFileSync(path.join(f.root, "package.json"), JSON.stringify({ version: "9.0.0" }));
  await assert.rejects(validateBundle(f.root, "linux-x64", f.verifier), /incompatible/);
  writeFileSync(path.join(f.root, "package.json"), JSON.stringify({ version: "0.1.1" }));
  writeFileSync(path.join(f.bundle, "alysis-linux-arm64"), "extra");
  await assert.rejects(validateBundle(f.root, "linux-x64", f.verifier), /single target runtime/);
  f.manifest.artifacts.push(f.manifest.artifacts[0]);
  f.save();
  await assert.rejects(validateBundle(f.root, "linux-x64", f.verifier), /duplicate target/);
});

test("release arguments preserve output paths and reject bypass flags or version mutation", () => {
  assert.deepEqual(parseArgs(["--target", "win32-x64", "--out", "folder with spaces/a.vsix"]), {
    target: "win32-x64", passthrough: ["--out", "folder with spaces/a.vsix"]
  });
  for (const value of ["--no-verify", "--allow-missing-repository", "1.0.0", "--out"]) {
    assert.throws(() => parseArgs([value]), /Unsupported/);
  }
});
