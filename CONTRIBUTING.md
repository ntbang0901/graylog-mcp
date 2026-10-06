# Contributing

## Setup

```bash
uv sync                     # Python 3.11+, installs dev tools from uv.lock
uv run pre-commit install   # optional: ruff and mypy on every commit (pip install pre-commit first)
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

CI runs lint, mypy and tests (Linux on 3.11-3.13, plus Windows and macOS), builds and smoke-tests the
wheel and the Docker image on every push. The integration matrix runs weekly and on pull requests that
touch the backends.

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

1. Move the "Unreleased" entries to a new version section in `CHANGELOG.md` and bump `__version__` in
   `src/graylog_mcp/__init__.py` and `version` in `pyproject.toml`.
2. Tag `vX.Y.Z` and push the tag. The release workflow publishes to PyPI (trusted publishing with
   attestations), signs the artifacts with Sigstore, creates the GitHub release and pushes the Docker image
   to GHCR.
