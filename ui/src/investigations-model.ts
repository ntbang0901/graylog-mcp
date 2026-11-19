// What the Investigations screen derives from the records, with no page, store or network in it: the stream's frames
// and cursor, the words for a state, and how steps and checks sit under branches. Tested in Node (tests/).
import type {
    Fields,
    InvEvent,
    InvEvidence,
    InvItem,
    InvScope,
    InvSnapshot,
    InvStep,
    InvSummary,
} from "./scripts/types";

// ------------------------------------------------------------------ the event stream
export interface Frame {
    event: string;
    id: string | null;
    data: string;
}
// Server-Sent Events: frames end at a blank line; a line starting with ":" is a comment (the keep-alive)
export function parseFrames(buffer: string): { frames: Frame[]; rest: string } {
    const frames: Frame[] = [];
    const parts = buffer.replace(/\r\n/g, "\n").split("\n\n");
    const rest = parts.pop() ?? "";
    for (const part of parts) {
        let event = "message",
            id: string | null = null;
        const data: string[] = [];
        let any = false;
        for (const line of part.split("\n")) {
            if (!line || line.startsWith(":")) continue;
            const at = line.indexOf(":"),
                name = at < 0 ? line : line.slice(0, at),
                value = at < 0 ? "" : line.slice(at + 1).replace(/^ /, "");
            if (name === "event") event = value;
            else if (name === "id") id = value;
            else if (name === "data") data.push(value);
            else continue;
            any = true;
        }
        if (any) frames.push({ event, id, data: data.join("\n") });
    }
    return { frames, rest };
}
// The events a stream delivers after the cursor: each once, in order. A first one that is not cursor + 1 means
// events were missed (or dropped by retention): the snapshot is fetched again instead of guessing.
export function advance(
    cursor: number,
    events: InvEvent[],
): { fresh: InvEvent[]; cursor: number; gap: boolean } {
    const fresh = [...events]
        .filter((e) => e.seq > cursor)
        .sort((a, b) => a.seq - b.seq);
    let at = cursor;
    for (const e of fresh) {
        if (e.seq !== at + 1)
            return {
                fresh: fresh.filter((x) => x.seq <= at),
                cursor: at,
                gap: true,
            };
        at = e.seq;
    }
    return { fresh, cursor: at, gap: false };
}
// seconds to wait before reconnecting after the stream broke: 1, 2, 4 ... 30
export const backoff = (attempt: number) =>
    Math.min(30, 2 ** Math.max(0, attempt));

// ------------------------------------------------------------------ words
export const STATE_WORD: Record<string, string> = {
    draft: "Not started",
    running: "Running",
    awaiting_input: "Waiting for input",
    interrupted: "Interrupted",
    completed: "Completed",
    cancelled: "Cancelled",
};
export const STATE_CLASS: Record<string, string> = {
    running: "info",
    awaiting_input: "warn",
    interrupted: "warn",
    completed: "ok",
    cancelled: "",
};
export const STEP_WORD: Record<string, string> = {
    running: "Running",
    succeeded: "Succeeded",
    failed: "Failed",
    cancelled: "Cancelled",
    interrupted: "Interrupted",
};
export const STEP_CLASS: Record<string, string> = {
    running: "info",
    succeeded: "ok",
    failed: "fail",
    cancelled: "",
    interrupted: "warn",
};
export const OUTCOME_WORD: Record<string, string> = {
    explanation_supported: "Explanation supported",
    cause_undetermined: "Cause undetermined",
    no_matching_evidence: "No matching evidence",
    no_anomaly_detected: "No anomaly detected",
};
export const ASSESSMENT_WORD: Record<string, string> = {
    supported: "Supported",
    contradicted_in_scope: "Contradicted in scope",
    undetermined: "Undetermined",
};
export const stateWord = (s: string) => STATE_WORD[s] ?? s;
// an investigation that wants a person: the agent waits for input, or its work was cut off
export const needsAttention = (i: Pick<InvSummary, "state">) =>
    i.state === "awaiting_input" || i.state === "interrupted";

