import assert from "node:assert/strict";
import { generateKeyPairSync, KeyObject, sign } from "node:crypto";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";

import type { ManagedCliSignedRecord } from "../src/runtime/ManagedCliRuntime";
import {
  createManagedCliReleaseSignatureVerifier,
  MANAGED_CLI_RELEASE_KEY_ID,
  MANAGED_CLI_RELEASE_PUBLIC_KEY_PEM,
  managedCliReleaseAttestation,
  type ManagedCliReleaseTrustKey
} from "../src/runtime/ManagedCliReleaseSecurity";

test("managed CLI embedded active trust anchor matches the packaged release public key", async () => {
  const packaged = await readFile(
    path.join(process.cwd(), "resources", "managed-cli-release-public.pem"),
    "utf8"
  );
  assert.equal(`${MANAGED_CLI_RELEASE_PUBLIC_KEY_PEM}\n`, packaged);
  assert.equal(MANAGED_CLI_RELEASE_KEY_ID, "alysis-release-2026-01");
});

test("runtime verifier matches the Python builder's canonical v3 contract fixture", async () => {
  const fixture = JSON.parse(await readFile(
    path.join(process.cwd(), "test", "fixtures", "managedCliAttestationV3.json"),
    "utf8"
  )) as {
    record: ManagedCliSignedRecord;
    publicKeyPem: string;
    signature: string;
  };
  const verifier = createManagedCliReleaseSignatureVerifier([{
    keyId: "contract-fixture",
    publicKeyPem: fixture.publicKeyPem,
    status: "active"
  }]);
  assert.equal(await verifier({
    filePath: "/unused",
    signature: fixture.signature,
    record: fixture.record,
    purpose: "install"
  }), true);
});

test("canonical v3 verifier binds every release, policy, provenance, and artifact field", async () => {
  const { privateKey, publicKey } = generateKeyPairSync("ec", { namedCurve: "prime256v1" });
  const original = record("active-key");
  const signature = releaseSignature(privateKey, original);
  const verifier = createManagedCliReleaseSignatureVerifier([trustKey("active-key", publicKey, "active")]);
  const input = { filePath: "/unused", signature, record: original, purpose: "install" as const };

  assert.equal(await verifier(input), true);
  const mutations: Array<(value: ManagedCliSignedRecord) => void> = [
    (value) => { value.release.tag = "v1.2.4"; },
    (value) => { value.release.sourceRepository = "https://example.com/repo"; },
    (value) => { value.release.sourceCommit = "b".repeat(40); },
    (value) => { value.artifactVersion = "cli-1.2.4"; },
    (value) => { value.cliVersion = "1.2.4"; },
    (value) => { value.compatibility.extension.max = "9.9.9"; },
    (value) => { value.provenance.builderId = "https://example.com/builder"; },
    (value) => { value.provenance.issuer = "https://example.com/issuer"; },
    (value) => { value.signingKeyId = "other-key"; },
    (value) => { value.artifact.target = "linux-arm64"; },
    (value) => { value.artifact.executable = "other"; },
    (value) => { value.artifact.url = "https://example.com/other"; },
    (value) => { value.artifact.size += 1; },
    (value) => { value.artifact.sha256 = "c".repeat(64); },
    (value) => { value.artifact.sbomSha256 = "d".repeat(64); },
    (value) => { value.artifact.nativeSignature.evidenceSha256 = "f".repeat(64); },
    (value) => { value.artifact.nativeSignature.policy = "authenticode"; },
    (value) => { value.artifact.nativeSignature.signerIdentity = "other"; }
  ];
  for (const mutate of mutations) {
    const tampered = structuredClone(original);
    mutate(tampered);
    assert.equal(await verifier({ ...input, record: tampered }), false);
  }
});

