# Maintenance scripts

This directory contains repository maintenance, quality-assurance, release, and benchmark helpers.
They are not imported by Alysis Code during normal use.

## Common tasks

- `refresh_litellm_model_catalog.py` updates the bundled LiteLLM model metadata snapshot.
- `refresh_chatgpt_codex_model_catalog.py` creates a sanitized fallback snapshot from a reviewed
  model-discovery response.
- `build_benchmark_wheel.sh` builds a provenance-stamped wheel for benchmark adapters in a unique
  `dist/benchmark.*` directory, then restores the original local build metadata.
- `qa/` contains dependency, repository-hygiene, protocol, and mock-runtime checks.
- `release/` contains package validation, dependency auditing, and attestation checks used by
  GitHub Actions.

## Installer

- `install.sh` is the end-user installer behind the one-line install command in
  the [README](../README.md#get-started). It installs uv when it is missing, then
  installs `alysis-code` as a uv tool. Users fetch it straight from this path on
  `main`, so moving or renaming it breaks the documented command. Keep it POSIX
  `sh`: `curl … | sh` runs it under dash on Ubuntu. CI runs it with shellcheck
  and in a clean home on Linux and macOS.

## Scope

Scripts may use network access or local tools. Review the script before running
it, and prefer explicit environment variables over persistent local state.

User-facing CLI behavior should live under `src/alysis_code/`, not in
this directory. The installer is the exception, because it runs before the
package exists.

## See Also

- [Contributing](../.github/CONTRIBUTING.md)
- [Release process](../docs/RELEASING.md)
