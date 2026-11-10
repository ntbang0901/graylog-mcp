# Contributing

## Setup

```bash
uv sync                     # Python 3.11+, installs dev tools from uv.lock
uv run --only-group lint pre-commit install   # optional: ruff and mypy on every commit
```

## Checks

```bash
uv run ruff check && uv run ruff format --check
uv run mypy
uv run pytest               # unit + contract tests, coverage must stay >= 85%
```

Changes to `backends/`, `client.py`, query building or `rca.py` should also pass the integration suite
against real Graylog containers (needs Docker, about 3 GB of RAM per version):

```bash
tests/integration/run.sh v52        # v43 v50 v52 v61 v70
```

CI runs on every pull request and every push to `main`: lint, mypy and tests (Linux on 3.11-3.13, plus
Windows and macOS), a build and smoke test of the wheel and of the Docker image (amd64 and arm64), a scan
of the image for fixable high-severity vulnerabilities, `pip-audit` on the runtime dependencies, and
actionlint, zizmor and the pre-commit hygiene hooks on the repository. The same tools run locally:

```bash
uv run --only-group lint actionlint
uv run --only-group lint zizmor .github/workflows
```

`main` only accepts pull requests whose "CI passed" check is green. Actions are pinned by commit SHA with the
version in a comment; Dependabot keeps them, the Python dependencies and the base images current, and its
minor and patch updates merge on their own once CI passes. The integration matrix runs weekly and on pull
requests that touch the backends.

## Rules of the project

- **Read-only.** Never add a request that changes Graylog state. New `POST` endpoints must execute or check
  a query without storing anything and be added to `READ_ONLY_POSTS` with a comment saying why.
- **Everything shown is redacted and budgeted.** Tool output goes through `Redactor` and the output budget.
  Add a test with a planted sensitive value for new output paths.
- **Site-specific values belong in config**, not in code.
- **Tests come with fixes.** For behaviour that differs between Graylog versions, extend
  `tests/fake_graylog.py` with the real response shape and note the version.
- Update `CHANGELOG.md` under "Unreleased".

## Releasing

1. In a pull request, move the "Unreleased" entries to a new version section in `CHANGELOG.md` and bump `__version__` in
   `src/graylog_mcp/__init__.py` and `version` in `pyproject.toml`.
   CI fails a pull request where the three disagree or the version has no `CHANGELOG.md` section.
2. Merge the pull request. The release workflow sees a version without a `vX.Y.Z` tag, runs the full CI on
   the merge commit, publishes the wheel and sdist CI smoke-tested to PyPI (trusted publishing with
   attestations), then signs them with Sigstore, creates the GitHub release with the `vX.Y.Z` tag and pushes
   the Docker image (amd64 and arm64) to GHCR. Do not push tags yourself; `v*` tags cannot be moved or deleted.