test("active, retiring, revoked, and unknown trust keys have explicit lifecycle semantics", async () => {
  const active = generateKeyPairSync("ec", { namedCurve: "prime256v1" });
  const retiring = generateKeyPairSync("ec", { namedCurve: "prime256v1" });
  const revoked = generateKeyPairSync("ec", { namedCurve: "prime256v1" });
  const trustSet: ManagedCliReleaseTrustKey[] = [
    trustKey("active", active.publicKey, "active"),
    trustKey("retiring", retiring.publicKey, "retiring"),
    trustKey("revoked", revoked.publicKey, "revoked")
  ];
  const verifier = createManagedCliReleaseSignatureVerifier(trustSet);

  for (const [keyId, privateKey, install, installed] of [
    ["active", active.privateKey, true, true],
    ["retiring", retiring.privateKey, false, true],
    ["revoked", revoked.privateKey, false, false]
  ] as const) {
    const signedRecord = record(keyId);
    const signature = releaseSignature(privateKey, signedRecord);
    assert.equal(await verifier({ filePath: "/unused", signature, record: signedRecord, purpose: "install" }), install);
    assert.equal(await verifier({ filePath: "/unused", signature, record: signedRecord, purpose: "installed" }), installed);
  }

  const unknown = record("unknown");
  assert.equal(await verifier({
    filePath: "/unused",
    signature: releaseSignature(active.privateKey, unknown),
    record: unknown,
    purpose: "installed"
  }), false);
});

test("trust-set and signature parsing fail closed", async () => {
  const { privateKey, publicKey } = generateKeyPairSync("ec", { namedCurve: "prime256v1" });
  const key = trustKey("active", publicKey, "active");
  assert.throws(() => createManagedCliReleaseSignatureVerifier([]), /trust set is empty/i);
  assert.throws(() => createManagedCliReleaseSignatureVerifier([key, key]), /duplicate key id/i);

  const verifier = createManagedCliReleaseSignatureVerifier([key]);
  const base = { filePath: "/unused", record: record("active"), purpose: "install" as const };
  assert.equal(await verifier({ ...base, signature: "not-base64" }), false);
  assert.equal(await verifier({ ...base, signature: "A".repeat(400) }), false);
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(
    () => verifier({ ...base, signature: releaseSignature(privateKey, base.record), signal: controller.signal }),
    /cancelled/i
  );
});

function record(signingKeyId: string): ManagedCliSignedRecord {
  return {
    schemaVersion: 3,
    release: {
      tag: "v1.2.3",
      sourceRepository: "https://github.com/AlysisAi/AlysisCode",
      sourceCommit: "a".repeat(40)
    },
    artifactVersion: "cli-1.2.3",
    cliVersion: "1.2.3",
    compatibility: {
      extension: { min: "0.1.1", max: "0.1.1" },
      protocol: { min: "1", max: "1" },
      cli: { min: "1.2.3", max: "1.2.3" }
    },
    signingKeyId,
    provenance: {
      issuer: "https://token.actions.githubusercontent.com",
      builderId: "https://github.com/AlysisAi/AlysisCode/.github/workflows/managed-cli-vsix-release.yml@refs/tags/v1.2.3"
    },
    artifact: {
      target: "linux-x64",
      url: "https://github.com/AlysisAi/AlysisCode/releases/download/v1.2.3/alysis-linux-x64",
      sha256: "a".repeat(64),
      size: 123,
      executable: "alysis-linux-x64",
      sbomSha256: "b".repeat(64),
      nativeSignature: {
        evidenceSha256: "e".repeat(64),
        policy: "not-applicable",
        signerIdentity: "not-applicable"
      }
    }
  };
}

function trustKey(
  keyId: string,
  publicKey: KeyObject,
  status: ManagedCliReleaseTrustKey["status"]
): ManagedCliReleaseTrustKey {
  return {
    keyId,
    publicKeyPem: publicKey.export({ type: "spki", format: "pem" }).toString(),
    status
  };
}

function releaseSignature(privateKey: KeyObject, signedRecord: ManagedCliSignedRecord): string {
  return sign("sha256", managedCliReleaseAttestation(signedRecord), privateKey).toString("base64");
}
