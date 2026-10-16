# Contributing to Alysis Code

Thank you for considering a contribution. Bug fixes, documentation improvements, tests, and
focused feature proposals are welcome.

## Before you start

- Search existing issues before opening a new one.
- Keep each pull request focused on one problem.
- For substantial behavior changes, open an issue before investing in an implementation.
- Report vulnerabilities privately according to [SECURITY.md](SECURITY.md).

## Development setup

Alysis Code requires Python 3.11 or newer and uses [uv](https://docs.astral.sh/uv/) for its locked
development environment.

```bash
git clone https://github.com/AlysisAi/AlysisCode.git
cd alysis-code
uv sync --extra dev
```

Run the standard checks:

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run python scripts/qa/check_public_repo.py
```

During development, run the smallest relevant test selection first. Run the complete suite before
requesting review for changes that affect shared runtime behavior.

## Making changes

Create a branch from the latest `main`:

```bash
git switch -c fix/short-description
```

When editing the project:

- Preserve backward compatibility unless the change explicitly targets a documented breaking
  release.
- Add or update tests for behavior changes.
- Update user-facing documentation when commands, configuration, or output changes.
- Do not commit credentials, local configuration, logs, benchmark results, or generated caches.
- Prefer small changes that reviewers can understand without reconstructing unrelated work.

Contributors are responsible for reviewing everything in their pull request, including generated
or tool-assisted changes.

## Pull requests

A pull request should explain:

- the problem being solved;
- the approach taken;
- how the change was tested; and
- any compatibility, security, or operational impact.

Link related issues and include screenshots for terminal UI changes when they help reviewers assess
the result. All required CI checks must pass before merge.

Commit messages should be short and descriptive. The repository generally follows Conventional
Commits, for example `fix: handle empty provider responses` or `docs: clarify sandbox setup`.

By participating, you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
