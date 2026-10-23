# Sandbox npm runtime rebuild

This is a complete sandbox-only rebuild of npm `11.20.0` as
`11.20.0-alysis.1`. It is used by the sandbox Dockerfile after six-image private
qualification. It is not published to the npm registry. Production image
promotion still requires the public workflow's scans, qualification and signatures.

The owner approved this rebuild on 4 October 2026 after the private six-image
trial remained blocked by three HIGH findings in npm. A fresh registry audit
also identified subsequent advisories; the reviewed rebuild addresses all of
them with these four dependency updates:

| Runtime dependency | Upstream bundle | Rebuild | Findings addressed |
| --- | --- | --- | --- |
| brace-expansion | 5.0.9 | 5.0.12 | CVE-2026-102276, CVE-2026-102278, GHSA-q2hr-2g5m-vwhr |
| undici | 6.28.0 | 6.28.1 | CVE-2026-19534 |
| http-cache-semantics | 4.2.0 | 4.3.0 | GHSA-ch52-4w7c-c8xp |
| ip-address | 10.5.0 | 10.7.3 | GHSA-rpw4-54j3-4h4q, GHSA-2vr4-cq9g-pvrc, GHSA-j6r3-76f7-8jcv, GHSA-h3mg-xc3c-68pw |

These version changes require a fresh full-image scan; they are not exceptions
to the scanner or proof that the entire image is vulnerability-free.

## Inputs and lock provenance

- Upstream release: <https://registry.npmjs.org/npm/11.20.0>.
- Complete upstream distribution SHA-256:
  `d1a92f40e6c407b84c3a00c3cf978a10b24fd42f153c527e2016cef7bb34a483`.
  During preparation, its SHA-512 also matched the registry integrity value.
- Upstream source commit: `d12b9434dd010b5fb7044c3cc149cdda317813f8`, the
  `v11.20.0` tag in <https://github.com/npm/cli>.
- The initial runtime lock was derived from that commit's `package-lock.json`
  and the **143 actual dependency package roots** in the published archive.
  Upstream development dependencies were excluded. The 12 shipped workspace
  packages were bound to their same-version registry tarballs and SHA-512
  integrity values instead of source-tree links.
- Root dependency versions are pinned to the shipped versions. Only the four
  dependency versions in the table change. `overrides` records those decisions.
  npm's lockfile resolver validated the complete graph without adding, removing,
  or changing any further package version.
- Every non-root lock entry has a registry URL and SHA-512 integrity. The
  checked-in lock, not a new resolution of version ranges, controls every build.

## Build and verify

`build.py` checks the upstream archive digest before extraction and uses its
exact npm CLI as the bootstrap. It copies upstream CLI code, generated docs,
and licenses, omitting the entire upstream `node_modules` tree from the runtime.
The reviewed manifest removes build-only workspaces, development dependencies,
and lifecycle scripts, and identifies the private rebuild version.

The builder runs `npm ci --ignore-scripts`, verifies every installed dependency,
then runs a second clean `npm ci --offline --ignore-scripts` against its isolated
cache. It checks `npm ls`, requires the current registry audit to pass at the
existing HIGH threshold, bundles all runtime dependencies, and opens the
resulting archive to verify the complete package inventory again. It never
edits files inside an existing nested dependency. The bootstrap and its old
dependencies remain in a separate builder stage and are not copied to the final
image. Build directories must be new, so retries cannot remove unrelated files.

```sh
python scripts/sandbox/npm-rebuild/build.py \
  --work-dir /tmp/alysis-npm-work --output-dir /tmp/alysis-npm-output
```

For local Windows checks, use short work/output paths to avoid legacy Windows
path-length limits in the Python cache archiver. Official image builds run on
native Linux amd64 and arm64 runners with the Dockerfile's pinned Node version.

`smoke.cjs` exercises the **packed** npm/npx CLI, brace expansion, undici's mock
HTTP dispatcher, local package packing, locked offline install, package scripts,
and dependency bin execution. CI runs it with Docker networking disabled.

```sh
node scripts/sandbox/npm-rebuild/smoke.cjs /path/to/unpacked/package
```

Each dev/server trial retains the rebuilt tarball, original upstream tarball,
content-addressed dependency cache, lock, package inventory, dependency-tree
validation, current dependency audit, and build receipt. The receipt binds the builder, manifest, lock,
archive, and cache by hash and records bootstrap/Node versions. Original inputs
are retained as CI artifacts, not in the runtime image. Do not treat private
trial artifacts as production signing or Marketplace publication evidence.

Production adoption still requires review of the Wolfi package graph and input
retention, all six passing image scans and smoke tests, and the existing public
sandbox release workflow. Existing scan severity and exception policies remain
unchanged.
