# Agent investigations: strategy, branches, and evidence

Status: current proposed concept, 18 November 2026. Sample data and interfaces are illustrative. No runtime changes or new commands are implemented by this package.

This concept develops the [lifecycle and architecture](lifecycle.md). The lifecycle specification’s persistence, authentication, redaction, cancellation, ownership, replay, retention, and agent-resume requirements still apply. This document defines the richer investigation behavior and uses three coordinated levels of detail.

## Product behavior

Every investigation connects an incident question to explicit branches and checks. A branch answers a scoped question; it can establish an observation, evaluate a possible cause, locate a coverage gap, or verify an outcome. Multiple causes and unrelated symptoms can coexist.

The user can follow this chain at any point:

**Question → hypothesis or alternative explanations → distinguishing check → saved evidence → assessment → next check.**

The connected agent chooses and revises the strategy. Graylog MCP executes bounded read-only searches and persists observable work. The workspace shows the recorded plan, evidence, and assessments. It does not expose private internal reasoning or invent a causal inference engine.

## Start broad, then distinguish explanations

1. Establish the environment, repository context, absolute incident window, baseline, and the reported versus observed symptoms.
2. Size and separate the symptoms. Group distinct error families and identify what evidence exists. Use exact aggregates where meaningful; label retrieved samples and their limits.
3. Select a strategy for each scoped question. Record why that strategy is useful with the data available.
4. Open a small set of plausible branches. Each branch must have an answerable question and either a next useful check or a named blocker. Keep alternative explanations visible.
5. Choose the next check by its ability to distinguish alternatives, available evidence, cost, and dependencies. Record expected observations before interpreting the result.
6. Run independent checks concurrently within the existing request limits. Do not infer a meaningful percentage complete from the number of branches.
7. Record what the result establishes, what remains unknown, and whether the next check or strategy changed. Preserve contradictory findings.
8. Stop or checkpoint when the question is answered, coverage is missing, a query budget is reached, or more scope/input is needed. A finished analysis may remain undetermined.

Strategy selection is an agent-authored, inspectable decision. It requires no additional model provider or fabricated confidence score. Reusing a recorded result avoids an unnecessary repeat query; a different scope or time window creates new evidence.

## Strategy catalogue and visual treatment

| Situation | Strategy and distinguishing checks | Workspace treatment | Limit to display |
|---|---|---|---|
| Known request or correlation ID | Follow explicit identifiers; inspect the first observed error and its surrounding records; test whether errors are upstream symptoms or local failures. | Request observations with selected branch and linked evidence. | Trace coverage and first observed error do not establish causation. |
| HTTP → queue → worker → payment | Expand from HTTP IDs to producer/consumer metadata; check message redelivery, acknowledgement, and processing attempts. | Service lanes, explicit queue boundary, identity chips, and separate attempts. | Matching a business key alone is a candidate association. |
| Retry or duplicate processing | Distinguish repeated logs, repeated executions, repeated provider requests, and repeated business effects. Inspect attempts and observed idempotency decisions. | Attempt tree with separate outcome markers; duplicate-effect verification branch. | A retry can be expected behavior and does not itself prove a duplicate side effect. |
| Several services fail together | Split symptoms by service/error family; compare affected and unaffected services; test a shared dependency or possible propagation path. | Symptom branches and a shared-dependency hypothesis, with links to supporting evidence. | An inferred dependency remains labeled inferred; earlier error onset is insufficient proof. |
| Rare or intermittent failure | Compare failed and successful cohorts using pre-outcome attributes such as version, host, or region; inspect representative cases from each. | Cohort table with failures/eligible operations, absolute counts, and samples. | Small samples, unequal coverage, confounders, or unknown denominators weaken comparison. |
| Nearby deploy or config change | Compare old/new versions and affected/unaffected instances; check onset and whether the error predates the change. | Change markers next to incident observations plus a hypothesis comparison. | Timing and before/after differences alone cannot isolate the change as cause. |
| Suspected race | Reconstruct overlapping operations; inspect explicit sequence/causation fields, locks, transaction versions, or state-transition evidence if logged. | Parallel observations with partial-order links and an ordering-unknown region. | Clock timestamps alone cannot establish a race or a total event order. |
| No matches or no trace ID | Reuse field diagnosis and counted suggestions; inspect samples for IDs; inspect contextual logs when correlation is unavailable. | Recovery branch with original/executed query and separate empty/no-ID/failed states. | Wider time or focus is an explicit scope revision; uncorrelated logs stay labeled. |
| Authoritative business outcome absent | Name the required source and request a specific verification from the user. | Blocked verification branch with a concrete evidence request. | Graylog log assertions are not a substitute for authoritative transaction or state records. |

