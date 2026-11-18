# Verification and dogfood

## Component and production-candidate verification

From the repository root, run:

```bash
bash scripts/qa/check_vscode_extension_release_candidate.sh
```

This local gate validates the generic extension component. Its VSIX deliberately has no signed
managed runtime, is labelled not-for-release, and can never be production evidence. The matching
`vscode-extension-component-candidate` workflow must stay red if parity, protocol, Extension Host,
or component dogfood fails.

Production candidates come only from `.github/workflows/managed-cli-vsix-release.yml`, dispatched
from an immutable release tag. It builds six natively signed/verified managed runtimes from
`uv.lock`, verifies executable and final-VSIX provenance/SBOM attestations, packages one runtime per
target, then clean-installs each VSIX and activates the installed extension in production mode.
Public beta signoff also requires a completed local manual real-provider report, validated with:

```bash
python scripts/qa/validate_vscode_manual_provider_report.py \
  <report.json> <target.vsix> <production-dogfood.json>
```

The generated manual `manual_steps_written` report is only a checklist placeholder, and HTTPS report
URLs are rejected by CI. Completed evidence must use schema-v5 `completed-manual-report`, bind the
exact target VSIX and managed-runtime hashes/signature/install evidence, pass the validator above,
bind the report to the exact signed candidate and production-summary bytes, record immutable
release/candidate/host metadata, and provide a timestamped structured receipt for every current
required check ID. Manual smoke coverage, required check IDs, and IDE v1 non-goals are tracked in
[`docs/vscode_extension_release_checklist.md`](../../../docs/vscode_extension_release_checklist.md).

## Dogfood harness

Use the repo-level dogfood harness before release:

```bash
bash scripts/qa/check_vscode_extension_release_candidate.sh
python scripts/qa/vscode_extension_dogfood.py --mode mock --extension-host skip
python scripts/qa/vscode_extension_dogfood.py --mode mock --extension-host run
VSCODE_TEST_EXECUTABLE_PATH=/absolute/path/to/code \
  python scripts/qa/vscode_extension_dogfood.py --mode mock --extension-host require-cached
python scripts/qa/vscode_extension_dogfood.py --mode manual-real-provider \
  --vsix-path /absolute/path/to/vscode-alysis-<target>.vsix --package-mode require
```

Mock mode is deterministic and does not require model credentials. It copies the disposable
`tests/fixtures/vscode_dogfood/simple-python-project` fixture, initializes git, checks bridge health
with the active Python interpreter plus `PYTHONPATH=src`, verifies or builds the VSIX, and writes
evidence under `qa_reports/vscode_dogfood/`. It does not default to `uv run`; use `--cli-command`
only when intentionally testing an installed binary.

The local script defaults to `ALYSIS_RELEASE_DOGFOOD=mock`. Set
`ALYSIS_RELEASE_DOGFOOD=extension-host` or `require-cached` for full generic component dogfood,
and use `skip` only for explicit iteration. `ALYSIS_RELEASE_STRICT=1` additionally requires
Extension Host coverage and a pre-release component package. Even then, the summary is
`component_valid: true` and `release_valid: false` because the generic VSIX has no signed managed
runtime. Use the target-specific managed-runtime workflow and production-install evidence before
completing `docs/vscode_extension_production_signoff_template.md`.

`--extension-host skip` is local smoke only. Full component dogfood uses `--extension-host run` or
`--extension-host require-cached`. Use `--vscode-executable /absolute/path/to/code` or
`VSCODE_TEST_EXECUTABLE_PATH=/absolute/path/to/code` to avoid downloading VS Code. If no executable
is provided and download is unavailable, the report records a clear Extension Host preflight
failure. Generic reports record `component_valid: true/false`, `release_valid: false`,
`extension_host: passed/skipped/failed`, and an `extension_host_preflight` object explaining the
cached executable or download preflight result. Manual real-provider mode prepares the same fixture
and writes the checklist maintainers must complete before public beta or Marketplace publication.

## Screenshot capture checklist

The Marketplace listing needs real captures: a short demo plus setup, plan-readiness,
approval/diff-review, and runtime/sandbox screenshots. Keep the README's `PUBLISH BLOCKER`
status until the listing visuals are complete and reviewed. Use packaged
`resources/screenshots/*.png` images or absolute HTTPS image URLs as supported by
`scripts/package-release.js`.

Capture from a disposable workspace after running the sidebar through:

- a review-mode task with an approval prompt so the approval card and scoped actions are shown
- native diff and artifact open actions so reviewers can verify backend-scoped outputs
- **Check Setup** so Runtime Details shows a real sandbox smoke result
- the Managed Browser start/navigation/snapshot/screenshot/diagnostics/close lifecycle, including
  its URI-backed preview, native Save As flow, public-only guard, and ephemeral close cleanup
- **Start local testing** modal confirmation, `public_loopback` status, direct-user/agent isolation,
  successful loopback navigation, and denied LAN plus link-local navigation

Screenshots must not contain secrets, customer code, private repository paths, or unpublished
Marketplace claims.
