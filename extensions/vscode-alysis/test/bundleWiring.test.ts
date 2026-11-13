import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, resolve } from "node:path";
import test from "node:test";

const packageRoot = resolve(__dirname, "../..");
const packageJson = JSON.parse(readFileSync(resolve(packageRoot, "package.json"), "utf8")) as {
  main: string;
  scripts: Record<string, string>;
  dependencies?: Record<string, string>;
  devDependencies: Record<string, string>;
};

test("VS Code loads one bundled entry file, not the tsc module tree", () => {
  assert.equal(packageJson.main, "./dist/extension.js");
  assert.match(packageJson.scripts.bundle, /scripts\/bundle\.js/);
  assert.match(packageJson.scripts["bundle:production"], /scripts\/bundle\.js --production/);
  // vsce runs vscode:prepublish itself, so a VSIX built by any path (package-release.js from CI,
  // package:dev-vsix locally, a bare vsce package) always carries a fresh production bundle.
  assert.equal(packageJson.scripts["vscode:prepublish"], "npm run bundle:production");
  assert.match(packageJson.scripts.clean, /'dist'/);
  // clean wipes dist/, so compile must rebuild it: every test:* script that launches VS Code
  // runs `npm run compile && ...` and would otherwise start an extension with no entry file.
  assert.match(packageJson.scripts.compile, /tsc -p \.\/ && npm run bundle$/);
  for (const [name, script] of Object.entries(packageJson.scripts)) {
    if (/^test:/.test(name) && /runTest\.js|test-cursor\.js/.test(script)) {
      assert.match(script, /^npm run compile && /, `${name} must compile (and therefore bundle) first`);
    }
  }
});

test("the VSIX ships dist/ and never the unbundled out/ tree", () => {
  const ignore = readFileSync(resolve(packageRoot, ".vscodeignore"), "utf8")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  assert.ok(ignore.includes("out/**"), "out/** must be excluded from the VSIX");
  assert.ok(ignore.includes("**/*.map"), "sourcemaps must be excluded from the VSIX");
  assert.equal(ignore.some((line) => /^dist\b/.test(line)), false, "dist/ must not be excluded");
});

test("esbuild is a build-time tool only; the runtime dependency count stays at zero", () => {
  assert.match(packageJson.devDependencies.esbuild, /^\^0\.\d+\.\d+$/);
  assert.deepEqual(Object.keys(packageJson.dependencies ?? {}), []);
});

test("extension sources never locate assets through __dirname or __filename", () => {
  // In the tsc layout __dirname is out/src/<dir>; in the bundle it is dist/. Only
  // context.extensionUri is stable across both, so a __dirname-relative asset path is a
  // bundling regression that unit tests on the tsc output would never catch.
  const offenders: string[] = [];
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir)) {
      const full = join(dir, entry);
      if (statSync(full).isDirectory()) {
        walk(full);
      } else if (entry.endsWith(".ts") && !entry.endsWith(".d.ts")) {
        // Comments may name the identifiers to explain the rule; only code is checked.
        const code = readFileSync(full, "utf8")
          .replace(/\/\*[\s\S]*?\*\//g, "")
          .replace(/(^|[^:"'`])\/\/.*$/gm, "$1");
        if (/\b__(dirname|filename)\b/.test(code)) {
          offenders.push(full.slice(packageRoot.length + 1));
        }
      }
    }
  };
  walk(resolve(packageRoot, "src"));
  assert.deepEqual(offenders, []);
});

test("the bundle script targets the extension host runtime and keeps vscode external", () => {
  const script = readFileSync(resolve(packageRoot, "scripts/bundle.js"), "utf8");
  assert.match(script, /platform:\s*"node"/);
  assert.match(script, /format:\s*"cjs"/);
  assert.match(script, /external:\s*\["vscode"\]/);
  assert.match(script, /entryPoints:\s*\[path\.join\(root, "src", "extension\.ts"\)\]/);
  assert.match(script, /const outfile = path\.join\(root, "dist", "extension\.js"\)/);
});
