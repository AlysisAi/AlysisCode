import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { copyTestDependencyTree } from "./integration/copyTestDependencyTree";

test("packaged test dependencies retain nested versions and hoisted transitive dependencies", () => {
  const root = mkdtempSync(join(tmpdir(), "alysis-test-deps-"));
  try {
    const source = join(root, "source", "node_modules");
    const target = join(root, "target", "node_modules");
    function fixture(path: string, name: string, dependencies: Record<string, string>, body: string): void {
      const dir = join(source, path);
      mkdirSync(dir, { recursive: true });
      writeFileSync(join(dir, "package.json"), JSON.stringify({ name, version: "1.0.0", main: "index.js", dependencies }));
      writeFileSync(join(dir, "index.js"), body);
    }
    fixture("runner", "runner", { helper: "*", shared: "*" }, "module.exports = [require('helper'), require('shared')];");
    fixture("runner/node_modules/helper", "helper", { shared: "*", leaf: "*" }, "module.exports = [require('shared'), require('leaf')];");
    fixture("runner/node_modules/helper/node_modules/shared", "shared", {}, "module.exports = 'nested';");
    fixture("shared", "shared", {}, "module.exports = 'root';");
    fixture("leaf", "leaf", {}, "module.exports = 'hoisted';");
    copyTestDependencyTree("runner", source, target);
    const requireStaged = createRequire(join(root, "target", "entry.js"));
    assert.deepEqual(requireStaged("runner"), [["nested", "hoisted"], "root"]);
    assert.throws(() => copyTestDependencyTree("missing", source, target), /dependency is missing/);
    assert.throws(() => copyTestDependencyTree("../outside", source, target), /Unsafe/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
