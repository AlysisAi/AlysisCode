import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { downloadDirToExecutablePath } from "@vscode/test-electron/out/util";

for (const executable of ["Electron", "Code"]) {
  test(`VS Code downloads resolve the macOS ${executable} executable`, () => {
    const root = mkdtempSync(join(tmpdir(), "alysis-vscode-download-"));
    try {
      const contents = join(root, "Visual Studio Code.app", "Contents");
      mkdirSync(join(contents, "MacOS"), { recursive: true });
      writeFileSync(join(contents, "MacOS", executable), "fixture executable");
      writeFileSync(
        join(contents, "Info.plist"),
        `<plist><dict><key>CFBundleExecutable</key><string>${executable}</string></dict></plist>`
      );
      for (const platform of ["darwin", "darwin-arm64"] as const) {
        assert.equal(
          downloadDirToExecutablePath(root, platform),
          join(contents, "MacOS", executable)
        );
      }
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
}
