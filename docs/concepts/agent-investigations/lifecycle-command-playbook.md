# End-to-end command and conversation playbook

Status: proposal. Existing setup commands are explicitly identified. Every `graylog-mcp investigate ...` command and every investigation lifecycle tool shown below is new design syntax, not implemented functionality.

## 1. Prepare the existing local service

These commands exist today:

```bash
graylog-mcp start
graylog-mcp status
graylog-mcp ui
```

Start sets up the shared service and can open the admin page. Use an already-connected agent to perform investigation work. This concept does not execute these commands or change the user's installation.

## 2. Start from the agent conversation

User:

```text
Investigate intermittent checkout failures in checkout/prod on
18 November 2026, 09:30–09:45 Asia/Ho_Chi_Minh.
Compare against 09:15–09:30. Show me the investigation link.
```

Proposed agent lifecycle call, shown as pseudocode:

```python
investigation(
    action="start",
    title="Intermittent checkout failures",
    instance="checkout/prod",
    from_time="2026-11-18T09:30:00+07:00",
    to_time="2026-11-18T09:45:00+07:00",
    baseline_from="2026-11-18T09:15:00+07:00",
    baseline_to="2026-11-18T09:30:00+07:00",
)
```

Illustrative response:

```text
Investigation: INV-024
Scope: checkout/prod, 09:30–09:45 ICT; baseline 09:15–09:30 ICT
Open: <running-local-admin-origin>/admin/investigations/<opaque-id>
Current action: Compare affected services and verify the leading findings.
```

An ID can be passed in place of the question when adopting an existing draft. A relative request such as “last 15 minutes” is resolved to absolute timestamps once and echoed back. If “prod” identifies multiple configured instances, the agent asks which before issuing queries.

## 3. Watch and inspect

Proposed CLI for viewing existing records:

```bash
graylog-mcp investigate list --state running
graylog-mcp investigate show INV-024 --open
graylog-mcp investigate show INV-024 --json
```

Opening the page retrieves saved state and live events. It does not run Graylog searches or start an agent. The service discovers its actual admin origin; the displayed URL does not contain credentials.

## 4. Follow requests or recover from missing information

Known-ID prompt:

```text
In INV-024, follow request req-7f3a across the available services.
Separate observed log events from measured durations and mark missing coverage.
```

No-ID prompt:

```text
In INV-024, group the checkout errors and inspect representative samples for
correlation IDs. If none exist, use surrounding logs and label them uncorrelated.
```

Zero-result prompt:

```text
In INV-024, explain why the query found no matches. Check the returned field
diagnosis and counted suggestions. Keep the original incident window and scope.
```

Scope-change prompt:

```text
Expand INV-024 to 09:00–09:45 ICT. Record the change and keep the earlier
evidence attached to its original window. Explain which conclusions need rechecking.
```

These prompts use existing Graylog operations through the proposed investigation context. Discovery uses `describe`; causal triage uses `root_cause`; counts/groupings use `aggregate`; detail uses `search_logs`, `get_message`, and `trace_request`. Proposed `investigation_id` propagation links their work to INV-024.

## 5. Compare explanations and steer through chat

```text
In INV-024, compare two explanations: database connection pressure and a payment
deployment regression. Show supporting and contradicting evidence for each.
Keep unrelated authentication errors in a separate branch.
```

```text
Checkpoint INV-024. Record what is established, what is uncertain, and the next
checks. Do not start another check after the current operation finishes.
```

The agent explicitly acknowledges checkpoint requests. The UI shows the request and resulting state rather than pretending that it instantly stopped an already-issued query.

## 6. Resume after interruption or in another conversation

Proposed CLI:

```bash
graylog-mcp investigate resume INV-024
```

Illustrative terminal output:

```text
Saved briefing for INV-024
State: interrupted; previous attempt is no longer active
Scope: checkout/prod, 18 Nov 2026, 09:30–09:45 ICT
Evidence: E-001 counts; E-002 error groups; E-003 request observations
Open questions: Did database pressure begin before the payment timeout group?
Next: Paste “Resume investigation INV-024” into a connected agent conversation.
```

This command only prints a briefing and instructions. It does not start, resume, or impersonate the external agent. The connected agent performs the actual lifecycle resume after retrieving saved context and checking ownership/access.

Agent prompt:

```text
Resume investigation INV-024. Read its checkpoint and saved evidence first.
Keep the original incident window. Continue the unresolved checks and report
any access or configuration changes before relying on old results.
```

## 7. Finish and hand off

```text
Finish the analysis for INV-024. Summarize observed impact, supported explanations,
contradictions, coverage gaps, and verification steps. Link every finding to evidence.
If the cause is still undetermined, say so explicitly.
```

Proposed export command:

```bash
graylog-mcp investigate export INV-024 --format markdown --output ./INV-024.md
```

The export contains redacted snapshots and an explicit outcome. Local UI links are labeled local. It does not publish a ticket, share credentials, deploy a fix, or claim that an exported report proves causation.

## 8. Verify a fix performed elsewhere

```text
For INV-024, start a linked verification attempt for 18 November 2026,
11:00–11:15 ICT. Compare request-event failures with the original incident,
check the affected error families, and record whether recovery is verified
or the available data is inconclusive.
```

A proposed lifecycle update starts a new verification attempt with its own absolute times. The original query window and report remain intact.

## Optional terminal-first draft

Proposed command:

```bash
graylog-mcp investigate start \
  --title "Intermittent checkout failures" \
  --instance checkout/prod \
  --from "2026-11-18T09:30:00+07:00" \
  --to "2026-11-18T09:45:00+07:00" \
  --baseline-from "2026-11-18T09:15:00+07:00" \
  --baseline-to "2026-11-18T09:30:00+07:00" \
  --open
```

Creates a draft, opens its page, and prints an adoption prompt for a connected agent. It performs no autonomous analysis. If no agent adopts it, the page remains Draft with the instruction “Continue this investigation in your agent conversation”.

## Proposed CLI consistency

- `list`, `show`, and `resume` read records; `resume` emits a briefing only.
- `start` creates local state; `export` writes the selected output file.
- `--open` is explicit, and opening a page never reruns saved queries.
- JSON output includes state, outcome, scope revision, evidence availability, and UI availability separately.
- Unknown ID, missing authentication, unavailable service, and expired evidence produce distinct explanations. Show retained metadata when detailed evidence has expired.
- A local file export can work from retained evidence; a UI link requires a reachable admin service.