export function windowText(s: Pick<InvScope, "from" | "to" | "timezone">) {
    const cut = (t: string) =>
        t.replace("T", " ").replace(/:\d\d(\.\d+)?(Z|[+-]\d\d:\d\d)?$/, "");
    return `${cut(s.from)} → ${cut(s.to)} ${s.timezone}`;
}
// "12 min left" / "expired" for an owner's lease; "" when nobody owns it
export function leaseText(lease: string | null, now = Date.now()) {
    if (!lease) return "";
    const left = Date.parse(lease) - now;
    if (Number.isNaN(left)) return "";
    if (left <= 0) return "expired";
    const min = Math.ceil(left / 60000);
    return min < 60 ? `${min} min left` : `${Math.round(min / 60)} h left`;
}
export function countText(e: {
    exact_count: number | null;
    returned: number | null;
    sampled: boolean;
}) {
    const exact =
        e.exact_count === null ? null : e.exact_count.toLocaleString("en-US");
    if (exact !== null && e.returned !== null && e.sampled)
        return `${exact} matched · ${e.returned.toLocaleString("en-US")} shown`;
    if (exact !== null) return `${exact} matched`;
    if (e.returned !== null)
        return `${e.returned.toLocaleString("en-US")} shown, total not given`;
    return "no count";
}

// ------------------------------------------------------------------ the workspace
export const itemsOf = (snap: InvSnapshot, kind: string): InvItem[] =>
    snap.items.filter((i) => i.kind === kind);
export const bodyText = (i: InvItem, ...names: string[]) => {
    for (const n of names)
        if (typeof i.data[n] === "string") return i.data[n] as string;
    return "";
};
export interface CheckView {
    item: InvItem;
    steps: InvStep[];
}
export interface BranchView {
    item: InvItem;
    checks: CheckView[];
}
export interface Workspace {
    branches: BranchView[];
    loose: CheckView[]; // checks of no branch
    adHoc: InvStep[]; // steps of no check: shown as what they are, never with an invented reason
    assessments: InvItem[];
    hypotheses: InvItem[];
}
export function workspaceOf(snap: InvSnapshot): Workspace {
    const stepsOf = (id: string) => snap.steps.filter((s) => s.check === id);
    const checks = itemsOf(snap, "check").map((item) => ({
        item,
        steps: stepsOf(item.id),
    }));
    const listed = (c: CheckView) =>
        Array.isArray(c.item.data.branches)
            ? (c.item.data.branches as string[])
            : [];
    const branches = itemsOf(snap, "branch").map((item) => ({
        item,
        checks: checks.filter((c) => listed(c).includes(item.id)),
    }));
    const known = new Set(checks.map((c) => c.item.id));
    return {
        branches,
        loose: checks.filter((c) => !listed(c).length),
        adHoc: snap.steps.filter((s) => !s.check || !known.has(s.check)),
        assessments: itemsOf(snap, "assessment"),
        hypotheses: itemsOf(snap, "hypothesis"),
    };
}
// ------------------------------------------------------------------ checks, results and what cites what
export type CheckState = "not_run" | "running" | "failed" | "done";
export const CHECK_WORD: Record<CheckState, string> = {
    not_run: "Not run",
    running: "Running",
    failed: "Failed",
    done: "Done",
};
export const CHECK_CLASS: Record<CheckState, string> = {
    not_run: "",
    running: "info",
    failed: "fail",
    done: "ok",
};
// one word for a check from its steps: it is done when a step gave evidence, failed when every step ended without
export function checkState(steps: InvStep[]): CheckState {
    if (!steps.length) return "not_run";
    if (steps.some((s) => s.state === "running")) return "running";
    return steps.some((s) => s.state === "succeeded") ? "done" : "failed";
}
export const evidenceOfSteps = (steps: InvStep[]): string[] => [
    ...new Set(steps.flatMap((s) => s.evidence)),
];
// what the agent wrote down as the result of one check
export const resultsOf = (snap: InvSnapshot, check: string): InvItem[] =>
    itemsOf(snap, "check_result").filter((r) => r.data.check === check);
