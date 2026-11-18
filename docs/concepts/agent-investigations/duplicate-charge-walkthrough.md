# Worked case: two reported charges and a checkout 500

Fictional example for the proposed feature. No financial event described here is a real finding.

## 1. Establish what is reported and what is observed

User report: “Customer says they were charged twice for order-842, and checkout returned 500.”

Scope: checkout/prod, 18 November 2026, 09:30–09:45 ICT. Baseline: 09:15–09:30 ICT. Start with a reported duplicate charge; whether two charges actually occurred is an open question.

Initial observed evidence:

- E-001: a checkout 500 log for req-a linked by a structured order field to order-842.
- E-002: two worker processing-start records mentioning queue-A / msg-91, with distinct recorded attempt values.
- E-003: two payment success log events associated with the order; neither returned excerpt establishes the provider's final transaction state.

These observations define useful checks. They do not establish a causal sequence, message redelivery, or duplicate settlement by themselves.

## 2. Open five scoped branches

| Branch | Question and alternatives | Next distinguishing check |
|---|---|---|
| B-1 Client submission | Two client submissions, or one submission followed by another retry mechanism? | Inspect request-event records with request IDs and gateway coverage; do not count arbitrary log lines as submissions. |
| B-2 Gateway retry | Did the gateway retry the upstream request, or did checkout receive it once? | Inspect explicit upstream attempt metadata and any propagated IDs. |
| B-3 Worker delivery | Was the message redelivered, or are these duplicate/independent application logs? | Compare delivery/acknowledgement metadata and application attempt records for the qualified message identity. |
| B-4 Outcome and timeout | Did a payment operation complete before checkout failed, or is that ordering unknown? | Check explicit response/causation metadata, timestamp sources, and provider transaction state where available. |
| B-5 Idempotency | Did repeated execution produce one business effect, multiple effects, or an unknown outcome? | Inspect observed keys and decision logs, then request authoritative transaction verification. |

The UI distinguishes branch dependencies. The client and gateway checks can run independently. Verifying two charges needs authoritative business outcome evidence regardless of which retry mechanism occurred.

## 3. Record the purpose of the selected check

Selected check C-03 in B-3:

```text
Purpose: Distinguish queue redelivery from repeated logging of a single processing attempt.
Scope: queue-A / msg-91, fixed incident window, checkout/prod.
Required coverage: worker attempt metadata and delivery/acknowledgement fields.
If redelivery: expect distinct delivery metadata linked to processing attempts.
If repeated logging: repeated event identifiers or one execution sequence may explain the records.
Stop: if delivery metadata is missing, retain attempts as observed and leave delivery mechanism unknown.
```

This check can use existing search_logs and get_message operations under the proposed branch/check context. Use exact field names discovered from the environment. The evidence inspector shows both requested and executed queries.

## 4. Show observations and gaps without filling them in

Illustrative check results:

- E-004: complete gateway records available for the examined request show one recorded inbound submission. Client telemetry is absent. B-1 assessment: duplicate client submission is not supported by this examined gateway slice; overall submission history remains incomplete.
- E-005: the retained gateway samples do not include upstream attempt metadata. B-2 assessment: undetermined due to missing coverage.
- E-006: distinct worker processing attempts are observed for the same qualified message identity. Broker redelivery/ack metadata is missing. B-3 assessment: repeated processing supported; redelivery mechanism undetermined.
- E-007: a payment success log and a checkout timeout log exist, but clock uncertainty and missing response linkage prevent a reliable cross-service ordering. B-4 assessment: ordering unknown.
- E-008: returned samples lack usable idempotency-decision fields and authoritative transaction status. B-5 assessment: enforcement and duplicate business effect unknown.

Select B-3 to focus worker attempts. Select E-006 to inspect records and typed identifiers. A diagram may group observations under order-842 but must not draw observed propagation edges solely because they share that order ID. “Missing broker metadata” remains a visible gap.

## 5. Ask for the exact missing evidence

The external evidence request is concrete:

```text
Need: authoritative transaction records for order-842 in the incident window.
Question: Are there two distinct captured/settled transactions, a single transaction
reported twice, or an authorization followed by another state?
Requested fields: redacted provider references, transaction type/status, timestamps,
and a usable relationship to the order or idempotency scope.
Source: authorized payment/provider or ledger verification supplied by the user.
```

The branch shows Awaiting external evidence. No payment API is called by this concept. If the user only supplies a statement, record it as user-reported. A redacted authoritative excerpt can provide stronger evidence within its documented scope, but must retain provenance and verification limitations.

## 6. Finish, checkpoint, or continue according to evidence

Without external evidence, a useful handoff is:

```text
Observed: checkout failure and repeated worker processing for one qualified message.
Unresolved: gateway retry mechanism, broker redelivery, cross-service ordering,
idempotency decisions, and whether two financial charges occurred.
Next: obtain authoritative transaction verification and missing delivery metadata.
Outcome: cause undetermined; reported duplicate charge not yet verified.
```

If later evidence establishes one transaction reported in multiple logs, close the duplicate-charge claim only within that source and scope; continue investigating the checkout 500 as appropriate. If it establishes two distinct captures, record the duplicate effect as supported and continue testing which mechanism allowed it. The outcome evidence alone does not determine that mechanism.

A checkpoint retains branch states, evidence IDs, fixed windows, and the pending verification request. A resumed agent continues these questions without treating missing evidence as received. A later recovery check gets a new observation window.

## Richer-coverage variant shown in the images

The concept images show a related fictional investigation, INV-042, with a richer evidence snapshot than the incomplete-coverage walkthrough above. Its E-031/E-032 records include explicit delivery-attempt fields, and the second processing attempt logs dedupe_hit. Those fields support the narrower claim of repeated delivery as asserted by the collected application metadata; the log statement that deduplication occurred weakens the duplicate-effect hypothesis but does not establish the provider's final outcome. Inspect the documented delivery-field semantics before treating it as observed redelivery.

One logged provider reference, txn-p761, remains an application assertion until verified. req-b is another request identity to examine, not proof of a second client submission or charge. The image can mark the redelivery subclaim supported while the compound claim “redelivery created another charge” stays undetermined. Supports and contradicts must refer to those exact subclaims in the implemented UI.
