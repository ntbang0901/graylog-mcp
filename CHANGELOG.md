# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `graylog-mcp start`: one command to install a stable copy (`uv tool`), run the shared server in the
  background with the admin page at `/admin`, start it at login (launchd, systemd, XDG autostart, Windows Task
  Scheduler), register Claude Code once for every project (private overrides for projects whose `.mcp.json` starts uvx) and open
  the page. `stop`, `status`, `update` (reinstall from the same source and restart); `ui` opens the running
  server's page. The admin page starts with a Setup checklist with one button per step, and versions show
  their git commit. Where no service manager restarts a crashed server (XDG autostart, Task Scheduler), it runs
  under `graylog-mcp keepalive`, which starts it again (giving up after 5 quick crashes); on Windows through
  pythonw, so no console window opens. A Startup-folder entry from an earlier version is replaced.
  The background server listens on 127.0.0.1:18742 (8000 is taken by many dev servers); when another program
  has the port, `start` takes the next free one, and `stop`, `status`, `update`, `ui` and `install --shared`
  follow the port `start` used.
  Command output no longer fails on consoles that cannot show ✓ (a Windows pipe in cp1252).
- `serve --shared`: one HTTP server process for every session and repository instead of one stdio process per
  session. Each client names its repository (`X-Graylog-MCP-Repo` header or `?repo=`) and gets that repository's
  config, group and focus; Graylog clients, version detection and caches are shared; configs reload when edited.
  `install --shared` (`--source shared`), `repo add --shared` and the admin UI write the matching HTTP entries.
- `scan`: runs scan rules concurrently with exact counts against a baseline and returns only what fired, most
  severe first, with the query, top groups (new values flagged) and one sample per finding. Eight built-in rules
  (crash, resource_exhaustion, error_spike, new_error_types, http_5xx, connectivity, database, auth_failures);
  `[scan.rules.<name>]` adds rules or overrides built-in keys, `[scan] disable/exclude`; ad hoc `checks` per call;
  selection by name, tag or `min_severity`. `list_scan_rules`, a `scan` MCP prompt, `scan` presets and
  `limits.scan_concurrency`. The server instructions tell the model how to scan fast and accurately.
- Scan accuracy: `per_traffic` rules compare shares of traffic (`traffic_query`), so errors that follow traffic
  stay quiet (the built-in error, 5xx, connectivity and database rules use it); `baseline_shift` /
  `baseline_periods` compare with the same window days or weeks earlier, using the median period and dropping
  periods without data; growth fires only when significant (exact conditional binomial test, rule
  `confidence`, reported per result). Identical counts are made once per scan. Accuracy scenarios in
  `tests/test_scan_scenarios.py`.
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
- Admin UI environment form: one Token/Password field, saved on this machine; the environment variable name is
  set automatically (editable under Advanced for CI). `init` no longer asks for variable names.
- `*_env` values must be variable names; a secret typed into such a field is refused without being echoed,
  and the admin UI scrubs it from what it shows.
- Group repositories: `[groups.<g>] repos = [...]` lists local folders or git remotes; running inside one of
  them makes its group the default. Admin UI and `graylog-mcp repo add|remove|list` attach repositories and set
  them up (`.graylog-mcp.toml` with `include`, Claude Code registration).
- Admin UI Settings page: edit settings per scope (global, environment, group, instance) with the inherited
  value shown; field detection can be applied to a chosen scope.
- A repository loads only its group: inside a repository listed by a group (or with `only_groups` /
  `GRAYLOG_MCP_GROUPS`), the other groups' instances are not loaded, and asking for one explains why and
  how to enable it. `list_instances` reports the `scope`; repository setup writes `only_groups`.
- A 403 on one search API (e.g. a role without universal search) falls back to the next API (views,
  scripting) and keeps the refused one as a last resort, for messages and aggregations.
- `top_values` / `error_summary` on a full-text field (`message`), which OpenSearch refuses to aggregate,
  count the newest 1000 matching messages instead, grouping variants of a log line by template, and say so
  (`method: sampled`).
- Focus per repository: inside a repository, search/count/summary/histogram/top/compare/detect_changes look
  at its service only (`[focus]` in its `.graylog-mcp.toml`, or guessed from the repository name), and widen
  only when asked (`streams=["*"]`, explicit streams, a query naming a service field). Set from the admin UI,
  `graylog-mcp repo focus` or `GRAYLOG_MCP_SERVICE`. `application` joins the default service fields.
- Admin UI redesign: sidebar navigation, an overview with health per group that runs checks on load, an
  environment drawer with inline validation, one card per group with its repositories, a sticky save bar,
  confirm dialogs, toasts, loading states, dark mode and a phone layout.
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