// assessments that rest on any of these evidence ids
export const citingAssessments = (
    snap: InvSnapshot,
    ids: string[],
): InvItem[] =>
    itemsOf(snap, "assessment").filter((a) =>
        citesOf(a).some((c) => ids.includes(c)),
    );
export interface CitedBy {
    supports: InvItem[];
    contradicts: InvItem[];
    noted: InvItem[]; // undetermined: looked at it, concluded nothing from it
}
export function citedBy(snap: InvSnapshot, evidence: string): CitedBy {
    const out: CitedBy = { supports: [], contradicts: [], noted: [] };
    for (const a of itemsOf(snap, "assessment")) {
        if (!citesOf(a).includes(evidence)) continue;
        const against = Array.isArray(a.data.contradicting)
            ? (a.data.contradicting as string[])
            : [];
        if (against.includes(evidence)) out.contradicts.push(a);
        else if (a.data.assessment === "undetermined") out.noted.push(a);
        else out.supports.push(a);
    }
    return out;
}
// what is still to do: a question the user must answer first, else the first check that has not run
export function nextCheck(
    snap: InvSnapshot,
): { kind: "blocked" | "check"; text: string } | null {
    const ask = itemsOf(snap, "evidence_request").find(
        (r) => r.data.status === "requested",
    );
    if (ask) return { kind: "blocked", text: bodyText(ask, "question") };
    const ran = new Set(snap.steps.map((s) => s.check));
    const next = itemsOf(snap, "check").find((c) => !ran.has(c.id));
    return next ? { kind: "check", text: bodyText(next, "purpose") } : null;
}
// a finished report: the agent's `report` is an object (findings, limits, next_checks) or a sentence
const lines = (v: unknown): string[] =>
    Array.isArray(v)
        ? v.filter((x): x is string => typeof x === "string")
        : typeof v === "string" && v
          ? [v]
          : [];
export function reportOf(r: Fields) {
    const body: Fields =
        r.report && typeof r.report === "object" && !Array.isArray(r.report)
            ? (r.report as Fields)
            : {};
    const text =
        typeof r.report === "string"
            ? r.report
            : typeof body.summary === "string"
              ? body.summary
              : "";
    return {
        outcome: typeof r.outcome === "string" ? r.outcome : "",
        findings: lines(body.findings),
        limits: lines(body.limits),
        next: lines(body.next_checks),
        text,
    };
}
// branches whose question has no assessment yet, and the checks that have not run: the overview's "still open"
export function openQuestions(ws: Workspace): BranchView[] {
    const assessed = new Set(
        ws.assessments.flatMap((a) =>
            typeof a.data.branch === "string" ? [a.data.branch] : [],
        ),
    );
    return ws.branches.filter((b) => !assessed.has(b.item.id));
}
export const stepsByAttempt = (snap: InvSnapshot) => {
    const order = new Map(snap.attempts.map((a) => [a.id, a.n]));
    const groups = new Map<string, InvStep[]>();
    for (const s of snap.steps)
        groups.set(s.attempt, [...(groups.get(s.attempt) ?? []), s]);
    return [...groups.entries()]
        .sort((a, b) => (order.get(a[0]) ?? 0) - (order.get(b[0]) ?? 0))
        .map(([attempt, steps]) => ({
            attempt,
            n: order.get(attempt) ?? 0,
            steps,
        }));
};

