## What and why

<!-- What does this change and which problem does it solve? Link the issue if there is one. -->

## How it was tested

- [ ] `uv run pytest` (unit + contract)
- [ ] `uv run ruff check && uv run ruff format --check && uv run mypy`
- [ ] Integration against real Graylog (`tests/integration/run.sh <v43|v50|v52|v61|v70>`) — needed for changes to `backends/`, `client.py` or query building

## Checklist

- [ ] The server still never writes to Graylog (only GET, and POST to the read-only search endpoints)
- [ ] New output goes through redaction and the size budget
- [ ] CHANGELOG.md updated under "Unreleased"
