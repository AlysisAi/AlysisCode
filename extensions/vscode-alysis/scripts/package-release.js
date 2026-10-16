#!/usr/bin/env node
"use strict";

// Release packages must contain a runtime accepted by the production verifier.
// Component-only CI and local development use package:dev-vsix instead.
const { spawnSync } = require("node:child_process");
const { createHash } = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { parseManagedCliManifest, managedCliSignedRecord } = require("../out/src/runtime/ManagedCliRuntime");
const { createManagedCliReleaseSignatureVerifier, MANAGED_CLI_RELEASE_PUBLIC_KEY_PEM } = require("../out/src/runtime/ManagedCliReleaseSecurity");
const { PROTOCOL_VERSION } = require("../out/src/client/AlysisProtocol");

const EXTENSION_ROOT = path.resolve(__dirname, "..");
const SOURCE = "https://github.com/AlysisAi/AlysisCode";

function parseArgs(argv) {
  const passthrough = [];
  let target = process.env.ALYSIS_VSIX_TARGET ?? "";
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--target") {
      target = argv[++index] ?? "";
    } else if (arg.startsWith("--target=")) {
      target = arg.slice("--target=".length);
    } else if (arg === "--pre-release") {
      passthrough.push(arg);
    } else if (arg === "--out" && argv[index + 1]) {
      passthrough.push(arg, argv[++index]);
    } else {
      throw new Error(`Unsupported release packaging argument: ${arg}`);
    }
  }
  return { target: target.trim(), passthrough };
}

function readRegularFile(root, name, limit) {
  const file = path.join(root, name);
  const info = fs.lstatSync(file);
  if (!info.isFile() || info.size <= 0 || (limit && info.size > limit) ||
      fs.realpathSync(file) !== path.join(fs.realpathSync(root), name)) {
    throw new Error(`Staged ${name} must be a nonempty regular file inside its bundle.`);
  }
  return fs.readFileSync(file);
}

function inRange(version, range) {
  const compare = (a, b) => {
    const left = a.split(".").map(Number);
    const right = b.split(".").map(Number);
    for (let i = 0; i < Math.max(left.length, right.length); i += 1) {
      const difference = (left[i] ?? 0) - (right[i] ?? 0);
      if (difference) return difference;
    }
    return 0;
  };
  return compare(version, range.min) >= 0 && compare(version, range.max) <= 0;
}

async function validateBundle(root, target, verifier = createManagedCliReleaseSignatureVerifier()) {
  if (!/^(win32|darwin|linux)-(x64|arm64)$/.test(target)) {
    throw new Error("Declare one supported VSIX target with --target or ALYSIS_VSIX_TARGET.");
  }
  const bundleRoot = path.join(root, "resources", "managed-cli");
  if (!fs.existsSync(path.join(bundleRoot, "manifest.json"))) {
    throw new Error("No signed managed CLI bundle is staged at resources/managed-cli/. Use package:dev-vsix for component builds.");
  }
  if (fs.lstatSync(bundleRoot).isSymbolicLink()) throw new Error("Bundle directory must not be a link.");
  const manifest = parseManagedCliManifest(JSON.parse(readRegularFile(bundleRoot, "manifest.json", 1024 * 1024)), {
    trustedDownloadHosts: ["github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"]
  });
  const artifact = manifest.artifacts.find((entry) => entry.target === target);
  if (!artifact) throw new Error(`Manifest has no artifact for ${target}.`);
  if (!artifact.signature) throw new Error("The target artifact carries no release signature.");
  const expectedExecutable = `alysis-${target}${target.startsWith("win32-") ? ".exe" : ""}`;
  if (artifact.executable !== expectedExecutable ||
      manifest.release.sourceRepository !== SOURCE ||
      manifest.provenance.issuer !== "https://token.actions.githubusercontent.com" ||
      manifest.provenance.builderId !== `${SOURCE}/.github/workflows/managed-cli-vsix-release.yml@refs/tags/${manifest.release.tag}` ||
      artifact.url !== `${SOURCE}/releases/download/${manifest.release.tag}/${expectedExecutable}`) {
    throw new Error("Runtime release identity does not match the pinned production policy.");
  }
  const packageManifest = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8"));
  if (!inRange(packageManifest.version, manifest.compatibility.extension) ||
      !inRange(PROTOCOL_VERSION, manifest.compatibility.protocol)) {
    throw new Error("Runtime is incompatible with this extension or IDE protocol.");
  }
  const names = fs.readdirSync(bundleRoot).sort();
  if (JSON.stringify(names) !== JSON.stringify([artifact.executable, "manifest.json"].sort())) {
    throw new Error("Bundle must contain exactly the manifest and its single target runtime.");
  }
  const bytes = readRegularFile(bundleRoot, artifact.executable);
  if (bytes.length !== artifact.size || createHash("sha256").update(bytes).digest("hex") !== artifact.sha256) {
    throw new Error("Runtime bytes do not match the signed size and SHA-256.");
  }
  const filePath = path.join(bundleRoot, artifact.executable);
  if (!await verifier({ filePath, signature: artifact.signature, record: managedCliSignedRecord(manifest, artifact), purpose: "install" })) {
    throw new Error("Runtime artifact release signature is invalid or untrusted.");
  }
  return { ...artifact, sourceCommit: manifest.release.sourceCommit };
}

