# graylog-mcp

[![CI](https://github.com/ntbang0901/graylog-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/ntbang0901/graylog-mcp/actions/workflows/ci.yml)
[![Integration](https://github.com/ntbang0901/graylog-mcp/actions/workflows/integration.yml/badge.svg)](https://github.com/ntbang0901/graylog-mcp/actions/workflows/integration.yml)
[![CodeQL](https://github.com/ntbang0901/graylog-mcp/actions/workflows/codeql.yml/badge.svg)](https://github.com/ntbang0901/graylog-mcp/actions/workflows/codeql.yml)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)
![Graylog](https://img.shields.io/badge/graylog-4.3%20%E2%80%93%207.0-green)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

A read-only [Model Context Protocol](https://modelcontextprotocol.io) server for **Graylog 4.x to 7.x**.
It lets an LLM investigate incidents on its own: search logs, follow one request across services,
group errors with exact counts, and find when a problem started. Sensitive data is masked
before any log line leaves the machine.

Everything site-specific (trace field names, redaction rules, timezone, application packages,
error query) lives in configuration, not in code.

## How it differs from other Graylog MCP servers

|                    | Typical community server                         | graylog-mcp                                                    |
|--------------------|--------------------------------------------------|----------------------------------------------------------------|
| Graylog versions   | one range only (4.x/5.0 *or* 5.2+)               | detects the version, works on 4.x through 7.x                  |
| Statistics         | raw messages, or counting over a sample          | exact counts and groupings computed by Graylog                 |
| Safety             | no masking                                       | masks by field name, regex and Luhn; per-country packs         |
| LLM context        | dumps raw log lines                              | folds stack traces, groups repeated lines, hard size cap       |
| Writes to Graylog  | some create saved searches                       | never writes anything                                          |

## Quick start

The fastest way, inside your application repository:

```bash
uvx --from git+https://github.com/ntbang0901/graylog-mcp graylog-mcp init   # guided setup
uvx --from git+https://github.com/ntbang0901/graylog-mcp graylog-mcp ui     # or the admin web UI
```

`init` asks for each environment, tests the connection (you can paste a token for the test; it is never
saved), detects your field names from the logs, writes `.graylog-mcp.toml` and registers the server in your
MCP client. See [Setup helpers](#setup-helpers-and-admin-ui).

Or by hand:

Two environment variables are enough:

```bash
export GRAYLOG_URL=https://graylog.example.com
export GRAYLOG_TOKEN=<access token of a read-only user>
uvx graylog-mcp --check      # connects, prints the detected version and APIs, exits
```

Claude Code:

```bash
claude mcp add graylog --env GRAYLOG_URL=https://graylog.example.com --env GRAYLOG_TOKEN=... -- uvx graylog-mcp
```

Claude Desktop / any MCP client (`mcpServers` JSON):

```json
{
  "mcpServers": {
    "graylog": {
      "command": "uvx",
      "args": ["graylog-mcp"],
      "env": {
        "GRAYLOG_URL": "https://graylog.example.com",
        "GRAYLOG_TOKEN": "...",
        "GRAYLOG_TIMEZONE": "Asia/Ho_Chi_Minh",
        "GRAYLOG_REDACTION_PACKS": "vn"
      }
    }
  }
}
```

### The Graylog user

Create a dedicated user with the **Reader** role and read access to the streams the model may see,
then generate an access token for it. The server only sends `GET` requests, plus `POST` to the four
search endpoints that execute or check a query without saving it (`/views/search/sync`, `/search/messages`,
`/search/aggregate`, `/search/validate`). Any other method or path is refused inside the client before a request is built.

## Tools

All tools are annotated `readOnlyHint` and accept an optional `instance`.

**Scan**
- `scan` — "is anything wrong?" in one call. Runs every scan rule concurrently (crashes, resource exhaustion,
  error spikes, new error types, HTTP 5xx, connectivity, database, auth failures, plus your own) with exact counts
  against a baseline, and returns only what fired, most severe first, with its query, top groups and a sample.
  Ad hoc rules (`checks`) cover a specific request in the same call. See [Scan rules](#scan-rules).
- `list_scan_rules` — the rules with their query, condition, severity and tags.

**Root cause analysis**
- `root_cause` — "what broke first, and why?" in one call. Compares every service's errors, traffic and
  latency with a baseline window, pins each service's first error to the millisecond, detects deploys in
  the logs, infers the call graph from traces, and returns a ranked verdict with a timeline and evidence.
- `detect_changes` — deploys and restarts found in the logs themselves: a new value of a version field
  (`app_version`, `build`, `commit`, ...), a host rollout (new sources replacing old ones), start/stop lines.
  No CI/CD integration needed.
- `service_map` — which service calls which, inferred from sampled traces with no configuration: edges with
  traffic, error rate and p50/p95 latency.

**Search**
- `search_logs` — Lucene query, relative (`15m`, `2h`) or absolute time (instance timezone or ISO 8601),
  streams by name or id, field selection, sort, paging; repeated lines grouped by default.
- `count_logs` — exact number of matching messages.
- `get_message` — one message with all fields, by `index/id` ref.

**Investigate**
- `trace_request` — follows a correlation/request/trace id through the configured `trace_fields` on all
  streams (falls back to full text); returns a cross-service timeline, per-service steps with durations
  and the first error.
- `context_around` — messages within ±N seconds of a message, limited to the same source, the same
  streams or everything.
- `error_summary` — groups errors by exception, logger, source or any field: exact count, first and
  last seen, one sample message per group.
- `log_histogram` — counts over time with automatic interval, peak bucket and the onset of a spike.
- `top_values` — top N values of a field with exact counts.
- `compare_periods` — two periods (e.g. before/after a deploy): which error groups are new, grew,
  disappeared or shrank, normalised per hour.

**Discover**
- `list_streams`, `list_fields` — so the model writes queries with real names.
- `list_presets`, `run_preset` — named queries you define in the config.
- `list_instances` — instances with detected version and the API in use.

The server also sends the model instructions about Lucene syntax and a suggested investigation flow.

## Scan rules

`scan` answers "is anything wrong?" or "scan X" with one call. Each rule is a Lucene query plus a condition, and
fires when:

| Condition | Fires when | Use it for |
|-----------|-----------|------------|
| `threshold = N` | more than N matches in the window (`0`: any match) | things that should never happen: crashes, OOM, data corruption |
| `growth = X`, `min_count = M` | the rate is X times the baseline's (or the baseline had none), with at least M matches, and the rise is significant | things that always happen a little: timeouts, 5xx, auth failures |
| `group_by` + `new_groups = true` | a value of the field (exception, logger, ...) appears that the baseline never saw, at least `min_count` times | new kinds of errors after a deploy |

What keeps false alarms out:

- **Share of traffic** (`per_traffic = true`): the rule compares matches / traffic instead of matches per
  hour, so errors that triple because a sale tripled the traffic stay quiet, and a rise from 1% to 10% of a busy
  hour fires. Traffic is every message in the scan's scope, or `traffic_query` (e.g. `path:/checkout` for checkout
  errors). The built-in error, 5xx, connectivity and database rules use it.
- **Seasonal baseline** (`baseline_shift = "1d"` or `"7d"`, `baseline_periods = 3`): the window is compared with
  the same window one day (week) earlier, three times; the period with the median rate is the reference, so the
  morning peak is compared with yesterday's morning peak, and one bad day among the three changes nothing.
  Periods without any data (older than the index retention) are dropped and the finding says so
  (`baseline_note`). By default the baseline is the period right before the window, as long as the window, or
  `baseline = "24h"`.
- **Significance**: a growth fires only if it is unlikely to be chance, by an exact conditional binomial test of
  the window's count against the reference period's (the standard test comparing two Poisson rates). 3 errors
  then 7 (x2.3) stays quiet with a `note` saying so; 400 then 600 (x1.5, with `growth = 1.4`) fires. Each growth
  result carries its `confidence`; the rule's `confidence` (default 0.99) is the bar.

Every rule costs two exact counts, plus one per extra baseline period and for traffic; identical counts are made
once per scan (rules share the error query, the traffic) and run concurrently (`limits.scan_concurrency`). Groups
and one sample are fetched only for rules that fired or need them to decide, and a sample shown by one finding is
not repeated by the next. The result has `findings` (fired, most severe first), `quiet` (checked and normal, with
counts and trend) and `skipped` (could not be checked, e.g. a field these logs do not have).

Built-in rules: `crash` and `resource_exhaustion` (critical, any match); `error_spike` (share of errors x2),
`new_error_types` (exception unseen in 24 h), `http_5xx` (share x2, only where a status field exists),
`connectivity` (timeouts, refused/reset connections, DNS; share x3) and `database` (deadlocks, lock timeouts,
too many connections; share x3), all high; `auth_failures` (rate x3, at least 20), medium.

```toml
[scan]
disable = ["auth_failures"]                 # built-in rules to turn off
exclude = 'logger_name:HealthCheck OR "GET /health"'   # noise dropped from every rule

[scan.rules.connectivity]                   # a built-in: only the keys you set change
min_count = 30

[scan.rules.card_declined]
description = "Bank declines"
query = 'service:payment AND message:"declined"'
severity = "high"                           # critical | high | medium | low
growth = 2.0
min_count = 20
per_traffic = true                          # compare declines / payment traffic, not declines per hour
traffic_query = "service:payment"
baseline_shift = "7d"                       # against the same hour of the last 3 weeks (payday, weekends)
group_by = "bank_code"                      # top groups shown with the finding
tags = ["payment"]

[scan.rules.ledger_mismatch]
query = '"ledger mismatch"'                 # no condition: any match fires
severity = "critical"
instances = ["payment/prod"]                # instances, groups or environments; everywhere when omitted

[scan.rules.consumer_lag]
query = "consumer_lag:>10000"
requires = ["consumer_lag"]                 # skipped (and said so) where the field does not exist
```

Other keys: `errors_only = true` ANDs the instance's `error_query`, `exclude` drops noise for one rule, `baseline`
sets the rule's own baseline length, `baseline_periods` the number of shifted periods, `confidence` the
significance bar. A rule's own baseline wins over the call's. The config is validated at startup like the rest (unknown keys, a rule that
would fire on all traffic, `new_groups` without `group_by`).

Selecting what to run: `scan(rules=["payment"])` takes rule names or tags, `min_severity="high"` leaves out
lower rules before any request, and `checks=[{"name": "declined", "query": "message:declined", "growth": 2}]`
adds rules written for the request at hand (same keys as above). A preset with `tool = "scan"` saves a scan
the team runs often, and the `scan` MCP prompt (`/mcp__graylog__scan` in Claude Code) runs scan, verifies each
finding and reports them as a table.

Writing rules that are fast and accurate:

- Match on fields when you have them (`http_status:[500 TO 599]`, `exception_class:...`); otherwise quoted
  phrases (`"Connection refused"`). Avoid leading wildcards and regex: they scan every term in the index.
- Pick the condition from how often the event normally happens: never, then `threshold = 0`; always a little,
  then `growth` (the significance test handles noise; `min_count` only sets the smallest count worth a look).
- Anything that grows with traffic gets `per_traffic = true`; traffic with a daily or weekly curve gets
  `baseline_shift`.
- Drop known noise with `exclude` (per rule or under `[scan]`) rather than raising thresholds.
- Use a longer baseline (`baseline = "24h"`) for rules on spiky or low-volume traffic, and `requires` for rules
  that depend on a field only some systems log.
- Give each rule a severity that matches who should be woken up, and tags that match how people ask
  ("payment", "security", "dependencies").

## Root cause analysis

Example verdict, from the scenario in the integration suite (a bad deploy of `payment`), identical on
Graylog 4.3, 5.0, 5.2, 6.1 and 7.0:

> Most likely origin: payment (confidence high). errors began at 08:42:49.756, 182.5s after payment deployed
> 1.3.9 -> 1.4.0 (hosts pay-1, pay-2 -> pay-3, pay-4). Also affected: gateway errors (+50ms), bank-adapter
> traffic drop.

How it gets there:

1. **Signals per service.** Two pivots (errors, and traffic with average latency) split by service over the
   incident window plus a baseline window right before it.
2. **Onsets.** For each service, the first interval that is clearly abnormal against its own baseline
   (median and MAD, so one noisy minute in the baseline does not hide anything) and stays abnormal: error
   rise, traffic drop or spike, latency rise. Error onsets are then refined to the exact first error message.
3. **Changes.** Version fields, host rollouts and start/stop lines (see `detect_changes`) in the window and
   the 24 hours before it. A service that merely went silent is reported as a traffic drop, not a change.
4. **Call graph.** Traces sampled per service (and from failing requests) give caller -> callee edges.
5. **Ranking.** Earliest onset (exact timestamps break ties inside the first interval), a change shortly
   before the onset, callers failing after it, and a dependency failing *before* it (which points further
   down) all move the score. The result lists the reasons for each candidate, a merged timeline, the first
   error message with its `ref`, and the next calls to verify the hypothesis.

It is a ranked hypothesis with its evidence, not a certainty. Field names come from config:
`service_fields`, `trace_fields`, `version_fields`, `latency_fields`, `change_query`.

## Version detection

At startup the server reads `GET /api/system` (falling back to `GET /api/` when the token may not read
system info), picks the APIs below and caches the result; `list_instances` shows the choice. An API that
answers 404/405/501 is dropped and the next one is used.

| Version   | Messages                                    | Aggregations                                   |
|-----------|---------------------------------------------|------------------------------------------------|
| 4.x – 5.1 | `GET /search/universal/absolute` → views     | `POST /views/search/sync` (pivot)              |
| 5.2 – 6.x | universal → views → Scripting API            | `POST /search/aggregate` → views pivot         |
| 7.x       | same as 5.2+                                 | same as 5.2+                                   |

Pivot requests use `"field": "x"` on 4.x and `"fields": [...]` from 5.0. Histograms always use a views
pivot with a time grouping. Query syntax errors and zero-result queries are checked with
`POST /search/validate` so the model gets the actual problem ("incomplete query", "unknown field: sevrity")
instead of a bare "all shards failed". Graylog 7 ships its own MCP endpoint; this server is still useful
there for masking and compact output.

Every tool, each API path forced on its own (universal, views, Scripting API), the read-only guarantee and
the no-leak check pass against real containers of:

| Graylog      | Search backend   | Result |
|--------------|------------------|--------|
| 4.3.15       | OpenSearch 1.3   | pass   |
| 5.0.13       | OpenSearch 2.15  | pass   |
| 5.2.12       | OpenSearch 2.15  | pass   |
| 6.1.16       | OpenSearch 2.15  | pass   |
| 7.0.13       | OpenSearch 2.15  | pass   |

Behaviour observed on real servers and handled: 5.x+ pivots return an `(Empty Value)` bucket for documents
without the grouped field (reported separately as `without_field`); the Scripting API returns `"-"` for
missing fields and no message index; invalid queries come back as HTTP 500 from universal search on
4.x-5.x; universal search is still present in 7.0.

## Output shaping

- **Masking** (before anything else):
  - always on: sensitive field names (`password`, `token`, `secret`, `authorization`, `cookie`, ...),
    emails, `Bearer`/`Basic` credentials, JWTs, `key=value` / `"key": "value"` secrets, credentials in
    URLs, PEM private keys, card numbers that pass the Luhn check;
  - packs enabled in config: `vn` (phone numbers, 12-digit CCCD, optional 9-digit CMND), `us` (SSN),
    `eu` (IBAN with checksum), `uk` (NINO), `in` (Aadhaar, PAN);
  - your own regexes, plus an allow-list and excluded field names to avoid false positives.
  Grouping keys (`top_values`, `error_summary`) are masked too.
- **Stack traces** (Java/Kotlin, Python, .NET, Go, Node): exception lines, `Caused by` blocks and frames
  from `app_packages` are kept; the rest becomes `… N frames`. Without `app_packages`, the first N frames
  are kept (the last N for Python).
- **Repeated lines**: numbers, UUIDs, hex, IPs and timestamps are normalised and identical templates
  grouped with `count`, `first`, `last` and one sample.
- **Size**: per-value and per-call character caps, with `truncated` and `next_offset`.
- **Time**: shown in the configured timezone with its offset. Each line carries `ref: index/id` for
  follow-up calls.
- **Errors**: clear messages for 401, 403, query syntax errors (with position), timeouts, unknown streams
  (with suggestions) and unsupported versions.

## Configuration

Environment variables:

| Variable | Purpose |
|----------|---------|
| `GRAYLOG_URL`, `GRAYLOG_TOKEN` | single instance with token auth |
| `GRAYLOG_USERNAME`, `GRAYLOG_PASSWORD` | basic auth instead of a token |
| `GRAYLOG_VERIFY_TLS`, `GRAYLOG_CA_BUNDLE`, `GRAYLOG_PROXY` | TLS and proxy for the env-only instance |
| `GRAYLOG_TIMEZONE` | display timezone and zone for naive times (default UTC) |
| `GRAYLOG_REDACTION_PACKS` | e.g. `vn,eu` |
| `GRAYLOG_APP_PACKAGES` | e.g. `com.acme,/srv/app/` |
| `GRAYLOG_MCP_CONFIG` | path of a TOML config file |
| `GRAYLOG_MCP_HTTP_TOKEN` | bearer token required by the HTTP transport |

A TOML file (`--config` or `GRAYLOG_MCP_CONFIG`, else `.graylog-mcp.toml` in the current directory or a
parent up to the repository root, else `~/.config/graylog-mcp/config.toml`) covers the
rest: several instances, token or basic auth, TLS verification and CA bundle, proxy, timezone,
`trace_fields`, `error_query` (because `level` is a syslog number or a string depending on how logs are
shipped), `app_packages`, redaction packs and custom patterns, presets and limits. Secrets are never
written in the file; it names the environment variable that holds them (`token_env`, `password_env`).
The file is validated at startup and any mistake (unknown key, bad regex, unknown timezone, missing
secret) stops the server with a precise message.

See [`examples/config.toml`](examples/config.toml) for every option.

## One repository, several environments

Commit a `.graylog-mcp.toml` at the root of your application repository with one instance per environment.
The server finds it from the current directory or any parent up to the repository root, so everyone working
in the repo gets the same setup. Tokens stay out of the file: each instance names its environment variable.

```toml
# .graylog-mcp.toml
default_instance = "staging"          # what tools use when no environment is named
timezone = "Asia/Ho_Chi_Minh"

[redaction]
packs = ["vn"]

[investigation]
trace_fields = ["traceId", "X-Request-ID"]

[instances.dev]
url = "https://graylog-dev.example.com"
token_env = "GRAYLOG_DEV_TOKEN"
description = "Development cluster, noisy, debug logs on"

[instances.staging]
url = "https://graylog-staging.example.com"
token_env = "GRAYLOG_STAGING_TOKEN"
description = "Staging, deployed on every merge to main"

[instances.prod]
url = "https://graylog.example.com"
token_env = "GRAYLOG_PROD_TOKEN"
description = "Production"
error_query = "level:<=3 AND NOT logger_name:healthcheck"   # any key can differ per environment
```

Register the server once for the project (Claude Code reads `.mcp.json` at the repo root and expands
`${VAR}` from each developer's environment):

```json
{
  "mcpServers": {
    "graylog": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/ntbang0901/graylog-mcp", "graylog-mcp"],
      "env": {
        "GRAYLOG_DEV_TOKEN": "${GRAYLOG_DEV_TOKEN}",
        "GRAYLOG_STAGING_TOKEN": "${GRAYLOG_STAGING_TOKEN}",
        "GRAYLOG_PROD_TOKEN": "${GRAYLOG_PROD_TOKEN}"
      }
    }
  }
}
```

Then ask in plain words: *"why is checkout failing on staging?"*, *"compare errors on prod before and
after 14:00"*. Every tool takes `instance`, `list_instances` shows each environment with its description
and detected Graylog version (environments may run different versions), and results always name the
instance they come from. A developer who has no token for an environment simply gets a clear error for that
one; the others keep working.

Clients that do not start the server inside the repository (Claude Desktop) need the path explicitly:
`GRAYLOG_MCP_CONFIG=/path/to/repo/.graylog-mcp.toml`.

## Setup helpers and admin UI

| Command | What it does |
|---------|--------------|
| `graylog-mcp init` | Guided setup: environments, connection test, field detection, config file, client registration. `--yes --env staging=https://... --env prod=https://...` for scripts. |
| `graylog-mcp login [INSTANCE...]` | Asks for each missing token/password, tests it, and saves it for this user in `~/.config/graylog-mcp/secrets.toml` (owner-only permissions, outside any repository). The server reads environment variables first, then this file. `graylog-mcp logout` forgets them. |
| `graylog-mcp doctor` | Checks every environment (token set, reachable, TLS, version, readable streams, data, configured fields exist, error query matches, redaction) and prints a fix for each problem. Exit code 1 on failure, `--json` for CI. |
| `graylog-mcp detect` | Suggests `service_fields`, `trace_fields`, `version_fields`, `latency_fields`, `error_query` (syslog numbers or words), exception/logger fields and `app_packages` from the logs, with coverage and redacted sample values. `--apply` writes them. |
| `graylog-mcp install <client>` | Registers the server in `claude-code` (`.mcp.json`), `cursor`, `vscode` or `claude-desktop`, merging with existing entries and keeping a `.bak`. Tokens are referenced as `${VAR}` / `${env:VAR}`, never written (Claude Desktop gets placeholders unless `--with-secrets`). |
| `graylog-mcp ui` | Local admin web UI (below). |

`graylog-mcp ui` opens a page on `127.0.0.1` with:

- **Overview**: environment cards and the doctor checks with fixes;
- **Environments**: add, edit, delete, set default; type the token or password once, test the connection, and
  save (the secret goes to your per-user secrets file, the variable name is chosen for you);
- **Field mapping**: run detection on an environment, review the evidence, apply the selected settings;
- **Redaction**: toggle country packs, add custom patterns and allow-list entries, and see live which rules
  mask your sample text;
- **Playground**: run any tool against any environment and see the exact output, its size and an
  approximate token count;
- **Connect clients**: ready-to-copy snippets and one-click install for each client;
- **Config file**: edit the TOML with validation and an automatic backup.

The UI listens on loopback only, checks the Host header, and every API call needs the random token in the
link printed at startup. It writes the config file and client configs when you ask. A token or password
entered in the form is saved, when you tick "Save it on this machine", to the same per-user secrets file as
`graylog-mcp login`, never to the config file.

`*_env` settings hold the **name** of a variable (`GRAYLOG_PROD_TOKEN`), never the secret: a value that is not
a valid variable name is refused, and never echoed back, since it is most likely a secret typed into the wrong
field.

## Groups and your own environments

Large organisations often run one Graylog per system *and* per environment: ERP, CXP, PAYMENT... each with
its own dev/uat/prod (or sandbox, dr, prod-eu...). Environment names are yours; nothing assumes
dev/staging/prod, and each group can have a different set.

```toml
# graylog-org.toml: one file for the whole company, e.g. in a shared platform repository
timezone = "Asia/Ho_Chi_Minh"
default_environment = "uat"            # used when a question names only the system

[environments.prod]                    # declare environments once; every group inherits these settings
description = "Production"
error_query = "level:<=2"
ca_bundle = "/etc/ssl/corp-ca.pem"
[environments.uat]
description = "User acceptance"

[groups.erp]
description = "ERP"
trace_fields = ["correlationId"]       # anything set on a group applies to all its environments
[groups.erp.environments.uat]
url = "https://graylog-erp-uat.corp"
token_env = "GRAYLOG_ERP_UAT_TOKEN"
[groups.erp.environments.prod]
url = "https://graylog-erp.corp"
token_env = "GRAYLOG_ERP_PROD_TOKEN"

[groups.payment]
description = "Payment platform"
default_environment = "sandbox"
[groups.payment.environments.sandbox]
url = "https://graylog-pay-sbx.corp"
token_env = "GRAYLOG_PAYMENT_SANDBOX_TOKEN"
[groups.payment.environments.prod]
url = "https://graylog-pay.corp"
token_env = "GRAYLOG_PAYMENT_PROD_TOKEN"
```

Settings are layered: `[environments.<env>]` < `[groups.<group>]` < `[groups.<group>.environments.<env>]`.
Each pair becomes an instance named `<group>/<environment>`.

A service repository then only says which group it belongs to:

```toml
# payment-api/.graylog-mcp.toml
include = "../platform/graylog-org.toml"   # relative to this file; a list is allowed
default_group = "payment"
only_groups = "payment"                    # load only this group here (a list, or "*" for every group)
```

How the model (and you) pick an instance, in every tool's `instance` argument:

| You say | Instance |
|---------|----------|
| `payment/prod`, `payment prod`, `PAYMENT:prod` | `payment/prod` |
| `payment` | the group's `default_environment` (else the global one) |
| `prod` | `prod` of the `default_group`; an error listing the options if several groups have it |
| nothing | `default_instance`, else `default_group` + `default_environment` |

`list_instances` returns the groups with their environments and descriptions, `graylog-mcp init` asks for
groups first, and the admin UI shows environments by group (instances from an included file are marked and
edited in that file).

### Repositories of a group

List the repositories each group serves, as local folders (paths relative to the config file, `~` allowed) or
git remotes (`git@gitlab.corp:f88/payment-api.git`, `f88/payment-api` or just `payment-api`):

```toml
[groups.payment]
repos = ["~/code/payment-api", "../payment-worker", "f88/payment-gateway"]
```

When the server runs inside one of them (a folder or any subfolder, or a clone whose `origin` matches), it
loads **only that group**: "errors on prod" means that system's production, and the other groups' Graylog
servers are not reachable from that repository. Asking for one returns an error that says why and how to
enable it. `list_instances` shows the repository and the `scope`; `graylog-mcp doctor` shows which repository
was recognised.

To reach more groups from a repository, set `only_groups = ["payment", "erp"]` (or `"*"` for all) in its
`.graylog-mcp.toml`, or `GRAYLOG_MCP_GROUPS=payment,erp` in the MCP client's environment. `only_groups` also
works without repositories, e.g. in a personal config. The admin UI always shows every group.

Manage them in the admin UI (Environments > Groups > Repositories: add a local folder, see its git remote and
whether it is set up, "Set up" writes `.graylog-mcp.toml` with an `include` of the shared file and
`only_groups`, so the scope holds on every machine whatever its folder layout, and registers
Claude Code there) or from the command line:

```bash
graylog-mcp repo --config ../platform/graylog-org.toml add payment   # current folder; also sets it up
graylog-mcp repo list
graylog-mcp repo remove payment ~/code/payment-api
```

### Focus: search this repository's service by default

Inside a repository, "errors in the last hour" means that service's errors. `search_logs`, `count_logs`,
`error_summary`, `log_histogram`, `top_values`, `compare_periods` and `detect_changes` add a filter on the
service field and say so in their result (`focus`). Other services and streams are searched only when asked for:
`streams=["*"]` (everything), explicit `streams`, or a query that names a service field
(`application:"other-service"`). `trace_request`, `service_map` and `root_cause` always span every service.

Each repository sets its focus in its own `.graylog-mcp.toml`:

```toml
[focus]
service = "cobra-mdm-service"   # value of the service field; a list for several; false = no service filter
streams = ["MDM"]               # optional: streams searched by default
# field = "application"         # optional: the service field (default: the first of service_fields present)
```

Without `[focus]`, the service is guessed from the repository name (folder or git remote) by matching it against
the values of the service fields (`service`, `application`, ...) over the last 24 hours: `cobra-mdm` finds
`cobra-mdm-service`. No clear match, no filter; `list_instances` shows what was picked. Set it from the admin UI
(Groups & repositories > Focus), with `graylog-mcp repo focus cobra-mdm-service --streams MDM` inside the
repository, or for one MCP client with `GRAYLOG_MCP_SERVICE=<name>` (`off` disables it).

### Settings by scope

The admin UI's **Settings** page edits field names, queries and connection options for a chosen scope: global,
one environment (for every group), one group, or a single instance. Each field shows the value currently in
effect; leaving it empty inherits it.

## One process for every session

With stdio, the client starts a server process for every session: ten Claude Code sessions are ten
Python processes, each with its own connections and version detection. `--shared` runs one server for all
of them, and still answers each session with the configuration of the repository it works in (its
`.graylog-mcp.toml`, group, focus), as if it had been started there:

```bash
graylog-mcp login                     # once: tokens saved on this machine, read by the shared server
graylog-mcp serve --shared            # keep it running (127.0.0.1:8000); see below to start it at login
graylog-mcp install claude-code --shared   # in each repository: .mcp.json connects to it instead of uvx
```

The entry it writes sends the repository folder in a header, which the client fills in:

```json
{ "mcpServers": { "graylog": {
  "type": "http", "url": "http://127.0.0.1:8000/mcp", "headers": { "X-Graylog-MCP-Repo": "${PWD:-}" } } } }
```

Cursor and VS Code send `${workspaceFolder}`; other clients can add `?repo=<folder>` to the URL. `repo add
--shared` and `init --source shared` write the same entry; for every project at once:
`claude mcp add --transport http graylog --scope user http://127.0.0.1:8000/mcp --header 'X-Graylog-MCP-Repo: ${PWD:-}'`.

- One Graylog client per instance, shared by every repository using it: one connection pool, one version
  detection, one stream and field cache.
- A repository's config is read again every few seconds when used; edits apply without a restart.
- `--config FILE` (or `GRAYLOG_MCP_CONFIG`) uses that file for every repository; the group and focus still
  follow the repository. Without a header, a session gets the config of the folder the server runs in.
- Tokens come from the server's environment or `graylog-mcp login`, not from the client.
- Claude Desktop already runs one server for all its chats and keeps stdio.

To start it at login on Linux (systemd user unit; on macOS a LaunchAgent running the same command):

```ini
# ~/.config/systemd/user/graylog-mcp.service, then: systemctl --user enable --now graylog-mcp
[Service]
ExecStart=%h/.local/bin/uvx --from git+https://github.com/ntbang0901/graylog-mcp graylog-mcp serve --shared
Restart=on-failure

[Install]
WantedBy=default.target
```

## Shared HTTP server and Docker

stdio is the default. For one server shared by a team, use streamable HTTP with its own bearer token:

```bash
GRAYLOG_MCP_HTTP_TOKEN=$(openssl rand -hex 32) graylog-mcp --transport streamable-http --host 0.0.0.0 --port 8000
```

The server refuses to listen on a non-loopback address without a token (unless `--allow-no-auth` is
given, e.g. behind an authenticating proxy). `/healthz` is open; the MCP endpoint is `/mcp`.

```bash
docker build -t graylog-mcp .
docker run -p 8000:8000 \
  -e GRAYLOG_URL=https://graylog.example.com -e GRAYLOG_TOKEN=... \
  -e GRAYLOG_MCP_HTTP_TOKEN=... graylog-mcp
```

## Testing

```bash
uv sync
uv run pytest              # unit + contract tests (no network), coverage >= 85%
uv run ruff check && uv run ruff format --check && uv run mypy
```

- **Unit tests**: masking (including values that must *not* be masked), stack traces in five languages,
  line grouping, time parsing, pivot building and parsing, config validation, HTTP auth.
- **Contract tests**: every tool against an in-memory Graylog (`tests/fake_graylog.py`, served through
  `httpx.MockTransport`) that reproduces the response shapes of 4.3, 5.0, 5.2, 6.1 and 7.0, with planted
  sensitive values that must never reach the output, an assertion that no request could change state,
  and a size budget per call.
- **Integration tests**: `tests/integration/docker-compose.yml` starts Graylog 4.3, 5.0, 5.2, 6.1 and 7.0,
  each with MongoDB and OpenSearch; `seed.py` ships the same GELF dataset, then every tool runs against it:

  ```bash
  tests/integration/run.sh v50      # or v43 v52 v61 v70
  ```

  CI runs the whole matrix weekly, on demand, and on pull requests that change the backends.

CI on every push: ruff, mypy, tests on Linux (Python 3.11-3.13), Windows and macOS, wheel build with
metadata check and a smoke test in a clean environment, and a Docker build with an HTTP smoke test. CodeQL
scans the code and workflows; Dependabot keeps dependencies and actions current. See
[CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## Project layout

```
src/graylog_mcp/
  config.py     environment + TOML, validated at startup (fail fast)
  client.py     async httpx client: auth, TLS, proxy, read-only guard, HTTP error mapping
  backends/     version detection and API selection: universal.py, views.py, scripting.py
  timerange.py  time parsing, display, interval selection
  redact.py     core rules, country packs, Luhn / IBAN checks
  shaping.py    field selection, truncation, stack traces, line grouping, output budget
  tools.py      tool implementations
  rca.py        root cause analysis: onsets, change detection, service map, ranking
  server.py     MCP tool declarations and model instructions
```

## License

MIT
