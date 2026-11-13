# Releasing Alysis Code

This document is for maintainers publishing the Python package or sandbox images. Release
credentials are managed through protected GitHub environments; they must not be stored in the
repository or passed on the command line.

The commands below describe separately authorized releases from this repository. Do not run
tag or package publication as part of an ordinary source update. Verify the destination
repository before any push.

1. Start from a clean commit on `main` with all required checks passing.
2. Update the version in `pyproject.toml`, `src/alysis_code/__init__.py`, and `uv.lock`.
   Keep `MIN_RECOMMENDED_ALYSIS_CLI_VERSION` in
   `extensions/vscode-alysis/src/client/compatibility.ts` and the tested CLI
   version in `extensions/vscode-alysis/docs/architecture.md` in step. The
   extension compatibility test verifies these against `pyproject.toml`.
3. Move user-facing entries from `Unreleased` into a dated section in `docs/CHANGELOG.md`.
4. Run the release checks locally, including the focused [release checklist](release_checklist.md):

   ```bash
   uv sync --frozen --extra dev --extra documents --extra server --extra azure-host --extra shared-database --extra cloud-storage
   uv run --frozen --no-sync ruff check .
   uv run --frozen --no-sync ruff format --check .
   uv run --frozen --no-sync pytest -q
   ```

   The full suite imports the optional hosted-compute, database, and storage
   adapters. Install their declared extras for qualification even when external
   integration tests have no configured service and skip intentionally.

5. Run the extension compile, lint and unit checks from `extensions/vscode-alysis`:

   ```bash
   npm ci
   npm run compile
   npm run lint
   npm test
   ```

   Commit the release metadata and obtain review. From the clean candidate commit,
   generate its build identity and validate the exact release archives:

   ```bash
   uv run --frozen --no-sync python scripts/generate_build_info.py --require-clean --no-fetch
   uv build --clear --no-create-gitignore --no-build-isolation --no-sources --out-dir dist/python
   uv run --frozen --no-sync python scripts/release/validate_python_distributions.py dist/python \
     --expected-commit "$(git rev-parse HEAD)"
   ```

   The generated `src/alysis_code/_build_info.py` is a build input, not a source change
   to commit. Restore its original contents after building; CI does this automatically.
6. Create and push the version tag:

   ```bash
   VERSION=0.14.0
   git tag "v${VERSION}"
   git push origin "v${VERSION}"
   ```

The tag-triggered `release` workflow validates that the tag, package version, source commit, and
default branch agree. It then tests and builds the source distribution and wheel, creates an SBOM
and provenance attestations, publishes through PyPI trusted publishing, and creates the GitHub
release.

After publication:

- confirm the GitHub Actions run completed successfully;
- verify the version and files on PyPI;
- install the wheel in a clean environment; and
- run `alysis-code --version` and `alysis-code --help`.

The distribution validator installs the exact wheel and its declared runtime dependencies
in a fresh virtual environment, without development extras or inherited Python paths.
It checks help, version, simulated hosted login, and an ordinary first agent turn with
a local model-response fixture. No real account or paid model call is needed.

Never move or reuse a published version tag. If a release is defective, fix the problem and publish
a new patch version. Yank a PyPI release only when leaving it available would harm users.

## Unsigned native build checks

`managed-cli-build-check` builds and smoke-tests the native executable on all six
supported platforms. Windows ARM64 and Intel macOS can reuse a tested static OpenSSL
installation in this unsigned workflow. Signing workflows continue building OpenSSL
from source and do not consume this cache.

