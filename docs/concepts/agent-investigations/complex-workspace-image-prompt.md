# Complex investigation workspace image prompt

Use case: ui-mockup.
Asset type: polished high-fidelity developer application concept image, generated with built-in imagegen.

Create one substantial, readable, sophisticated wide desktop application design for graylog-mcp. This is a proposed investigation workspace with fictional data, not a generic dashboard or a list of tool calls. Wide landscape composition, crisp flat straight-on UI, no perspective or device frame, no surrounding explanatory poster. Rich but calm information architecture with clearly differentiated THREE LAYERS: incident overview -> selected hypothesis branch -> evidence inspector. Strong mature developer tool styling: deep slate navy chrome, layered ink surfaces, crisp light text, restrained teal selection, amber uncertainty, coral genuine failures, fine neutral dividers, monospaced identifiers, sensible density and readable type. No neon glow, glossy effects, fake confidence scores, invented duration bars, or decorative statistics.

Full-width product header: graylog-mcp / Investigations / INV-042. Small top-right label “Proposed feature · Sample data”. Main title “Customer charged twice, checkout returned 500”. Under title a small amber qualifier “Customer report · duplicate charge not verified”. Metadata row “checkout / prod”, “18 Nov 2026 · 09:30–09:45 ICT”, “order-842”. Persistent state pills “Cause undetermined” and “Fix not verified”.

The main workspace has three strongly organized columns with headings “Incident overview”, “Selected hypothesis”, and “Evidence inspector”. Approximately 27%, 39%, and 34% width, with a slim app navigation rail on the far left. Highlight Investigations in the navigation. Avoid extra nav items or large blank space.

LEFT COLUMN: a compact incident context strip “Business operation: order-842” and “HTTP requests: req-a, req-b”. Small exact note “Same business key does not prove causation”. A strategy tree headed “Questions to distinguish” with five branching readable hypotheses:
1. “Client duplicate submission” — “Open”.
2. “Gateway retry” — “Retry metadata missing”.
3. “Worker redelivery” — selected teal, “Observed”.
4. “Payment succeeded before timeout” — “Log evidence”.
5. “Idempotency handling” — “Needs verification”.
Arrows here are explicitly work dependencies, not application calls. Bottom compact strategy shortcuts “No trace ID”, “Compare cohort”, “Compare deploy”. Label them “Other investigation routes”.

CENTER COLUMN: a selected branch titled “Did redelivery create another charge?” with hypothesis ID H-03. A clearly labeled short rationale block “Why this check” with text “Distinguish repeated delivery from repeated payment.” Below, a branching sequence of three useful checks: “Match message identity” completed, evidence E-031; “Inspect both delivery attempts” completed, evidence E-032; “Verify provider transactions” amber “External evidence needed”. Show a small mini-ledger with columns “Observed event” and “Assessment”: msg-91 · attempt 1 — “Payment success logged”; msg-91 · attempt 2 — “Dedupe hit logged”. State label “Observed assessment”. Main assessment text “Redelivery observed; a second charge is unverified.” Under it a compact “Next distinguishing check” with text “Compare authoritative provider transactions for order-842.” A secondary linked branch “Inspect req-b and idempotency scope” remains open. This must feel like an explicit evolving investigation strategy, not automatic root-cause generation.

RIGHT COLUMN: selected evidence heading “E-032 · delivery attempts”. Bounded query area msg_id:"msg-91". Scope line “Saved logs · prod · 09:30–09:45 ICT”. Three clearly distinct expandable-looking evidence groups with concise readable contents:
“Supports”: “Same message ID, delivery attempts 1 and 2”.
“Contradicts”: “Attempt 2 reports dedupe hit”.
“Gaps”: “Provider transaction ledger unavailable”; “Idempotency key scope unknown”.
Below, two short exact log excerpts: “attempt=1 txn=txn-p761 payment_success”; “attempt=2 dedupe_hit”. Add a neutral source badge “Application logs” and amber badge “Not a settlement record”. A note “Repeated success logs do not establish two charges.” Show an action “Open async attempts” in teal outline and one evidence reference link E-031. Display typed evidence and clear source limitations, not a claimed financial conclusion.

BOTTOM full-width narrow persistent bar, visually subordinate: “Current next check” → “Obtain authoritative transaction records” → amber state “Awaiting external evidence”. On the right a small “Save briefing” button. No deploy button, no claim of fixed or verified payment, no automatic external access, no fake completion score. Main takeaway: the workspace exposes the incident, competing questions, rationale, observed assessment, evidence limits, and next useful check with enough detail to reason about a complex case.

Exact identifiers to preserve: order-842, req-a, req-b, msg-91, txn-p761. No second transaction identifier is known. Do not invent one. Never imply two payment log lines establish two settled charges. Small typography remains genuinely readable and coherent. No watermark.
