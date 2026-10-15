import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";

const computedFingerprintKeys = new Set([
  "fingerprint",
  "contractFingerprint",
  "contract_fingerprint",
  "full_content_fingerprint",
]);

function compareCodePoints(left, right) {
  const a = Array.from(left, (character) => character.codePointAt(0));
  const b = Array.from(right, (character) => character.codePointAt(0));
  for (let index = 0; index < Math.min(a.length, b.length); index += 1) {
    if (a[index] !== b[index]) return a[index] - b[index];
  }
  return a.length - b.length;
}

function normalizeString(value) {
  for (let index = 0; index < value.length; index += 1) {
    const unit = value.charCodeAt(index);
    if (unit >= 0xd800 && unit <= 0xdbff) {
      const next = value.charCodeAt(index + 1);
      if (!(next >= 0xdc00 && next <= 0xdfff)) throw new Error("unpaired_surrogate");
      index += 1;
    } else if (unit >= 0xdc00 && unit <= 0xdfff) {
      throw new Error("unpaired_surrogate");
    }
  }
  return value.normalize("NFC");
}

function normalize(value) {
  if (value === null || typeof value === "boolean") return value;
  if (typeof value === "string") return normalizeString(value);
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value)) throw new Error("unsafe_integer");
    return Object.is(value, -0) ? 0 : value;
  }
  if (Array.isArray(value)) return value.map(normalize);
  if (typeof value === "object") {
    const normalized = new Map();
    for (const [rawKey, item] of Object.entries(value)) {
      if (computedFingerprintKeys.has(rawKey)) continue;
      const key = normalizeString(rawKey);
      if (normalized.has(key)) throw new Error("duplicate_nfc_key");
      normalized.set(key, normalize(item));
    }
    return Object.fromEntries([...normalized].sort(([a], [b]) => compareCodePoints(a, b)));
  }
  throw new Error("unsupported_json_value");
}

function canonical(value) {
  return JSON.stringify(normalize(value));
}

const corpus = JSON.parse(readFileSync(process.argv[2], "utf8"));
for (const vector of corpus.vectors) {
  const text = canonical(vector.input);
  const digest = createHash("sha256").update(text, "utf8").digest("hex");
  if (text !== vector.canonical || digest !== vector.sha256) {
    throw new Error(`golden mismatch: ${vector.id}`);
  }
}
for (const vector of corpus.rejectionVectors) {
  let rejected = false;
  try {
    canonical(vector.input);
  } catch (error) {
    rejected = error instanceof Error && error.message === vector.error;
  }
  if (!rejected) throw new Error(`rejection mismatch: ${vector.id}`);
}
