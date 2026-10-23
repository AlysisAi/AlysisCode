import { cpSync, existsSync, mkdirSync, readFileSync } from "node:fs";
import { basename, dirname, relative, resolve, sep } from "node:path";

/** Stage test dependencies with the same nested layout used by Node resolution. */
export function copyTestDependencyTree(
  packageName: string,
  sourceNodeModules: string,
  targetNodeModules: string
): void {
  const sourceRoot = resolve(sourceNodeModules);
  const copied = new Set<string>();

  function copy(name: string, fromDirectory: string, optional = false): void {
    if (!/^(?:@[a-z0-9._-]+\/)?[a-z0-9._-]+$/i.test(name)) {
      throw new Error(`Unsafe Extension Host test dependency name: ${name}`);
    }
    let search = fromDirectory;
    let sourceDirectory: string | undefined;
    while (search === dirname(sourceRoot) || search === sourceRoot || search.startsWith(`${sourceRoot}${sep}`)) {
      const modules = basename(search) === "node_modules" ? search : resolve(search, "node_modules");
      const candidate = resolve(modules, ...name.split("/"));
      if (candidate.startsWith(`${sourceRoot}${sep}`) && existsSync(resolve(candidate, "package.json"))) {
        sourceDirectory = candidate;
        break;
      }
      search = dirname(search);
    }
    if (!sourceDirectory) {
      if (optional) return;
      throw new Error(`Extension Host test dependency is missing: ${name}`);
    }
    if (copied.has(sourceDirectory)) return;
    copied.add(sourceDirectory);
    const manifest = JSON.parse(readFileSync(resolve(sourceDirectory, "package.json"), "utf8")) as {
      dependencies?: Record<string, unknown>;
      optionalDependencies?: Record<string, unknown>;
    };
    const targetDirectory = resolve(targetNodeModules, relative(sourceRoot, sourceDirectory));
    mkdirSync(dirname(targetDirectory), { recursive: true });
    cpSync(sourceDirectory, targetDirectory, { recursive: true, force: true });
    const optionalNames = new Set(Object.keys(manifest.optionalDependencies ?? {}));
    for (const dependency of Object.keys(manifest.dependencies ?? {})) {
      copy(dependency, sourceDirectory, optionalNames.has(dependency));
    }
    for (const dependency of optionalNames) {
      copy(dependency, sourceDirectory, true);
    }
  }

  copy(packageName, dirname(sourceRoot));
}
