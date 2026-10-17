# graylog-mcp

A read-only MCP server for Graylog 4.x to 7.x (Python 3.11+, managed with uv). README.md explains every
feature; CONTRIBUTING.md how to work on it.

## Rules

- **Never write to Graylog.** `client.py` only sends GET, and POST to the read-only search endpoints in
  `READ_ONLY_POSTS`. Do not add another method or endpoint.
- **Everything returned to the model is redacted and size-capped**: values go through the redactor
  (`redact.py`, `App.redact_key`) and results through the budget (`shaping.py`).
- Site-specific values (field names, queries, timezones) belong in configuration, not in code.
- Tests and docs use neutral examples only: `acme`, `example.com`, `*.test`, `fake-...` passwords. Never a real
  company, service, hostname, token or password.

## Before opening a pull request

```bash
uv sync
uv run ruff check && uv run ruff format --check
uv run mypy
uv run pytest
```

All four must pass. Add tests for new behaviour (`tests/fake_graylog.py` emulates Graylog 4.3 to 7.0), and add a
line under "Unreleased" in CHANGELOG.md. Match the surrounding code: its naming, comment density and the plain,
specific wording of its docstrings and error messages.