// ------------------------------------------------------------------ evidence
// the query as asked beside the one that ran, and why they differ
export function queryPair(e: Pick<InvEvidence, "step">) {
    const asked = e.step?.requested?.query,
        ran =
            e.step?.executed?.effective_query ??
            e.step?.executed?.query ??
            asked;
    return {
        asked: typeof asked === "string" ? asked : null,
        ran: typeof ran === "string" ? ran : null,
        differs:
            typeof asked === "string" &&
            typeof ran === "string" &&
            asked !== ran,
    };
}
// the evidence of a search can be opened in the Logs screen with the same window
export function logsViewOf(e: InvEvidence) {
    const ex = e.step?.executed ?? {},
        rq = e.step?.requested ?? {},
        query = ex.effective_query ?? ex.query ?? rq.query;
    if (
        ![
            "search_logs",
            "count_logs",
            "top_values",
            "log_histogram",
            "error_summary",
        ].includes(e.kind) ||
        typeof query !== "string"
    )
        return null;
    return {
        instance: e.step?.instance ?? "",
        query,
        from: typeof ex.from === "string" ? ex.from : undefined,
        to: typeof ex.to === "string" ? ex.to : undefined,
        range: typeof ex.range === "string" ? ex.range : undefined,
    };
}

// ------------------------------------------------------------------ the address
// ROOT/investigations/<id>/<branch>/<check>/<evidence>; "-" stands for a part not chosen
export interface Selection {
    id: string | null;
    branch: string | null;
    check: string | null;
    evidence: string | null;
}
export function selectionPath(s: Selection): string[] {
    if (!s.id) return [];
    const parts = [s.id, s.branch ?? "-", s.check ?? "-", s.evidence ?? "-"];
    while (parts.length > 1 && parts[parts.length - 1] === "-") parts.pop();
    return parts;
}
export function selectionOf(sub: string[]): Selection {
    const at = (i: number) => (sub[i] && sub[i] !== "-" ? sub[i] : null);
    return { id: at(0), branch: at(1), check: at(2), evidence: at(3) };
}

// ------------------------------------------------------------------ findings
export const CITE_FIELDS = ["evidence", "supporting", "contradicting"];
export const citesOf = (i: InvItem): string[] =>
    CITE_FIELDS.flatMap((f) =>
        Array.isArray(i.data[f]) ? (i.data[f] as string[]) : [],
    );
// evidence cited by more than one record: one saved piece, counted once, marked where it is shared
export function sharedEvidence(snap: InvSnapshot): Map<string, string[]> {
    const by = new Map<string, string[]>();
    for (const i of snap.items)
        for (const e of new Set(citesOf(i)))
            by.set(e, [...(by.get(e) ?? []), i.id]);
    return new Map([...by].filter(([, ids]) => ids.length > 1));
}
export const LINK_WORD: Record<string, string> = {
    candidate: "Candidate: not established",
    observed: "Observed",
};
export const REQUEST_WORD: Record<string, string> = {
    requested: "Waiting for an answer",
    received: "Received",
    withdrawn: "Withdrawn",
};
export const PROVENANCE_WORD: Record<string, string> = {
    user_reported: "reported by the user",
    unverified: "unverified",
    verified: "verified",
};
export interface Cohort {
    name: string;
    count: number | null;
    eligible: number | null;
    rate: string; // "" when there is nothing to say; "unknown denominator" when there is no eligible count
}
// a rate is shown only over a known denominator
export function cohortsOf(i: InvItem): Cohort[] {
    const raw = Array.isArray(i.data.cohorts)
        ? (i.data.cohorts as Record<string, unknown>[])
        : [];
    return raw.map((c) => {
        const eligible =
            typeof c.denominator === "number" ? c.denominator : null;
        return {
            name: String(c.name ?? ""),
            count: typeof c.count === "number" ? c.count : null,
            eligible,
            rate:
                eligible === null
                    ? "unknown denominator"
                    : typeof c.rate === "number"
                      ? `${(c.rate * 100).toFixed(1)}%`
                      : "",
        };
    });
}
// requests that hold a branch up
export const blockedBranches = (snap: InvSnapshot): Set<string> =>
    new Set(
        itemsOf(snap, "evidence_request")
            .filter(
                (r) =>
                    r.data.status === "requested" &&
                    typeof r.data.branch === "string",
            )
            .map((r) => r.data.branch as string),
    );
