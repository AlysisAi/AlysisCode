import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

const { listFiles, PackageManager } = require("@vscode/vsce") as {
  listFiles(options: { cwd: string; packageManager: number }): Promise<string[]>;
  PackageManager: { None: number };
};

test("the real VSIX packager includes runtime assets and excludes local QA and credential files", async (t) => {
  const root = mkdtempSync(path.join(tmpdir(), "alysis-vsix-inventory-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const put = (name: string, content = "fixture") => {
    const target = path.join(root, name);
    mkdirSync(path.dirname(target), { recursive: true });
    writeFileSync(target, content);
  };
  put("package.json", JSON.stringify({ name: "inventory-fixture", publisher: "alysisai", version: "0.3.0", engines: { vscode: "^1.90.0" } }));
  put(".vscodeignore", readFileSync(path.resolve(__dirname, "../../.vscodeignore"), "utf8"));
  const included = [
    "README.md", "LICENSE", "dist/extension.js", "media/startView.js", "media/THIRD_PARTY_NOTICES.txt",
    "media/walkthrough/install-cli.md", "resources/icon.png", "resources/screenshots/setup.png",
    "resources/managed-cli-release-public.pem", "resources/managed-cli/manifest.json",
    ...["win32-x64.exe", "win32-arm64.exe", "linux-x64", "linux-arm64", "darwin-x64", "darwin-arm64"]
      .map(target => `resources/managed-cli/alysis-${target}`)
  ];
  const excluded = [
    ".qa-results/transcript.log", "qa_reports/provider.json", ".env", "private-key.pem",
    "resources/managed-cli/signing.key", "resources/managed-cli/provider.env",
    "dist/extension.js.map", "media/local-debug.json", "resources/screenshots/session.json",
    "out/extension.js", "src/extension.ts", "notes.txt", "unexpected.js"
  ];
  for (const name of [...included, ...excluded]) put(name);
  const actual = (await listFiles({ cwd: root, packageManager: PackageManager.None })).map(name => name.replaceAll("\\", "/"));
  assert.deepEqual(actual.sort(), ["package.json", ...included].sort());
});