Cohort comparisons should use attributes present before the outcome being compared. Record filters, eligible population, observation units, time, and exclusions; do not let outcome-derived fields or changed sampling silently define the cohorts. Findings may identify associations that deserve another check without proving causation.

## Three connected UI levels

### 1. Incident overview

Show the reported problem, observed impact, fixed scope/baseline, current strategy, active branches, unresolved questions, and missing evidence. Distinguish “Customer reports two charges” from “Two payment success log events observed”.

The branch list or compact map shows each question, current assessment, active or next check, and blocker. Selecting a branch focuses the next two levels. Keep unrelated error families separate. A shared evidence item may support several branches without being copied into independent corroborating sources.

### 2. Selected branch

Show the scoped question and competing explanations, then a compact check card:

- **Purpose:** “Compare attempts and message delivery metadata to distinguish a client resubmission from worker redelivery.”
- **Expected observations:** what would support each alternative, and what required coverage is absent.
- **Execution:** planned, running, succeeded, failed, interrupted, or cancelled.
- **Result:** the observed facts, evidence references, and limitations.
- **Assessment:** supported, contradicted in the examined scope, or undetermined, with a short explanation and coverage status.
- **Next useful check:** a bounded query, an explicit scope/input request, or an external evidence request.

Assessment is separate from execution. A successful tool call may leave all hypotheses undetermined. A failed check provides an execution failure, not evidence against a hypothesis. Missing data is not a contradictory observation.

### 3. Evidence detail

Display original and executed queries, effective instance/focus/streams, absolute window, identity types, exact versus sampled counts, saved excerpts, message references, and retention/coverage status. Let the user switch to request observations, attempts, cohort comparison, or a change timeline when the selected strategy needs it.

Each reported claim links to the smallest relevant evidence record. Log observations appear as dots. Measured intervals require explicit duration/span evidence. Uncertain links use a distinct line style and a visible label. The selected branch and check remain deep-linkable and survive refresh.

## Three distinct structures

Do not collapse these into one misleading graph:

1. **Investigation structure:** branches, checks, prerequisites, retries, and user requests. Arrows mean work dependency.
2. **Observed system relationships:** typed identities and explicitly observed propagation, with uncertain candidate associations kept separate. Link labels state the evidence basis.
3. **Evidence assessment:** a record supports, contradicts, or fails to resolve a particular claim under a recorded scope. An assessment is authored and revisioned.

The UI may coordinate these views through selection. It must always explain what an edge means. A hypothesis changes through explicit updates; drawing a new arrow cannot silently promote a candidate association into an observed relationship.

## Identity and ordering model

| Identity | Meaning | Example | Caution |
|---|---|---|---|
| Business operation | Domain operation under investigation | order-842 | Shared order ID groups records but does not establish request causality or uniqueness of payment effects. |
| HTTP request | One observed HTTP request | req-a | A retry can have another request ID; a service can also reuse an ID. |
| Message | Queue/broker message with its system namespace | queue-A / msg-91 | IDs can be reassigned or reused across systems; record queue/topic and producer/consumer context. |
| Processing attempt | One observed execution of a request/message | worker attempt 1 or 2 | Application attempt identity is separate from an MCP investigation execution attempt. |
| Idempotency key | Intended deduplication scope if observed | key-redacted | Presence alone does not prove enforcement or success. |
| Provider transaction | Provider-assigned transaction reference if present | provider-ref-A | A reference in a log is an assertion until checked against the required authoritative source. |
| Investigation attempt | One owned execution period of the agent investigation | run-2 | Keep it separate from business processing attempts and tool retries. |

Every identity records its type, namespace/source, redacted display value, and supporting evidence. Never join records solely on a redaction placeholder. Exact matching identities still need source semantics to establish the relationship being claimed; repeated copies of one event must not count as independent corroboration.

An observed propagation link requires explicit matching producer/consumer, parent/child, or equivalent application metadata with documented meaning. Same business key, similar text, or nearby timestamps create candidate associations. Partial-order edges require evidence of ordering semantics. Preserve timestamp source, precision, timezone, any clock uncertainty, and within-source order when known. Show only the ordering established by evidence.

## Extended records and contracts

Extend the lifecycle model with these proposed records:

| Record | Minimum contract |
|---|---|
| Branch | ID, investigation and scope-revision references, parent/dependencies/question/symptom scope, selected strategy and reason, alternatives, lifecycle state, assessment, coverage, next check or blocker, revision |
| Check | ID, investigation/scope-revision/branch references, purpose, alternatives distinguished, expected observations per alternative, required source/coverage, planned operation, scope/identity filters, query budget, stop condition |
| Check result | Check ID, execution step and operation-attempt references, investigation-run and scope-revision references, execution state, evidence IDs, observed facts, assessment author, alternative-specific implications, limitations, next check |
| Identity | Typed/qualified identifier, redacted display, evidence references, availability and replay restrictions |
| Relationship | ID, typed endpoints, relationship kind and direction (or explicitly symmetric), basis evidence, observed or candidate classification, scope revision, assessment history |
| Evidence request | Exact missing question/source, requester, state, received evidence/provenance, verification limits |
| Comparison | Population/cohort definitions, observation unit, eligible denominator, sampling/exclusions, scope/windows, counts, relevant confounders |

