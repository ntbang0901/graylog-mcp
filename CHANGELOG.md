# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `root_cause`: ranks the service that broke first using per-service error/traffic/latency onsets against
  a baseline (first error pinned to the millisecond), changes found in the logs and the inferred call graph;
  returns a verdict, timeline, evidence and next steps.
- `detect_changes`: deploys and restarts from version fields, host rollouts and start/stop lines.
- `service_map`: caller -> callee graph inferred from traces sampled per service, with error rates and
  p50/p95 latency.
- Config: `version_fields`, `latency_fields`, `change_query`.
- Project config discovery: `.graylog-mcp.toml` in the current directory or a parent (up to the repository
  root), for one repository with several environments; per-instance `description` shown by `list_instances`.
- Integration scenario (bad deploy cascading through three services) verified on Graylog 4.3-7.0.
- Setup helpers: `graylog-mcp init` (guided or scripted setup with connection tests and field detection),
  `doctor` (checks with fixes), `detect` (field suggestions with coverage), `install` (Claude Code, Claude
  Desktop, Cursor, VS Code).
- `graylog-mcp ui`: local admin web UI (environments, field mapping, redaction playground, tool playground,
  client installation, config editor), loopback only with a per-run access token.
- CLI subcommands; `graylog-mcp` alone still runs the server.
- Per-user secret store: `graylog-mcp login` / `logout`, the init wizard and the admin UI save tokens and
  passwords in `~/.config/graylog-mcp/secrets.toml` (owner-only, outside the repository); the server uses
  environment variables first, then this file. Client configs use `${VAR:-}` so unset variables fall back to it.
- `*_env` values must be variable names; a secret typed into such a field is refused without being echoed,
  and the admin UI scrubs it from what it shows.
- Groups x user-defined environments: `[groups.<g>.environments.<e>]` (instances `<g>/<e>`), shared
  `[environments.<e>]` settings, `default_group` / `default_environment`, and `include` for one company-wide
  file. The `instance` argument accepts `payment/prod`, `payment prod`, a group or an environment.

## [0.1.0] - 2026-10-06

### Added
- Read-only MCP server for Graylog 4.x-7.x with version detection (`/api/system`, falling back to `/api/`)
  and per-version API selection with automatic fallback (universal search, views search, Scripting API).
- Tools: `search_logs`, `count_logs`, `get_message`, `trace_request`, `context_around`, `error_summary`,
  `log_histogram`, `top_values`, `compare_periods`, `list_streams`, `list_fields`, `list_presets`,
  `run_preset`, `list_instances`.
- Masking of sensitive data: field names, emails, credentials, JWTs, key/value secrets, URL credentials,
  private keys, Luhn-checked card numbers; country packs `vn`, `us`, `eu`, `uk`, `in`; custom patterns,
  allow-list and field exclusions.
- Output shaping: stack trace folding (Java, Python, .NET, Go, Node), grouping of repeated lines,
  per-value and per-call size caps with paging, timezone-aware timestamps, `index/id` refs.
- Configuration through environment variables or a validated TOML file (multiple instances, token or
  basic auth, TLS/CA bundle, proxy, timezone, trace fields, error query, presets, limits).
- stdio and streamable HTTP transports; bearer-token auth for HTTP; Docker image.
- Query validation through `/search/validate`: precise syntax errors and unknown-field hints on empty results.
- Unit, contract (emulated 4.3/5.0/5.2/6.1/7.0) and docker-compose integration test suites; verified against
  Graylog 4.3.15, 5.0.13, 5.2.12, 6.1.16 and 7.0.13.
