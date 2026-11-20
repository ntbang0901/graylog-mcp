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

One command (needs [uv](https://docs.astral.sh/uv/)), run in the folder holding your `.graylog-mcp.toml` or
anywhere for a fresh start:

```bash
uvx --from git+https://github.com/ntbang0901/graylog-mcp graylog-mcp start
```

It installs graylog-mcp, runs it in the background as **one process for every Claude session**, starts it
again when you log in, connects Claude Code in the repositories of your groups and opens the admin page. Its **Get set up** card
shows what is left; **Finish setup** does every step that needs no input in one click and then opens the form
for what does (usually: your Graylog URL and a token).

Afterwards:

| | |
|---|---|
| `graylog-mcp ui` | open the admin page again |
| `graylog-mcp status` | is it running, which version, is Claude Code connected |
| `graylog-mcp update` | install the latest version and restart (or "Check for updates" on the page) |
| `graylog-mcp version` | the version of this copy and, when installed from git, its commit |
| `graylog-mcp stop` | stop it and no longer start it at login |

Prefer the terminal? `graylog-mcp init` is a guided setup that writes `.graylog-mcp.toml` and registers the
server per repository (one process per session). See [Setup helpers](#setup-helpers-and-admin-ui).

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

All tools are annotated `readOnlyHint` and accept an optional `instance`. Tools that do several related things
take a `kind`, `view` or `what`; an argument that does not apply to the one chosen is refused, not ignored.

**Scan**
- `scan` — "is anything wrong?" in one call. Runs every scan rule concurrently (crashes, resource exhaustion,
  error spikes, new error types, HTTP 5xx, connectivity, database, auth failures, plus your own) with exact counts
  against a baseline, and returns only what fired, most severe first, with its query, top groups and a sample.
  Ad hoc rules (`checks`) cover a specific request in the same call. See [Scan rules](#scan-rules).

**Root cause analysis**
- `root_cause` — "what broke first, and why?" in one call. Compares every service's errors, traffic and
  latency with a baseline window, pins each service's first error to the millisecond, detects deploys in
  the logs, infers the call graph from traces, and returns a ranked verdict with a timeline and evidence.
  - `view="changes"` — only the deploys and restarts found in the logs themselves: a new value of a version
    field (`app_version`, `build`, `commit`, ...), a host rollout (new sources replacing old ones), start/stop
    lines. No CI/CD integration needed.
  - `view="service_map"` — only which service calls which, inferred from sampled traces with no configuration:
    edges with traffic, error rate and p50/p95 latency.

**Search**
- `search_logs` — Lucene query, relative (`15m`, `2h`) or absolute time (instance timezone or ISO 8601),
  streams by name or id, field selection, sort, paging; repeated lines grouped by default.
- `get_message` — one message with all fields, by `index/id` ref. Long values are cut and listed in `cut_fields`;
  name them in `fields` to read them in full. With `context_seconds`: the messages within ±N seconds of it
  instead, limited to the same source, the same streams or everything (`scope`).

**Investigate**
- `aggregate` — exact counts computed by Graylog:
  - `kind="count"` — number of matching messages.
  - `kind="histogram"` — counts over time with automatic interval, peak bucket and the onset of a spike.
  - `kind="top"` — top N values of the `group_by` field with exact counts.
  - `kind="errors"` — groups errors by exception, logger, source or any field: exact count, first and
    last seen, one sample message per group.
- `trace_request` — follows a correlation/request/trace id through the configured `trace_fields` on all
  streams (falls back to full text); returns a cross-service timeline, per-service steps with durations
  and the first error.
  - A business code (an order or transaction number) works too: when the messages holding it carry a trace id,
    the result lists the requests the code went through (`traces`, at most 10), details the one with an error or
    the most services (`shown_trace`) and adds the messages that hold the code without a trace id (`unlinked`).
    A code whose shape the [glossary](#glossary-your-teams-words) knows is looked up in the fields named there
    before the text is searched, so a number that also appears in unrelated lines leads only to its own requests;
    `code` names the kind of code and the fields that held it.
  - Business errors (`business_error_query`, by default HTTP 4xx) are marked apart from technical ones, with the
    same definitions as the Logs screen: `error` on the line, `business_errors` per service and step,
    `first_business_error`.
  - A trace longer than `limit` keeps its error lines (the earliest `limit` of each kind, at least 50) and its
    last line next to the earliest messages; `hidden` counts what is left out.
  - A request whose services log the id under different trace fields is followed through all of them.
  - When nothing is found, one look in a wider range says how many messages match there and when the newest was
    logged (`suggestions`).
- `compare_periods` — two periods (e.g. before/after a deploy): which error groups are new, grew,
  disappeared or shrank, normalised per hour.

**Discover**
- `describe` — `what="instances"` (default: detected version and the API in use), `"streams"`, `"fields"` (so
  the model writes queries with real names), `"presets"` or `"scan_rules"` (query, condition, severity, tags).
- `run_preset` — runs a named query you define in the config. A preset's `tool` keeps the names of the
  underlying operations: `scan`, `root_cause`, `detect_changes`, `service_map`, `search_logs`, `count_logs`,
  `error_summary`, `log_histogram`, `top_values`, `trace_request` or `compare_periods`.

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

## Searches that find nothing

`search_logs` and `aggregate` with `kind="count"` do not stop at "no matches". Four decisions, each explained in the result:

1. **Safe rewrite, before the search runs**, only for a query that cannot match as written: a field this Graylog
   does not have but with one clear counterpart (`Service:payment` -> `service:payment`; the instance's configured
   name for it, `exception:` -> `exception_class:` from `group_fields`; the one field ending in that word,
   `status:500` -> `http_status:500`; or an alias learned below), or a level word on a numeric level field
   (`level:ERROR` -> `level:3`). The result carries `rewritten: {from, ran, why}`.
2. **Counted suggestions, after a search found nothing**: a misspelled field (close names and known synonyms
   such as `app` -> `service`), the level in the other form, another letter case (keyword fields are
   case-sensitive), a prefix wildcard (`OrderNotFound` -> `OrderNotFound*`), lower-case `and` / `or` (Lucene
   searches them as words), a bare id (`req-7f3a9c`, a UUID) also looked for in the `trace_fields`, outside the
   repository's focus, a wider range (with `last_seen`, the time of the newest match). Each is counted exactly by
   Graylog (in parallel, bounded); the ones that find something come back in `suggestions` with their count and
   reason, best first, and the hint names the one to run.
3. **Learning from use.** A suggestion counts as offered; it counts as used when the model then runs it and
   finds something. A fix's weight is the mean of Beta(1 + used, 1 + offered - used), so it needs evidence and
   loses weight when it is ignored. A fix used at least twice with weight >= 0.75 applies by itself on that
   instance: a field alias becomes a safe rewrite, and a case or prefix fix reruns the empty search. Focus and
   range are only ever suggested, since they change the question.

4. **Diagnosis, after a search found nothing**, in `diagnosis`: the counts of each top-level `AND` clause, the
   ones that match nothing on their own (or that each matches but not together), the values the field of such a
   clause really has in the range with the close ones (`service:paymnt` -> did you mean `payment`; numbers are
   never guessed), and the dead clauses that match on their own further back.

Learned fixes live in `~/.config/graylog-mcp/learned.json`, per instance; the admin page (Activity > Learned) shows
each one with its counts, weight and whether it applies by itself, and forgets one on request. `GRAYLOG_MCP_LEARN=off` stops learning and applying learned fixes; the deterministic fixes stay.

## Glossary: your team's words

People who know the business say "thanh toán", "refund" or "order ORD-1001", not `service:payment`. The glossary
is where a team writes down once what its words stand for in the logs:

```toml
[glossary.terms.payment]
description = "payment service"
aliases = ["thanh toán", "payment"]
query = "service:payment"            # ANDed into the search when a question names the term

[glossary.terms.refund]
aliases = ["hoàn tiền", "refund"]
keywords = ["refund"]                # the English words the logs use for it
preset = "refund_errors"             # optional: one of your [presets] about this term

[glossary.codes.order]
pattern = 'ORD-\d+'
fields = ["order_id"]                # searched before the text
```

A term needs `aliases` and at least one of `query`, `keywords` and `preset`. Only `query` narrows a search today;
`keywords` and `preset` are listed for the model of a chat client, which can search for the words or run the preset
itself. How a question is read:

- An alias matches as whole words, whatever the case and spacing: "refund" does not match "refunded".
- The longest phrase wins: with both "cổng thanh toán" and "thanh toán" in the glossary, a question about "cổng
  thanh toán" names only the first.
- Accents are optional when typing: "thanh toan" matches "thanh toán". Words typed with accents only match the
  alias with those accents, and words typed without any that fit two terms are reported, not guessed. Both places
  of a tone mark are the same word: "hoá đơn" and "hóa đơn" need one alias.
- One alias means one term: the same alias on two terms of one instance stops the load, naming both. Systems that
  use one word for different things limit each term with `instances` (instance, group or environment names), as
  scan rules do.

Who reads it:

- **`describe(what="glossary")`** lists it, so the model of a chat client can turn the user's words into the
  right query itself.
- **`trace_request`** looks a code up in the `fields` of its kind before searching the text. A bare number such as
  `100200` also appears in durations, amounts and ids of other things; in `order_id` it is only the order.
- **`ask`** turns a term into a filter and says so in its answer, and finds codes by their `pattern`
  (see [below](#ask-in-plain-language)).

Keep an alias to what its term really covers. "Checkout" usually spans more than the payment service; pointing it
at `service:payment` gives answers that look right and miss logs. The line "… is understood as …" in `ask`'s answer
is where a wrong mapping shows.

## Ask in plain language

For people who do not write Lucene or know field names. `ask` takes a question, works out what it means, finds the
error it describes and follows that error's trace across services:

```
ask("Why did checkout fail with a bank connection error on prod this morning?")
ask("Thanh toán bị lỗi timeout trên prod trong 1 giờ qua, đi qua những service nào?")
ask("How many errors were there yesterday?")
```

It **needs no model and no key**: the question is read by rule, and with no provider configured nothing leaves
the machine but the read-only requests to Graylog. A model is an option you add (see [Providers](#providers)); it
is then asked only for what the rules could not read. Either way the answer is built from the existing tools and
fixed sentences, and starts with what was understood, so the asker can correct it:

```
Understood: prod, "trong 15 phút qua" (2026-11-19 10:15 to 10:30), technical and business errors.
"thanh toán" is understood as payment service, filtered by service:payment.
```

**What is read by rule.** Six things are read from the question itself:

| Read by rule | Examples | Set in |
|---|---|---|
| What is asked | "how many", "bao nhiêu": a count; "so với", "what changed": a comparison; "nguyên nhân", "which service broke first": the root cause; "các lỗi chính", "top errors": a summary; "xem log", "show me": log lines; a code: its history | `[ask.intent_words]` |
| The environment | "ở prod", "on production", "shop dev", an alias such as "môi trường thật" | instance names, groups, `aliases` |
| Time, as an exact range in the instance's timezone | "15 phút qua", "last 2 hours", "sáng nay", "yesterday afternoon", "tuần trước", "từ 9h đến 11h", "lúc 9h tối qua", "since 9am", "ngày 12/11", "từ 12/11 đến 15/11", "ngày 12 tháng 11" | `[ask.time]` |
| Kind of error | "lỗi" or "error" alone: technical and business; "timeout", "exception", "sập": technical; "bị từ chối", "declined", "invalid": business | `[glossary.error_words]` |
| The team's terms | "thanh toán" as `service:payment` | `[glossary.terms]` |
| Codes | `ORD-1001` | `[glossary.codes]` |

Time phrases work with or without Vietnamese accents (in a question typed with accents, a bare word stays what
it is: "ngay" is not "ngày"). "Last week" and "tuần trước" are the previous calendar week, "past week" and "tuần
qua" the last 7 days; a single clock time ("lúc 9h") is half an hour either side, and an hour said with its part
of the day is the hour of that part ("9h tối" is 21:00); a day without a year is the last such day. A phrase that
is only half understood is not read at all, because a wrong period is searched in silence: "vài ngày qua" (how
many?), a date that does not exist, or "12/11" with nothing saying it is a day (write "ngày 12/11", or add the
year; alone it is as much a share as "3/4"). A question that names no period covers the last 24 hours, and the
first line says so. One whose words of time the rules cannot place ("tuần rồi", "5m ago") is passed to a provider
when there is one, which picks one of five periods (asked back when it is unsure); without one the last 24 hours
are searched and the summary says a period was named but not read.

**Sure, guessed, or asked back.** Words that name what is asked ("how many", "so với", "nguyên nhân") are read for
sure, and no provider is asked about them. A question with only an error word ("thanh toán bị timeout") is most
likely about following that error, and one with only a term of the team ("thanh toán hôm nay") about its log
lines: those readings are **guesses**. With a provider the guess is checked with it. Without one the question is
answered as guessed, `guessed` in the result names the reading and the others it could be, and the summary says
how to ask for those ("Read as a request to follow one error across services; that is a guess. If you meant
something else, ask for it in words: list the main errors ("what are the main errors of …"); …"). A question
nothing can be read from ("Is the system ok?") is asked back: `clarify`, with up to three `readings`.

The environment: named by the caller, else read from the question, else the default one. Words narrow each other
("shop prod"). A word that still fits several instances ("prod" when two groups have one and none is the default
group), or words that exclude each other ("prod and dev"), are not guessed: `clarify` lists the instances in
`options`. A provider, when there is one, is asked to choose only among those; it is never asked for an
environment the question does not name. `aliases = ["môi trường thật"]` adds words for an instance; set on a
group or on `[environments.prod]` they are words for all of its instances, and the levels add up. "production",
"development" and "stg" are known for environments named `prod`, `dev` and `staging`.

On 90 questions written by another hand than the rules', without sight of them
(`tests/data/held_questions.py`), the rules read what is asked right in 86%; the 66 they read for sure are all
right, the guesses about half. The environment is read right in all of them. These are sample questions with
neutral names, not questions collected from users.

Technical and business errors are the instance's own definitions (`technical_error_query`, `business_error_query`;
by default the error query or HTTP 5xx, and HTTP 4xx). The kind is applied to counts, searches and the search for
a described error; a count of "errors" gives both kinds apart (`result.by_kind`). An instance that defines no
business errors is counted on technical ones and the summary says so. The error summary, the root cause analysis
and the comparison look at the instance's `error_query` alone: when the question named business errors, the
summary says they are not part of that answer.

`understood` in the result holds the same reading as data, with where each part came from (`sources`: `rules`,
`guess`, `caller`, `default`, or the name of the provider that chose it), and `ran` is the Graylog query that ran.

**Which error is meant.** Without a provider that may see the logs, the error groups of the period are matched by
the words of the question: a group that holds one of them is a match, its score the share of the words it holds
(`scored_by: "rules"`). Words that only carry the sentence, or that were read as the time, the environment or what
is asked, are not looked for; a term's `keywords` are. When no group holds a word, which is usual for a question
in Vietnamese about logs in English, the most frequent errors of the period are listed and the summary says the
words were not found: give the terms your team uses English `keywords` in the glossary, or add a provider. With a
provider that may score, it judges each group against the question (`scored_by` names it); when nothing in the
period matches it says so with the best score. Either way, two errors that match about equally are listed, not
picked between. A question about "errors" also gets each kind counted apart (`by_kind`).

An error without a trace id cannot be followed across services: `around` then holds the lines of the same source
within 30 seconds of it, and the hint says they are near in time, not one request. `cost` in the result counts the
requests made, next to the cap (`graylog_cap`, from `[limits] max_requests`, 20), and what each provider was sent
(`provider_requests`, and `providers` with the requests and bytes sent to each). A question that would go past the
cap is answered in part and says what it left out: "Checked 2 of 3 codes; 1 not checked: 15/20 Graylog requests
used."

`ask` reads the [glossary](#glossary-your-teams-words) itself, and hands nothing of it to a provider: the `query` of a term named in the
question is ANDed into the search, `understood.topics` lists the term, and the summary says how it was read
("thanh toán" is understood as payment service, filtered by service:payment). A term without a `query` is named
too, with the note that it does not narrow the search. Two terms with a `query` narrow together (only lines
matching both count), and the summary says so. Words typed without accents that fit two terms are not guessed: the
summary names both. Codes are found with `[glossary.codes]`, and a question about a code is not narrowed by a term,
because a code's history crosses services.

```toml
[ask]
# language = "en"                    # "en" or "vi": the language of the summary text
# min_confidence = 0.6               # a provider's choice below this is asked back
# relevance = 0.5                    # an error group a provider scores lower is "not what you described"
# code_patterns = ['ORD-\d+']        # older name of [glossary.codes]: lets "what happened to order ORD-123?" find the code
# max_candidates, send_chars         # error groups looked at, and characters of each a provider may see

[ask.intent_words]                   # a key given here replaces the built-in list of that key
# count = ["bao nhiêu", "how many"]  # also compare, root_cause, error_summary, follow_flow, search, trace_code

[ask.time]
# morning = [6, 12]                  # hours of "sáng" / "morning"; also noon [11, 14], afternoon [12, 18],
#                                    # evening [18, 24], night [22, 6]
# week_start = "monday"              # or "sunday"
# date_order = "DMY"                 # "MDY": 11/12 is 12 November

[glossary.error_words]               # a key given here replaces the built-in list of that key
# either = ["lỗi", "error", "failed"]
# technical = ["timeout", "exception"]
# business = ["bị từ chối", "declined"]
```

How the described error is found: every error line of the period when one read (`max_limit`, 500) holds them all;
else the newest lines of six equal parts of the period, so an error from its first hour is still a candidate.
Where the instance has the field of `group_fields.exception`, one aggregation adds every exception of the period,
also one no sampled line showed, with its exact count (`exception_count` on a match: the lines with that
exception, whatever their message). The count of a match itself is exact when every line was read; otherwise it is
scaled from the sample, marked `estimated` and written with a "~" in the summary. Set that field to what your logs
use (`graylog-mcp detect` suggests it). At most half of the size cap goes to the matches (`matches_omitted` counts
the rest). The traces of one error are looked up in one query of at most 500
messages. The trace and service fields are the instance's (`trace_fields`, `service_fields`).

### Providers

A provider is a model `ask` may ask for what its rules left open. None is needed, none is built in as a
requirement, and you can list several. It can be given two tasks (`can`):

- **understand**: choose what a question asks for, its period and its environment from lists the server gives,
  and give English words for the error it describes;
- **score**: judge which of the error groups of the period the question describes. This is the task that sees
  text from the logs.

A provider only chooses. Whatever it replies, `ask` keeps a choice only when it is one of those offered, a
probability only from 0 to 1, and words only when they are plain words: nothing a model writes becomes a field,
a filter or a query.

| `kind` | What it is | Settings |
|---|---|---|
| `jev` | TypeSafe's System One API (`POST /v1/systemone`) | `api_key_env`, `model`, `base_url` (https) |
| `openai` | Any OpenAI-compatible Chat Completions endpoint: a gateway, a model served on this machine, a hosted API | `base_url` (https, or http on `127.0.0.1` / `localhost`), `model`, `api_key_env` (optional) |
| `command` | A program on this machine, for instance a command-line client you are signed in to | `command` (the program and its arguments, as a list), `timeout` |

```toml
[ask]
provider_order = ["local", "jev"]     # who is asked first; [] turns every provider off

[ask.providers.local]
kind = "openai"
base_url = "http://127.0.0.1:4000/v1"
model = "acme-default"
# api_key_env = "ACME_GATEWAY_KEY"    # the name of the variable; leave out for an endpoint without a key
can = ["understand", "score"]         # the default

[ask.providers.jev]
kind = "jev"
can = ["score"]

[ask.providers.cli]
kind = "command"
command = ["acme-llm", "--json"]
can = ["understand"]
# timeout = 60                        # seconds; 30 for the two HTTP kinds
```

| `provider_order` | What happens |
|---|---|
| absent, and no `[ask.providers]` | As before this option existed: with a TypeSafe key saved, Jev is the one provider, for both tasks; without a key, the rules alone |
| `[]` | The rules alone, also when a key is still saved |
| a list | Those providers, in that order, each for what it `can`. A name that `[ask.providers]` does not declare is a configuration error, and so is `[ask.providers]` without a `provider_order` |

The first provider that can do a task is asked. The next one is asked **only when the one before failed**: it
could not be reached, answered an error, or replied nothing `ask` could read. A valid but unsure answer is not
taken to a second provider; `ask` asks back instead. So the text of one question reaches one place, unless that
place is down. A provider that fails never fails the question: the rules answer, `provider_failures` names the
provider and why, and the summary says so. A provider whose key is missing is such a failure, not a server that
does not start.

**The TypeSafe key.** `graylog-mcp login --ask` asks for the key without echoing it, tests it with one request
and saves it in `~/.config/graylog-mcp/secrets.toml` (or set `TYPESAFE_API_KEY` in the environment), then restart
the server. `--model NAME` and `--base-url https://...` on the same command save your choice of model and
endpoint; `GRAYLOG_MCP_ASK_MODEL` and `GRAYLOG_MCP_ASK_BASE_URL` do the same, and `[ask] model`, `base_url` and
`api_key_env` in a config file win over both. A provider of kind `jev` starts from these settings.
`graylog-mcp logout --ask` forgets the saved key, model and endpoint.

**What a provider is handed**, and what each environment allows. `provider_data` on an instance (or on a group,
or on `[environments.prod]` for every group's prod) says what of a question about it may be handed over:

| `provider_data` | Handed to a provider |
|---|---|
| `"none"` | Nothing. The rules and the glossary still answer |
| `"question_only"` | The redacted question and the lists to choose from (the kinds of question, five periods, and the names and descriptions of the instances a word of the question fits). Enough to understand; scoring is then done by rule, with the English words an `openai` or `command` provider gives for the error (Jev chooses and scores, it gives no words) |
| `"question_and_logs"` (default) | Also up to `max_candidates` (30) error groups, each as service, exception and message, redacted like every other output and cut to `send_chars` (250) characters |

Never raw logs, field values the redactor masks, anything from the glossary, or anything from the Graylog token.
Graylog itself is still only read (GET and the read-only searches). Until the environment of a question is known
(named by the caller, read from the question, or chosen by a provider among the instances its words fit), the
strictest setting of all your instances applies to everything the question hands over, the scoring too: the
default instance is where the search runs, not what the asker said. When that strictest setting is `"none"` and a
provider would have been needed to understand the question, `ask` asks you to name the environment rather than
pick one. `cost.providers` in every answer says how many requests and bytes went to each provider for that
question. A provider cannot be named `rules`, `guess`, `caller`, `default` or `unread`: those are what
`understood.sources` calls the parts no provider chose.

**A command as a provider.** The program is started for each request, without a shell, with the environment of
the server (so it finds its own login; it also sees what the server sees). It reads one JSON document on standard
input and writes one on standard output; what it writes to standard error is dropped. After `timeout` seconds it
is stopped together with whatever it started (on Windows only the program itself), and its reply may be at most
1 MB.

```
stdin   {"version": 1,
         "state": "thanh toán bị gì",                       # or {"request": "...", "candidates": ["...", ...]}
         "questions": {"intent": {"type": "choice", "instructions": "What does the request ask for?",
                                  "criteria": {"count": "The user asks how many ...", "search": "..."}},
                       "c0": {"type": "noul", "instructions": "Is candidates[0] directly related to ...?"},
                       "keywords": {"type": "words", "instructions": "Which words would a log line ...?", "max": 8}}}
stdout  {"answers": {"intent": {"choice": "search", "confidence": 0.8},
                     "c0": {"noul": 0.9},
                     "keywords": {"words": ["payment", "declined"]}}}
```

A `choice` answers with one key of `criteria`, a `noul` with the probability that the statement holds, `words`
with at most `max` plain words. An `openai` endpoint is told the same in one message and must reply with the same
JSON; text around it (a code fence, a sentence, a `<think>` block) is skipped. The `openai` kind has been tested
against a stand-in that speaks the Chat Completions format, not yet against a served model.

A repository's own `.graylog-mcp.toml`, and what it includes from inside the repository, may set the other `[ask]`
keys but not `base_url`, `api_key_env`, `providers` or a `provider_order` that names one, which decide where the
key and the questions go and which program runs: set those in your user config, or in a central file the
repository's file includes from outside it. `provider_order = []` is the exception: a repository may turn every
provider off. It can also set `provider_data`, `send_chars` and `max_candidates`, which decide how much your own
provider is handed, like the other settings of a file you [trust](#one-repository-several-environments).

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
3. **Changes.** Version fields, host rollouts and start/stop lines (see `view="changes"`) in the window and
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
system info), picks the APIs below and caches the result; `describe` shows the choice. An API that
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
  - a ready-made `profile` (below), then your own `packs`, `sensitive_fields` and regexes on top, plus an
    allow-list and excluded field names to avoid false positives.

  | `profile` | Adds to the core rules |
  |-----------|------------------------|
  | `basic` (default) | nothing |
  | `pii` | field names for personal names, phone, birth date, home/billing/shipping address, street |
  | `finance` | field names for account, card, CVV, IBAN, routing and tax ids; the `eu` pack (IBAN) |
  | `strict` | `pii` + `finance` + every country pack + IPv4 addresses (noisy: `1.2.3.4` version numbers are masked) |

  ```toml
  [redaction]
  profile = "pii"
  packs = ["vn"]                    # added to the profile's packs
  sensitive_fields = ["order_note"] # added to the profile's field names
  exclude_fields = ["phone"]        # always wins over the profile
  ```

  Grouping keys (`aggregate` with `kind="top"` or `"errors"`) are masked too.
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
- **Load on Graylog**: each instance takes at most `max_concurrency` requests at a time (default 16), shared
  by every tool call and, with the shared server, every session. Connection errors, 429 and 502/503/504 are
  retried `retries` times (default 2) with exponential backoff, honouring `Retry-After`.

## Configuration

Environment variables:

| Variable | Purpose |
|----------|---------|
| `GRAYLOG_URL`, `GRAYLOG_TOKEN` | single instance with token auth |
| `GRAYLOG_USERNAME`, `GRAYLOG_PASSWORD` | basic auth instead of a token |
| `GRAYLOG_VERIFY_TLS`, `GRAYLOG_CA_BUNDLE`, `GRAYLOG_PROXY` | TLS and proxy for the env-only instance |
| `GRAYLOG_TIMEZONE` | display timezone and zone for naive times (default UTC) |
| `GRAYLOG_REDACTION_PACKS` | e.g. `vn,eu` |
| `GRAYLOG_REDACTION_PROFILE` | `basic`, `pii`, `finance` or `strict` |
| `GRAYLOG_APP_PACKAGES` | e.g. `com.acme,/srv/app/` |
| `GRAYLOG_MCP_CONFIG` | path of a TOML config file |
| `GRAYLOG_MCP_HTTP_TOKEN` | bearer token required by the HTTP transport |
| `GRAYLOG_MCP_HOME` | the server's state folder: admin token, pid, log and recorded calls (default `~/.config/graylog-mcp`) |
| `GRAYLOG_MCP_USAGE` | `calls` records the calls without their result previews, `off` records nothing (see [the admin UI](#setup-helpers-and-admin-ui)) |
| `GRAYLOG_MCP_USAGE_HOT_DAYS`, `GRAYLOG_MCP_USAGE_DAYS`, `GRAYLOG_MCP_USAGE_SESSION_DAYS`, `GRAYLOG_MCP_USAGE_MAX_MB` | how long recorded calls keep every detail (default 2 days), their summary (15, at the least) and one line per conversation (90), and the folder's size limit (50 MB) |

A TOML file (`--config` or `GRAYLOG_MCP_CONFIG`, else `.graylog-mcp.toml` in the current directory or a
parent up to the repository root, else `~/.config/graylog-mcp/config.toml`) covers the
rest: several instances, token or basic auth, TLS verification and CA bundle, proxy, timezone,
`trace_fields`, `error_query` (because `level` is a syslog number or a string depending on how logs are
shipped), `app_packages`, how request logs read (`endpoint_fields`, `status_fields`, `business_error_query`,
`technical_error_query`, `slow_ms`, `latency_unit`, for the Logs screen of the admin UI), `ui_links`, redaction
packs and custom patterns, presets, the [glossary](#glossary-your-teams-words) and limits. Secrets are never
written in the file; it names the environment variable that holds them (`token_env`, `password_env`).
The file is validated at startup and any mistake (unknown key, bad regex, unknown timezone, missing
secret) stops the server with a precise message.

See [`examples/config.toml`](examples/config.toml) for every option.

## One repository, several environments

Commit a `.graylog-mcp.toml` at the root of your application repository with one instance per environment.
The server finds it from the current directory or any parent up to the repository root, so everyone working
in the repo gets the same setup. Tokens stay out of the file: each instance names its environment variable.

**Only use this with repositories you trust.** The file is applied as it is, with no approval step, and with the
shared server, opening a client in the repository is enough. Its `url`, `proxy`, `verify_tls` and `*_env` settings
decide where a saved secret is sent and which one, `include` can pull in other files, `[redaction]` can switch masking
off, `provider_data` and `[ask] send_chars` decide how much of the logs a provider you configured for `ask` is handed,
`repos` decides which folders get a `.mcp.json`, and `[http]` can open the server to the network. Read a file you
did not write before you run `start`, `init` or `install` there, or open a client in that repository.

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
after 14:00"*. Every tool takes `instance`, `describe` shows each environment with its description
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
| `graylog-mcp detect` | Suggests `service_fields`, `trace_fields`, `version_fields`, `latency_fields`, `endpoint_fields` (a route template before a path with ids in it), `status_fields` (only a field holding HTTP codes), `error_query` (syslog numbers or words), exception/logger fields and `app_packages` from the logs, with coverage and redacted sample values. `--apply` writes them. |
| `graylog-mcp install <client>` | Registers the server in `claude-code` (`.mcp.json`), `cursor`, `vscode` or `claude-desktop`, merging with existing entries and keeping a `.bak`. Tokens are referenced as `${VAR}` / `${env:VAR}`, never written (Claude Desktop gets placeholders unless `--with-secrets`). |
| `graylog-mcp install claude-code --trace` | Opt-in: adds two hooks to your Claude Code settings (`~/.claude/settings.json`, or `.claude/settings.local.json` with `--scope project`) so the admin page groups calls by conversation, subagent and turn. They record hashed ids only, never the prompt's text. `--untrace` takes them out; your own hooks are kept. The admin page's Connect screen has the same switch. |
| `graylog-mcp ui` | Local admin web UI (below). |
| `graylog-mcp bundle export\|import` | Share a team's settings without secrets ([below](#share-a-team-setup)). |

`graylog-mcp ui` opens a page on `127.0.0.1` with:

- **Overview**: a status line, then **Needs attention** (failing environments, failed checks, setup steps with one
  **Finish setup** button, searches that found nothing, each with its fix), log volume and errors over the last 24 hours or
  7 days (read from Graylog when the page opens, nothing stored), every environment's health with a sparkline or
  its problem and a button to fix it (*Add token*, *Fix*), the tools the model used, and the searches that found
  nothing;
- **Activity**, in four tabs:
  - *Dashboard*: calls per hour (found something / found nothing / error), calls per environment and a table
    per tool (calls, errors, latency median and p95, slow calls, results cut to the size budget);
  - *Conversations*: each turn of each conversation, newest first. Opening one shows its calls grouped by agent,
    the Graylog requests each sent (waiting, retries, status) and its redacted result preview, refreshed while a
    call is running. Older conversations keep a summary only. *Download JSON* exports the turn;
  - *Calls*: the recent calls with what was asked and their status (running, cancelled, refused, error);
    *Re-run* opens one in the Playground;
  - *Learned*: the fixes graylog-mcp learned (below).

  *Playground runs* counts the Playground's own calls too. Every view has its own address
  (`/admin/activity/conversations/<id>?turn=<ms>`), so a link to a turn or call can be bookmarked or shared
  with someone who has the page's token;
- **Logs**: request logs read straight from Graylog, every value redacted with your rules, in three tabs:
  - *Dashboard*: messages over time with their technical errors, messages and errors per application, the share
    of each HTTP status class, and a sortable table per API: calls, business errors (status 4xx by default),
    technical errors (5xx or the error query), % errors, p50/p95/p99 latency, slow calls from a threshold of
    `slow_ms`, and the last call. Every number is counted by Graylog over the whole range. A click on a status
    class, an application, an API or one of its counts narrows the query, a bar of the histogram the range;
  - *By trace*: the newest traces with their duration, services, worst level and first API; one trace opens as a
    timeline of its messages;
  - *All logs*: the newest messages, 100 a page, each opening on its fields with the stack trace folded. Graylog
    pages at most the newest 10,000: narrow the range or the query for older ones.

  The bar on top picks the environment, query, streams and range (*last 1h* or from … to …). *Save view* keeps a
  view in this browser's favourites, *Recent from the agent* reopens what the model searched, *Open in Graylog*
  runs the same search there, and the auto refresh (off, 30 s, 1 or 5 minutes) pauses while the browser tab is
  hidden or a row is open. These reads are recorded under Activity apart from the model's calls (*Playground
  runs*), the refreshes not at all. The view is in the address (`/admin/logs/traces?instance=prod&q=…&trace=…`);
- **Environments**: one card per group with its environments, default environment, repositories and their
  focus; descriptions and defaults save as you change them. The environment form tests the connection and saves
  in one click (*Save anyway* if Graylog cannot be reached), names the environment from the URL
  (`graylog-uat.acme.com` gives `uat`) and saves the token or password to your per-user secrets file;
- **Log fields**: settings per scope, with detection from your logs in the same page: suggestions next to the
  value in effect, applied to the scope you are editing;
- **Redaction**: toggle country packs, add custom patterns and allow-list entries, and see live which rules
  mask your sample text;
- **Playground**: run any tool against any environment and see the exact output, its size and an
  approximate token count;
- **Connect**: a tab per client (Claude Code first), each a three-step stack; snippets and one-click install for Claude Desktop, Cursor, VS Code and Claude Code per project;
- **Config file** (reached from the status bar, the command palette or the *Raw file* button): edit the TOML with line
  numbers, validation on <kbd>Ctrl</kbd>/<kbd>⌘</kbd>+<kbd>S</kbd> and an automatic backup. Below it, **Team
  bundle** exports the [shared settings](#share-a-team-setup) as a file, and previews and imports one.

An **environment picker** in the top bar carries one environment across Overview, Activity, Playground and Log fields.
**Settings** (the gear, or <kbd>,</kbd>) picks the colour scheme (Slate, Midnight, Graphite, Catppuccin), light or dark, and density.
Shortcuts: <kbd>/</kbd> search, <kbd>E</kbd> environment, <kbd>,</kbd> settings, <kbd>T</kbd> theme, <kbd>1</kbd>/<kbd>2</kbd>/<kbd>3</kbd>
switch tabs, <kbd>G</kbd> then <kbd>O</kbd>/<kbd>A</kbd>/<kbd>L</kbd>/<kbd>E</kbd>/<kbd>F</kbd>/<kbd>R</kbd>/<kbd>P</kbd>/<kbd>C</kbd> go to a screen.

Tool calls are recorded by the MCP server (stdio or shared) in `~/.config/graylog-mcp/usage/`, one file per
process and day: tool, environment, duration, number of results, size, the query or id asked for, the conversation
it belongs to, the Graylog requests it sent and the first 2,000 characters of its result, all **redacted with your
redaction rules before they are written**. It stays on your machine. A call is listed as running until it ends,
cancelled when the client gave up on it, and interrupted when its server stopped first. Calls group into
conversations by the client's session (with `install claude-code --trace`, by Claude Code conversation, subagent
and turn). The Playground's runs are recorded too, marked as such, and left out of the model's numbers unless
*Playground runs* is on. The `usage.jsonl` file of an earlier version is imported once. In the server's environment,
`GRAYLOG_MCP_USAGE=calls` keeps the calls without their results and `GRAYLOG_MCP_USAGE=off` turns recording off.
The server tidies the folder in the background: everything is kept for today and yesterday, each call without its
result and requests (a summary of them instead) up to 15 days, then one line per conversation up to 90 days, and
the folder stays under 50 MB, the oldest dropped first. `GRAYLOG_MCP_USAGE_HOT_DAYS`, `GRAYLOG_MCP_USAGE_DAYS`
(15 at the least), `GRAYLOG_MCP_USAGE_SESSION_DAYS` and `GRAYLOG_MCP_USAGE_MAX_MB` change those numbers.

<kbd>Ctrl</kbd>/<kbd>⌘</kbd>+<kbd>K</kbd> jumps to any page, environment, tool or action; unsaved changes show a
save bar (<kbd>Ctrl</kbd>/<kbd>⌘</kbd>+<kbd>S</kbd>) and are not lost by leaving the page. Light or dark follows
the OS until you pick one (the **Follow OS theme** checkbox goes back to it), with a phone layout; slate greys, teal for actions and data, amber for warnings, coral for failures.

The UI listens on loopback only, checks the Host header, and every API call needs the random token in the
link printed at startup. The browser keeps that token once the link is opened, so the page's other addresses
carry no token; a browser without it is told how to sign in. Since the browser keeps the token per address,
`graylog-mcp ui` keeps its port (8765, or `--port`): run again, it opens the page already running there, and a
port held by another program is an error instead of a move to the next one. It writes the config file and client configs when you ask. A token or password
entered in the form is saved, when you tick "Save it on this machine", to the same per-user secrets file as
`graylog-mcp login`, never to the config file.

While an admin page runs on the machine (`graylog-mcp ui`, or the shared server's `/admin/`), `search_logs`,
`trace_request` and `root_cause` add `ui` to their result: a link to the same view on the Logs screen, with the
environment, the redacted query and the absolute range, never the token. The model is told to hand it to you as
it is. A trace id that redaction would change is left out (the page could not look it up), and an environment the
page's config does not have is reported there, not replaced by another. The query stays in the local browser's
history like any address. `ui_links = "off"` at the top of the config file turns the links off.

`*_env` settings hold the **name** of a variable (`GRAYLOG_PROD_TOKEN`), never the secret: a value that is not
a valid variable name is refused, and never echoed back, since it is most likely a secret typed into the wrong
field.

### Share a team setup

One person who has everything set up exports the part a team shares; the others import it and only save their
own tokens:

```bash
graylog-mcp bundle export --name acme -o acme.toml   # what was left out is listed on stderr
graylog-mcp bundle import acme.toml                  # shows what changes, then asks
graylog-mcp login                                    # each person's own tokens or passwords
```

A bundle holds the environments (URL, description, group, aliases, `auth`, `verify_tls`, `provider_data`), log
fields, error queries, `[limits]`, `[redaction]`, `[stacktrace]`, `[investigation]`, presets, `[scan]`, the
glossary, and the settings of `[ask]` that choose no destination (`language`, `send_chars`, `max_candidates`,
`min_confidence`, `relevance`, `time`, `intent_words`, `code_patterns`). It never holds a token, a password, a
username, the name of a variable holding a secret, a local path (`repos`, `ca_bundle`), `proxy`, `[http]`,
`focus`, `only_groups`, the `default_*` choices, `ui_links`, nor any setting that decides where data goes
(`[ask] api_key_env`, `model`, `base_url`, `providers`, `provider_order`). Exporting refuses a value that looks
like a secret (a private key, a JWT, a bearer or basic credential, a password in a URL).

Importing writes `teams/<name>.toml` beside your config file (`~/.config/graylog-mcp/config.toml`, or `--config`)
and adds it first to the file's `include`, so everything in your own file stays in force. The preview lists every
setting added, changed and removed; apart, the changes that decide where a token or log text goes (a URL of an
environment you already have, `verify_tls = false`, `provider_data`), and the settings your own file sets
differently. Nothing is written until you confirm (`--yes` in a script). Variables for the secrets are named by
the server after each environment (`GRAYLOG_PAYMENT_PROD_TOKEN`, `_USERNAME`, `_PASSWORD`), never by the bundle,
and `graylog-mcp login` asks for each. A bundle with a key outside this list, or of a newer format than this
version reads, is refused; so is a team file edited by hand to add one, when the config loads. Importing a newer
version of the bundle replaces the team file and keeps a `.bak`; if the config would no longer load, both files
are put back.

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

`describe` returns the groups with their environments and descriptions, `graylog-mcp init` asks for
groups first, and the admin UI shows environments by group (instances from an included file are marked and
edited in that file).

### Repositories of a group

List the repositories each group serves, as local folders (paths relative to the config file, `~` allowed) or
git remotes (`git@gitlab.corp:acme/payment-api.git`, `acme/payment-api` or just `payment-api`):

```toml
[groups.payment]
repos = ["~/code/payment-api", "../payment-worker", "acme/payment-gateway"]
```

When the server runs inside one of them (a folder or any subfolder, or a clone whose `origin` matches), it
loads **only that group**: "errors on prod" means that system's production, and the other groups' Graylog
servers are not reachable from that repository. Asking for one returns an error that says why and how to
enable it. `describe` shows the repository and the `scope`; `graylog-mcp doctor` shows which repository
was recognised.

To reach more groups from a repository, set `only_groups = ["payment", "erp"]` (or `"*"` for all) in its
`.graylog-mcp.toml`, or `GRAYLOG_MCP_GROUPS=payment,erp` in the MCP client's environment. `only_groups` also
works without repositories, e.g. in a personal config. The admin UI always shows every group.

Manage them in the admin UI (Environments, in each group's card: add a local folder, see its git remote and
whether it is set up, "Set up" writes `.graylog-mcp.toml` with an `include` of the shared file and
`only_groups`, so the scope holds on every machine whatever its folder layout, and registers
Claude Code there) or from the command line:

```bash
graylog-mcp repo --config ../platform/graylog-org.toml add payment   # current folder; also sets it up
graylog-mcp repo list
graylog-mcp repo remove payment ~/code/payment-api
```

### Focus: search this repository's service by default

Inside a repository, "errors in the last hour" means that service's errors. `search_logs`, `aggregate`,
`compare_periods` and `root_cause` with `view="changes"` add a filter on the
service field and say so in their result (`focus`). Other services and streams are searched only when asked for:
`streams=["*"]` (everything), explicit `streams`, or a query that names a service field
(`application:"other-service"`). `trace_request` and the other `root_cause` views always span every service.

Each repository sets its focus in its own `.graylog-mcp.toml`:

```toml
[focus]
service = "catalog-mdm-service"   # value of the service field; a list for several; false = no service filter
streams = ["MDM"]               # optional: streams searched by default
# field = "application"         # optional: the service field (default: the first of service_fields present)
```

Without `[focus]`, the service is guessed from the repository name (folder or git remote) by matching it against
the values of the service fields (`service`, `application`, ...) over the last 24 hours: `catalog-mdm` finds
`catalog-mdm-service`. No clear match, no filter; `describe` shows what was picked. Set it from the admin UI
(Environments > the repository's Focus), with `graylog-mcp repo focus catalog-mdm-service --streams MDM` inside the
repository, or for one MCP client with `GRAYLOG_MCP_SERVICE=<name>` (`off` disables it).

### Settings by scope

The admin UI's **Log fields** page edits field names, queries and connection options for a chosen scope: global,
one environment (for every group), one group, or a single instance. Each field shows the value currently in
effect; leaving it empty inherits it.

## One process for every session

With stdio, the client starts a server process for every session: ten Claude Code sessions are ten
Python processes, each with its own connections and version detection. `graylog-mcp start` runs one server
for all of them instead, and still answers each session with the configuration of the repository it works
in (its `.graylog-mcp.toml`, group, focus), as if it had been started there.

What `start` does, so you know what is on your machine:

- **A stable copy**: `uv tool install` (in `~/.local/share/uv/tools`), not uvx's temporary cache.
  `update` reinstalls it from the same source; the version shown everywhere includes the git commit.
- **The server**: `graylog-mcp serve --shared --admin --port 18742 --config <file>` on 127.0.0.1 (the port is fixed so
  no client configuration goes stale: if another program uses it, `start` stops with an error; `--port` picks
  another one, later commands follow it), started at
  login and started again if it crashes:

  | | Started at login by | Restarted after a crash by |
  |---|---|---|
  | macOS | a LaunchAgent (`~/Library/LaunchAgents/io.github.ntbang0901.graylog-mcp.plist`) | launchd |
  | Linux with systemd | a user unit (`~/.config/systemd/user/graylog-mcp.service`) | systemd |
  | Linux desktop without systemd | XDG autostart (`~/.config/autostart/graylog-mcp.desktop`) | `graylog-mcp keepalive` |
  | Windows | a Task Scheduler task `graylog-mcp`, no console window | `graylog-mcp keepalive` |

  Elsewhere (WSL or a server without systemd, no desktop session) it runs until you log out; on WSL, turning
  on systemd (`[boot] systemd=true` in `/etc/wsl.conf`) is enough. Log: `~/Library/Logs/graylog-mcp.log` or
  `~/.config/graylog-mcp/server.log`.
- **The admin page** at `http://127.0.0.1:18742/admin/`, with the access token kept in
  `~/.config/graylog-mcp/admin-token` (owner only); `graylog-mcp ui` opens it.
- **Claude Code**, connected repository by repository: `start` writes the shared server into the `.mcp.json` of
  each repository of your groups, creating the file when it has none (the other servers in it stay, the previous
  file is kept as `.mcp.json.bak`):
  `{"mcpServers": {"graylog": {"type": "http", "url": "http://127.0.0.1:18742/mcp", "headers": {"X-Graylog-MCP-Repo": "${PWD:-}"}}}}`.
  Claude Code fills in the folder it runs in; the server walks up to the repository, and asks you once to approve
  a project's server. A repository whose `.mcp.json` still starts graylog-mcp itself gets a private override
  (Claude Code's local scope) instead: the file stays as it is for your teammates. `graylog-mcp start --global`
  registers it once for every project (user scope) instead. Without it, `start` removes a registration for every
  project that an earlier version made, so that Claude Code does not report the server as defined in several
  scopes with different endpoints.
- **A different port**: whenever the server starts (by `start`, at login, after `update`), what an earlier port
  registered follows it: Claude Code's registrations and overrides, the URL of a `.mcp.json` entry on
  another local port (rewritten in place, `.bak` kept) and the Cursor and VS Code entries of your groups' repositories. `status` lists what still points
  elsewhere.

How the server picks the configuration of a session:

- The repository's own `.graylog-mcp.toml`; without one, the `--config` file, with the group matched from its
  `repos`. `start` keeps one such file wherever you run it: the one given to `start --config` last time, else
  `~/.config/graylog-mcp/config.toml`. `graylog-mcp ui` opened in a folder without a config edits that file too.
- Edits (admin page, `login`, `repo focus`) apply within seconds, without a restart.
- One Graylog client per instance, shared by every repository using it: one connection pool, one version
  detection, one stream and field cache.
- Tokens come from the server's environment or `graylog-mcp login` (the admin page saves them there too).
- Other clients: Cursor and VS Code send `${workspaceFolder}` (`graylog-mcp install cursor --shared`), any
  client can add `?repo=<folder>` to the URL. Claude Desktop already runs one server for all its chats.

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

The image's user has no home folder, so tool calls are only recorded when `GRAYLOG_MCP_HOME` points to a writable
volume (`-e GRAYLOG_MCP_HOME=/data -v graylog-mcp:/data`). Keep that folder off network and synced file systems: the
server's processes lock and rename files in it. A server shared by a team records everyone's queries, redacted, in
that one folder; `GRAYLOG_MCP_USAGE=off` records none.

## A server for the team on a VM

One `serve --shared` process on a VM can serve every developer and every repository. The VM cannot read the
developers' folders, so a client names its repository by **identity**: its git remote
(`github.com/acme/payment-api`, `acme/payment-api`, any git URL) or, failing that, its folder name. Groups list
their repositories by remote or name, and `[repos."<repository>"]` holds each one's settings (what a
repository's own `.graylog-mcp.toml` holds on a developer's machine):

```toml
# /etc/graylog-mcp/org.toml on the VM, kept in a git repository and deployed by the platform team
[groups.payment]
repos = ["acme/payment-api", "acme/payment-worker"]
[groups.payment.environments.prod]
url = "https://graylog-pay.example.com"
token_env = "GRAYLOG_PAYMENT_PROD_TOKEN"

[repos."acme/payment-api"]                  # focus, default_group/environment/instance, only_groups, scan, presets
focus = { service = "payment" }

[http]
auth_token_env = "GRAYLOG_MCP_HTTP_TOKEN"   # every developer sends it
# repo_paths = false                        # default off a loopback address: folders clients name are never read
```

```bash
graylog-mcp serve --shared --host 0.0.0.0 --port 8000 --config /etc/graylog-mcp/org.toml
```

The config is read again within seconds of a change: deploy the file, no restart. The admin page writes files
on the machine it runs on, so it only listens on the loopback address; reach it with an SSH tunnel
(`ssh -L 8000:127.0.0.1:8000 vm`, then `http://127.0.0.1:8000/admin/` with `serve --admin`).

On each developer's machine, nothing runs but Claude Code:

| | |
|---|---|
| `GRAYLOG_MCP_HTTP_TOKEN=<token>` | the server's token, in the environment Claude Code starts in (shell profile; `setx` on Windows). Claude Code sends it as `Authorization: Bearer ${GRAYLOG_MCP_HTTP_TOKEN}`; no graylog-mcp command prints it |
| `graylog-mcp connect https://graylog-mcp.example.com` | shows how the server identifies the current repository and registers the server in Claude Code for every project (asks for the token when the variable is not set, to check the connection) |
| `graylog-mcp connect URL --project` | writes the repository's `.mcp.json` instead. It holds no secret and no path: commit it; each teammate only sets the variable |
| `graylog-mcp status` | is the server reachable, which version, and how it identifies this repository: group, focus, default environment, each Graylog instance |

How a connection names its repository: in a project's `.mcp.json`, Claude Code runs `graylog-mcp headers`
(its `headersHelper`) in the project folder when it connects; it prints the repository's git remote. Without
graylog-mcp installed, the remote written by `connect` applies. Registered for every project, there is no helper
(it would run outside the project): the server identifies the repository by the name of the folder Claude Code
runs in (`${PWD}`), so list repositories by name in `repos`, or use `connect --project` for the ones listed by
remote.

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
  trace.py      following one request or one business code across services
  glossary.py   the team's business terms and code shapes, matched in a question
  rca.py        root cause analysis: onsets, change detection, service map, ranking
  ask/          the `ask` tool: the rules that read a question (time, intent, environment), what was understood,
                error candidates and flow, the optional providers (Jev, OpenAI-compatible, a command), fixed texts
  operations.py every operation in one table: what presets, the admin page and usage know by name
  toolspecs/    the tools a model sees, each declared once: its arguments and the operations it runs
  server.py     model instructions, and the MCP tools built from those declarations
  usage*.py     the recorded tool calls: their store, its three tiers, and conversations and turns
  explorer*.py  the admin page's Logs screen: per-API counts, traces and messages read from Graylog
  ui_links.py   where a running admin page listens, and the `ui` links to it in tool results
  admin/        the admin UI: a JSON API (app.py) and one page with its scripts (static/)
```

## License

MIT
