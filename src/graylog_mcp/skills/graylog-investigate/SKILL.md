---
name: graylog-investigate
description: Use when investigating an incident or a "why did X happen" question with the graylog MCP tools (more than one search, or work someone may continue later). Keeps a local record the user follows in the admin page; covers starting or resuming one, recording checks, evidence and conclusions, and finishing. Needs the investigation tool, which is off until the admin page's Investigations screen turns it on.
---

# Investigating with graylog-mcp

An investigation is a local record of what was asked, what ran, what it showed and what you concluded. The user
watches it in the admin page, and a later session can continue it. It never changes Graylog. A single lookup does not
need one: just search.

## 0. Is it available?

If there is no `investigation` tool, investigations are off. Say so and stop recording: tell the user to open the admin
page, Investigations, turn them on, and restart the server (`graylog-mcp start`) for new sessions. Investigate without
a record meanwhile; do not make one up in files.

## 1. Resume, follow up, or start

1. `get_investigation` with no id lists this repository's recent ones. If an open one covers the question, resume it:
   `investigation(action="resume", investigation_id="INV-3")`. It returns a new `owner_fence`. A running one owned by
   another session is refused until its 10-minute lease ends; take it over (`takeover=true`) only if the user says the
   other session is gone. `get_investigation(investigation_id, view="briefing")` is the short way to catch up.
2. To check that a fix worked, start a new one with `follows="INV-3"` (it must be completed). Whether the fix worked
   is `verification` at finish, apart from `outcome`.
3. Otherwise start one. Turn the user's words into absolute times once, in their timezone, and keep them. The page
   shows every time in the browser's zone, so give times with their offset and do not convert them for display. If the
   environment is ambiguous ("prod" in two groups), ask before searching.

```
investigation(action="start", title="Intermittent checkout failures", question="Why do checkouts fail?",
              instance="checkout/prod", from_time="2026-11-18T09:30:00+07:00", to_time="2026-11-18T09:45:00+07:00",
              baseline_from="2026-11-18T09:15:00+07:00", baseline_to="2026-11-18T09:30:00+07:00")
```

The answer has `id`, `label` (INV-3), `owner_fence` and `ui`. Keep the fence for every later call. Give the user `ui`
exactly as it is: do not open, fetch or edit it. If it says the page is unavailable, say so and go on.

## 2. Run checks

The page draws each check as one node in a task flow, so work in small checks, one per piece of work:

1. `investigation(action="update", investigation_id, owner_fence, items=[{"kind": "check", "id": "c1",
   "purpose": "Which service errors first?"}])`. The `purpose` is the node's title: a short phrase, not a paragraph.
   Checks that test one explanation go under one branch (`kind: "branch"`, `question`; the check lists it in
   `branches`); the page draws them side by side under the question. Hypotheses are `kind: "hypothesis"`,
   `statement`.
2. Run the search with `investigation_id`, `owner_fence` and `check_id="c1"` added to the arguments of `search_logs`,
   `aggregate`, `compare_periods`, `get_message`, `trace_request`, `scan`, `root_cause` or `run_preset`. Keep inside
   the window and the instance. A call without the three arguments is not recorded, and so is not evidence.
3. The result does not carry an evidence id. Read it from `get_investigation(investigation_id)`: each step lists its
   `evidence`; `view="evidence"` without an id is the index, with an id one record in full. Evidence comes from
   steps that succeeded. A failed, cancelled or interrupted step gives none, and neither does an empty result: say
   what was empty and why it may be.
4. To change the window, `update` with `query`, `from_time`/`to_time` and a `reason`. It makes a new scope revision;
   earlier steps stay what they were.

## 3. Record what you found

After a check, write what it showed and what you conclude from it:

- `check_result`: `check`, `result`, `evidence` (ids). One per finished check. `result` is the one line its node
  shows: what it found, with each count beside its total ("126 of 4,200 requests failed (3.0%)"). A step that ran is
  not a result.
- `assessment`: `claim` (a short statement of one explanation), `assessment` = `supported` | `contradicted_in_scope`
  | `undetermined`, with `evidence` (or `supporting`/`contradicting`) ids. `supported` and `contradicted_in_scope`
  without evidence are refused; when nothing was found, say `undetermined`. "Contradicted in scope" means not seen in this window and these streams; it is not
  "did not happen".
- `identity` (a user, order, trace id): `type`, `namespace`, `value`, `evidence`. Values come from evidence. A masked
  value (`[EMAIL]`, `[CARD]`, `[REDACTED]`) is never an identity and never a join key; do not try to recover it.
- `relationship`: `from`, `to` (identity ids), `relation`, `status`. Same key, similar text or nearby times are
  `candidate`. `observed` needs a `basis` and evidence of the link itself.
- `comparison`: `unit` and `cohorts` of `{name, count, denominator?}`. Give a `rate` only with a numeric
  `denominator`; when the eligible total is not known, leave it out and say it is unknown.
- `evidence_request`: when only the user has it (a log you cannot reach, a deploy time, a ticket), record
  `question` with `status` `requested` and ask them; the page shows it as a node waiting for the user. Record their answer with `status` `received` and
  `provenance` `user_reported`, `unverified` or `verified`. Do not treat it as log evidence.

Items are created by `id` (reuse an id with its current `revision` to change it). One `update` can carry several items.
If an `update` is refused, the message says which rule; fix the record, do not rephrase it to get past the rule.

## 4. Stop, hand over, finish

- Stopping partway or near the end of your context: `investigation(action="checkpoint", investigation_id,
  owner_fence, briefing="...")`: what is established, what is ruled out, the next check. A new session continues from
  it.
- Done: `investigation(action="finish", investigation_id, owner_fence, outcome=..., report={...})`, where `outcome` is
  `explanation_supported`, `cause_undetermined`, `no_matching_evidence` or `no_anomaly_detected`, and `report` holds
  `findings`, `limits` and `next_checks`. Finish only when no step is running. `cause_undetermined` and
  `no_anomaly_detected` are real outcomes: do not stretch a thin result into `explanation_supported`.
- The user changed their mind: `action="cancel"` with a `reason`. A cancelled investigation cannot be resumed.
- Retrying a call after a timeout: pass the same `operation_id` and you get the same answer.

## What the user sees

The page's explanations and the "still unverified" part of its handoff row come from your assessments, requests and
checks not yet run, and its findings are the ones the assessments cite, so cite what you rely on. Tell them the outcome, what
the evidence does not cover (the window, the streams, sampled or cut results), and the `ui` link.
