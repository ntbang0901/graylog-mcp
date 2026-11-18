# Complex-case command and conversation playbook

Status: proposed feature, not implemented commands. Existing setup remains `graylog-mcp start`, `graylog-mcp status`, and `graylog-mcp ui`. The [lifecycle playbook](lifecycle-command-playbook.md) defines record management, browser opening, exports, and manual agent resume.

## Begin with a reported symptom

```text
Investigate order-842 in checkout/prod on 18 November 2026, 09:30–09:45 ICT.
The customer reports two charges and checkout returned 500.
Separate reported symptoms from observed evidence. Open an investigation and
give me the UI link. Establish whether repeated processing occurred and which
evidence is needed to verify the financial outcome.
```

Illustrative agent response:

```text
Created INV-028. Scope fixed to checkout/prod, 09:30–09:45 ICT.
Open: <local-admin-origin>/admin/investigations/<opaque-id>
Reported: two charges. Verification status: not established.
Strategy: separate HTTP submissions, upstream retries, worker attempts,
payment outcomes, and idempotency handling.
First checks: identify available fields/coverage and representative evidence.
```

## Direct a useful distinguishing check

```text
In INV-028, check whether the gateway received two submissions or retried one
upstream request. State what evidence would distinguish those explanations,
then inspect the available request and attempt metadata. Keep coverage gaps visible.
```

```text
In INV-028, inspect worker processing attempts for queue-A / msg-91.
Separate redelivery, application retries, and repeated log records.
Do not infer the delivery mechanism solely from a repeated message ID.
```

```text
In INV-028, test whether payment completed before the checkout timeout.
Show explicit linkage and ordering evidence. If clock uncertainty or missing
response metadata prevents ordering, leave it unresolved.
```

Proposed lifecycle update shape, shown as pseudocode rather than executable API syntax. The owner fencing token is obtained from lifecycle start/resume; it is not a credential embedded in the UI link. The operation ID deduplicates retries of this preparation update.

```python
investigation(
    action="update",
    investigation_id="INV-028",
    expected_revision=7,
    owner_fence="current-owner-token",
    operation_id="prepare-C-03",
    scope_revision=1,
    branch={
        "id": "B-3",
        "question": "Was the message redelivered or logged repeatedly?",
        "strategy": "inspect_processing_attempts",
        "reason": "Distinct attempts would distinguish repeated execution from duplicate logging",
    },
    check={
        "id": "C-03",
        "branch_ids": ["B-3"],
        "purpose": "Compare delivery metadata and processing attempts",
        "alternatives": ["redelivery", "duplicate_logging"],
        "scope": "incident scope revision 1; checkout/prod; fixed absolute window",
        "identity_filters": [{"type": "message", "namespace": "queue-A", "value": "msg-91"}],
        "counting_units": ["qualified deliveries", "processing attempts"],
        "planned_operation": "search_logs; construct query from discovered fields",
        "query_budget": {"max_operation_calls": 3, "max_returned_messages": 100},
        "expected_observations": {
            "redelivery": "Distinct deliveries linked to distinct processing attempts",
            "duplicate_logging": "Repeated event IDs or one observed execution sequence",
        },
        "required_sources": ["worker logs", "logged broker delivery metadata"],
        "required_coverage": ["worker_attempts", "delivery_metadata"],
        "stop_condition": "Retain uncertainty if delivery metadata is unavailable",
    },
)
```

Subsequent existing Graylog operations receive proposed investigation/branch/check references. The tool wrapper saves execution evidence; the agent then records an assessment. Neither the data shapes nor extra parameters above exist yet.

## Ask for a comparison strategy

Rare failure:

```text
In INV-028, compare failed and successful eligible operations by version and host.
Show absolute counts and denominators, sample coverage, and relevant confounders.
Treat any association as a lead for another check.
```

Deploy hypothesis:

```text
In INV-028, test the rollout explanation against old/new versions and unaffected
instances. Check whether the failure existed before the rollout. Preserve evidence
that contradicts the explanation.
```

Race hypothesis:

```text
In INV-028, inspect overlapping state transitions for explicit sequencing or causation
metadata. Distinguish observed order from timestamp-only order. Keep ordering unknown
when the evidence cannot establish it.
```

## Request external verification without implying new access

```text
In INV-028, list the exact authoritative transaction evidence needed to establish
whether two charges occurred. Record a pending evidence request. Do not infer the
outcome from two success logs or call payment systems.
```

If the user later supplies redacted evidence through a supported interaction:

```text
Attach this evidence to INV-028 with its source and supplied time. State which
claims it supports and which mechanism questions remain unanswered. Keep user-reported
statements distinguishable from verified records.
```

## Inspect and resume

Proposed CLI, unchanged from the lifecycle playbook namespace:

```bash
graylog-mcp investigate show INV-028 --open
graylog-mcp investigate show INV-028 --json
graylog-mcp investigate resume INV-028
graylog-mcp investigate export INV-028 --format markdown --output ./INV-028.md
```

Resume prints a briefing; it does not launch an external agent. The briefing now includes branches, check assessments, typed identities, coverage gaps, and pending external evidence requests.

Connected-agent prompt:

```text
Resume INV-028. Read its branch assessments and checkpoint first. Continue the
next distinguishing check using the original scope. Treat pending external evidence
as unavailable until it is actually supplied.
```

## Finish with an evidence-backed outcome

```text
Finish analysis of INV-028. Report observed facts, supported mechanisms, alternatives,
and missing evidence separately. State whether the reported duplicate charge is
verified, contradicted within a documented scope, or still unverified.
Record the next checks even if the overall cause remains undetermined.
```

The report can be complete while business outcome verification or a causal explanation remains unresolved. A fix or post-fix verification requires a separate user-directed action and fresh scope, as defined in the lifecycle specification.