Use the proposed `investigation` lifecycle update for branch/check/assessment records instead of adding an unrelated tool for every strategy. Investigation-aware operations carry validated `investigation_id`, `branch_id`, and `check_id` references as needed. Membership, active owner, and scope revision are checked before execution. A shared check can reference multiple branches; its evidence remains one record.

Branch/check preparation is local state, recorded before a relevant operation begins. Server wrappers record observable operation start and outcome; the agent explicitly supplies check purpose, expectations, assessment, and next action. The server cannot infer a completed assessment merely from a successful operation. Existing tools without investigation context retain their behavior.

External verification is a request and evidence-ingestion contract, not a new payment, database, deployment, or ticket integration. A user can supply a redacted excerpt or reference through a supported interaction. Record its source, supplied-by identity, time, requested scope, and whether it is user-reported, unverified, or verified against an available authoritative source. Imported content follows the existing redaction, size, access, and untrusted-content rules. The agent never silently escalates data access or performs a payment action.

## Worked case and commands

The [duplicate-charge walkthrough](duplicate-charge-walkthrough.md) follows five branches through fictional observations and missing external evidence. The [command playbook](command-playbook.md) shows the corresponding conversation and proposed CLI interactions.

All briefings and exports include branch questions, coverage, competing explanations, completed checks, next checks, and blocked evidence requests. Resuming preserves original scope and evidence; it does not automatically rerun searches or assume missing external evidence has arrived.

## Scope and delivery

The first release should include the branch/check model, explicit check purpose and result, three UI levels, simple typed identity displays, evidence provenance, manual branching, and support for recording cohort/deploy/race limitations. Reuse existing Graylog operations; let the connected agent orchestrate the strategy. Missing evidence and undetermined outcomes are first-class states.

Automatic cross-system stitching, automatic race detection, definitive causal graph generation, autonomous external-source access, and model-derived confidence percentages are outside this concept's implementation promise. Rich strategy-specific views can be delivered incrementally: request observations and attempt lists first, then comparison and change views, then a relationship explorer. Stored contracts must preserve their semantics from the beginning.

The UI-steering phase remains later. In the initial workflow, users direct strategy changes in the conversation. A pinned or requested check in a future UI needs the explicit acknowledged delivery path already defined in the lifecycle specification.

## Acceptance checks for the strategy model

1. A branch has a scoped question, strategy/reason, and next useful check or blocker; its status cannot be inferred from the tool's success flag.
2. A check records alternatives and expected observations before execution. If the agent omitted that information, show it as an unplanned/ad hoc step rather than fabricating a rationale.
3. The same saved evidence can be referenced by multiple branches without appearing as multiple independent confirmations.
4. Two success log events never become a claim of two financial charges without the required outcome evidence.
5. Business, HTTP, message, processing-attempt, provider, and investigation-run identifiers remain distinguishable and qualified by source.
6. Missing or redacted identity fields cannot create false joins, and full-text trace fallback is labeled as a matching strategy rather than proof of identity semantics.
7. A candidate link stays uncertain until an explicit evidence-backed update; time order alone never promotes it.
8. Async attempts and tool retries are different records. Redelivery is an observation to investigate, not an automatic fault verdict.
9. A cohort view shows eligible counts, units, exclusions, and missing coverage; unknown denominators suppress unsupported request error rates.
10. Deploy comparisons include contradictory evidence and affected/unaffected coverage; an earlier rollout marker does not become causal proof.
11. A race branch can end “ordering unknown” while retaining the observations and requested missing evidence.
12. External evidence requests preserve source/provenance and remain blocked until received; user assertions are not silently relabeled verified facts.
13. Refresh, checkpoint, export, and resume preserve branch assessments and next checks under their original scope revisions.
14. A check budget or unavailable source produces a clear stopping reason and a useful handoff, without silently widening scope.
15. The investigation remains understandable at the overview, branch, and evidence levels without requiring the user to inspect every tool call.

### Required scenario fixtures

Validate the model with two deliveries and one verified business effect; two separately published messages for the same order; missing broker metadata; version cohorts with unequal traffic and collection coverage; two independent incident causes; unresolved concurrency order; and a conclusion waiting on authoritative outcome evidence. Each fixture must retain its distinct identities, count units, alternatives, and stopping reason across checkpoint and resume.