The cache key binds the platform, hosted runner image version, installation path,
build script and cache validator. Changes to the source pin, configure flags or test
recipe therefore invalidate it. An exact hit must also match a receipt containing
the identity and SHA-256 of every installed file. This receipt detects stale or
damaged contents; it is not signed provenance. Cache access follows
[GitHub's branch and PR scope rules](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching#restrictions-for-accessing-a-cache).

On a miss, partial match or invalid receipt, the installation is cleared and the full
source build and upstream tests run. Only a successful job saves a new cache. Every
run still installs the frozen application dependencies, checks the linked OpenSSL
version and SBOM, builds the application, and smoke-tests its actual executable.

For a fresh qualification, manually dispatch `managed-cli-build-check` with
`rebuild_openssl` enabled; this bypasses both cache reads and writes. If an immutable
cache entry is damaged, delete that specific entry in Actions caches so the next
successful normal build can replace it. Runner image or build-input changes require
one new cold build before later runs benefit.

## Sandbox images

Sandbox images are published from tags named `sandbox-vX.Y.Z`. The `sandbox-image` workflow builds,
scans, signs, attests, and smoke-tests each supported image variant before promotion.

Before tagging, confirm that the protected `sandbox-release` environment has required reviewers,
self-review prevention, and deployment restrictions for `main` and `sandbox-v*` tags.

Create and push the tag:

```bash
VERSION=0.14.0
git tag "sandbox-v${VERSION}"
git push origin "sandbox-v${VERSION}"
```

## PyPI

Release tags build the wheel and source distribution. The publish job expects PyPI trusted
publishing to be configured for this repository and release workflow.

After the workflow finishes:

- Confirm the package page shows the expected version.
- Install the package in a clean environment.
- Run `alysis --help`.

## Sandbox Images

Sandbox images are published under:

```text
ghcr.io/alysisai/alysis-sandbox
```

Sandbox releases use an isolated Git tag namespace. Create a tag such as
`sandbox-v0.9.7`; ordinary Python or extension `v*` tags do not trigger the
container workflow.

The workflow first publishes source- and run-bound candidates under
`:candidate-<variant>-<full-sha>-<run-id>-<run-attempt>`. It scans and
smoke-tests both `linux/amd64` and `linux/arm64`, creates provenance, and signs
every candidate digest. Only after **all three variants** pass does the
promotion job create consumer tags:

- `:<variant>` for the moving variant tag, for example `:dev`
- `:<variant>-<sha12>` for the immutable per-commit tag
- `:<variant>-<sandbox-git-tag>` for release tags
- `:<sandbox-git-tag>` for the default variant

The default variant is `dev`. Manual runs default to `operation=check`, which builds,
scans, and smoke-tests a local image without publishing. A release run must select
`operation=release`, target the public repository default branch, and explicitly
select `publish_moving_tags` to update moving tags.
Candidate tags are not consumer release channels.
Promotion refuses to replace an existing per-commit or release tag with a
different digest; only the explicitly selected moving aliases may move.

Repository administrators must configure the `sandbox-release` GitHub
environment with required maintainer reviewers, self-review prevention, and
deployment-branch/tag restrictions for the default branch and `sandbox-v*`
release tags. The write-enabled promotion job is the only job attached to this
protected environment. The workflow checks that reviewers and self-review prevention
are configured before building.

### First publication and missing-image recovery

The CLI and sandbox have separate releases. Updating the image name in source or merging a PR
does not publish an image. Development pushes and PRs do not start hosted CI. Rerunning an old
Alysis workflow also uses that run's old source and image name.

After merging reviewed changes into the public repository's default branch and configuring
`sandbox-release`, start a new run with moving tags enabled:

```bash
gh workflow run sandbox-image.yml --repo AlysisAi/alysis-code --ref main \
  -f operation=release -f default_variant=dev -f publish_moving_tags=true
```

Without `publish_moving_tags=true`, a manual run does not create or update the `:base`, `:dev`,
`:server`, and `:latest` references used by normal installations. A `sandbox-v*` tag release does
update those references.

For the first publication, the package owner must check **Packages > alysis-sandbox > Package
settings**, set visibility to **Public**, and grant `AlysisAi/alysis-code` Actions access. New GHCR
packages default to private; a public source repository does not establish anonymous package
access. See [GitHub's package visibility guide](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility).

The workflow checks anonymous candidate access before changing consumer tags. If that check
fails because the new package is private, correct its visibility and rerun the failed jobs.
Resolve scan or smoke-test failures before approving the protected promotion job. A release is
successful only after every promoted reference passes anonymous pulls on both architectures.

Reproduce a fresh user's download without changing the operator's Docker login:

```bash
bash scripts/sandbox/check-public-images.sh --pull \
  ghcr.io/alysisai/alysis-sandbox:dev \
  ghcr.io/alysisai/alysis-sandbox:server
alysis sandbox doctor --smoke
```

The script uses temporary, explicitly empty registry credentials, disables native credential-helper
autodetection, and ignores `DOCKER_AUTH_CONFIG`. Without `--pull`, it checks public manifests and
platform support without downloading image layers. Do not close an image
availability issue based only on a successful build or an authenticated pull.

### Image security maintenance

Refresh the digest-pinned Wolfi base and complete `scripts/sandbox/packages.*.lock` closures
together. Keep signed APK verification enabled and update matching checksums for downloaded
toolchains. Python, Node, npm, and Bubblewrap receive their packaged fixes from Wolfi. See
[sandbox supply-chain requirements](../scripts/sandbox/README.md#supply-chain-contract).

HIGH/CRITICAL findings remain blocking, including unfixed findings. Review retained reports from
both architectures; a reduction in findings is not a passing scan. Exceptions require evidence
that the vulnerable code does not apply to the image, with the scope and rationale documented.
Reviewed applicability evidence is bound to the exact image using
[OpenVEX assessments](../scripts/sandbox/ASSESSMENTS.md); original findings remain available.
Do not blanket-ignore unfixed findings or remove scanner metadata to make an image pass.

Native Linux Bubblewrap installations use the host package and receive updates through the host
package manager.

## Verify A Release Image

Pull the image:

```bash
docker pull ghcr.io/alysisai/alysis-sandbox:dev
```

For production use, prefer a digest-pinned image:

```bash
docker buildx imagetools inspect ghcr.io/alysisai/alysis-sandbox:dev
```

The workflow file is the authoritative description of image variants, signing, provenance, SBOM,
and promotion checks.

## Failed releases

- Do not rerun publication from a different commit under the same tag.
- Do not bypass a failing validation, scan, signature, or provenance check.
- Keep failed workflow logs and artifacts private if they contain operational details.
- Correct the source, increment the version when necessary, and create a new reviewed tag.