function validateListing(root, preRelease) {
  const readme = fs.readFileSync(path.join(root, "README.md"), "utf8");
  if (/PUBLISH BLOCKER|not published to the Marketplace|\bscaffold\b/i.test(readme)) {
    throw new Error("README.md still contains capture placeholders or unfinished listing copy.");
  }
  if (!/!\[[^\]]*\]\((?:https:\/\/|resources\/)/.test(readme)) {
    throw new Error("README.md must include product screenshots.");
  }
  for (const match of readme.matchAll(/!\[[^\]]*\]\((resources\/[^)]+)\)/g)) {
    const relative = match[1];
    if (!/^resources\/screenshots\/[a-z-]+\.png$/.test(relative)) throw new Error("Unsafe listing image path.");
    const bytes = readRegularFile(path.join(root, "resources", "screenshots"), path.basename(relative));
    if (!bytes.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))) {
      throw new Error(`Listing image is not a PNG: ${relative}`);
    }
  }
  const manifest = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8"));
  const version = /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/.exec(manifest.version);
  if (!version || (Number(version[2]) % 2 === 1) !== preRelease) {
    throw new Error("Use an odd minor version with --pre-release for beta, or an even minor version for stable.");
  }
  if (!preRelease && manifest.preview !== false) throw new Error("Stable packages require preview: false.");
  const publicKey = fs.readFileSync(path.join(root, "resources", "managed-cli-release-public.pem"), "utf8");
  if (publicKey.trim() !== MANAGED_CLI_RELEASE_PUBLIC_KEY_PEM.trim()) throw new Error("Packaged release public key differs from the embedded trust anchor.");
}

async function main() {
  const { target, passthrough } = parseArgs(process.argv.slice(2));
  validateListing(EXTENSION_ROOT, passthrough.includes("--pre-release"));
  const artifact = await validateBundle(EXTENSION_ROOT, target);
  process.stdout.write(`Release signature and runtime bytes verified for ${target}: ${artifact.executable}\n`);
  // Run the installed JS entry directly. .cmd needs a shell on Windows; output
  // paths must never pass through a shell.
  const vsceRoot = path.dirname(require.resolve("@vscode/vsce/package.json"));
  const vscePackage = require(path.join(vsceRoot, "package.json"));
  const result = spawnSync(process.execPath, [path.join(vsceRoot, vscePackage.bin.vsce),
    "package", "--no-dependencies", "--target", target,
    "--baseContentUrl", `${SOURCE}/blob/${artifact.sourceCommit}/extensions/vscode-alysis`,
    "--baseImagesUrl", `https://raw.githubusercontent.com/AlysisAi/AlysisCode/${artifact.sourceCommit}/extensions/vscode-alysis`,
    ...passthrough], { cwd: EXTENSION_ROOT, stdio: "inherit" });
  if (result.error) throw result.error;
  process.exitCode = result.status ?? 1;
}

module.exports = { validateBundle, validateListing, parseArgs };
if (require.main === module) {
  main().catch((error) => {
    process.stderr.write(`Release packaging refused: ${error.message}\n`);
    process.exit(1);
  });
}
