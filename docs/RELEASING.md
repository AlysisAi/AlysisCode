# Releasing Alysis Code

This document is for maintainers publishing the Python package or sandbox images. Release
credentials are managed through protected GitHub environments; they must not be stored in the
repository or passed on the command line.

## Python package

1. Start from a clean commit on `main` with all required checks passing.
2. Update the version in `pyproject.toml`, `src/alysis_code/__init__.py`, and `uv.lock`.
   Keep `MIN_RECOMMENDED_ALYSIS_CLI_VERSION` in
   `extensions/vscode-alysis/src/client/compatibility.ts` and the tested CLI
   version in `extensions/vscode-alysis/docs/architecture.md` in step. The
   extension compatibility test verifies these against `pyproject.toml`.
3. Move user-facing entries from `Unreleased` into a dated section in `docs/CHANGELOG.md`.
4. Run the release checks locally, including the focused [release checklist](release_checklist.md):

   ```bash
   uv sync --frozen --extra dev
   uv run --frozen --no-sync ruff check .
   uv run --frozen --no-sync ruff format --check .
   uv run --frozen --no-sync pytest -q
   ```

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

After the workflow succeeds, inspect the published manifest and prefer digest-pinned references in
production:

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
