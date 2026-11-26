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
    running: "Investigating",
    awaiting_input: "Needs your input",
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

const MONTHS = [
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
];
// The zone every time on the page is read in: the browser's, so what the page says matches the reader's clock
export function viewZone() {
    try {
        return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
    } catch {
        return "UTC";
    }
}
// A UTC instant read in a time zone (an IANA name; the browser's unless one is given). A name this browser does not
// know reads as UTC, and says so, instead of passing the clock of one zone off as another.
export interface Zoned {
    ymd: string;
    day: string; // "18 Nov"
    time: string; // "09:30"
    seconds: string; // "09:30:07"
    zone: string; // "GMT+7"
}
export function zoned(
    iso: string | null | undefined,
    tz: string = viewZone(),
): Zoned | null {
    const at = iso ? Date.parse(iso.replace(" ", "T")) : NaN;
    if (Number.isNaN(at)) return null;
    const read = (timeZone: string) =>
        new Intl.DateTimeFormat("en-US", {
            timeZone,
            hourCycle: "h23",
            year: "numeric",
            month: "numeric",
            day: "numeric",
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
            timeZoneName: "short",
        }).formatToParts(at);
    let parts: Intl.DateTimeFormatPart[];
    try {
        parts = read(tz || "UTC");
    } catch {
        parts = read("UTC");
    }
    const get = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
    const two = (v: string) => v.padStart(2, "0");
    const time = `${two(get("hour"))}:${two(get("minute"))}`;
    return {
        ymd: `${get("year")}-${two(get("month"))}-${two(get("day"))}`,
        day: `${Number(get("day"))} ${MONTHS[Number(get("month")) - 1]}`,
        time,
        seconds: `${time}:${two(get("second"))}`,
        zone: get("timeZoneName"),
    };
}
// when a check ran, kept short: the time of day alone for today, with the day for an earlier one; the seconds and the
// zone only in the full text, for telling apart checks that ran close together
export function ranAt(iso: string | null | undefined, now = Date.now()) {
    const at = zoned(iso),
        today = zoned(new Date(now).toISOString());
    if (!at || !today) return null;
    const day = at.ymd === today.ymd ? "" : `${at.day} `;
    return {
        text: `${day}${at.time}`,
        full: `${at.day} ${at.seconds} ${at.zone}`,
    };
}
// "18 Nov 09:30 → 09:45 GMT+7": the date once when both ends fall on one day
export function windowText(s: Pick<InvScope, "from" | "to">) {
    const a = zoned(s.from),
        b = zoned(s.to);
    if (!a || !b) return `${s.from} → ${s.to}`;
    return `${a.day} ${a.time} → ${a.ymd === b.ymd ? "" : `${b.day} `}${b.time} ${b.zone}`;
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
    const id = e.step?.requested?.trace_id,
        // a request is asked for by its id: shown as the query that finds it
        asked =
            e.step?.requested?.query ??
            (typeof id === "string" && id ? `trace_id:"${id}"` : undefined),
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
// the window a step ran over: the absolute one it recorded (an object, from the tool's header), else its own from/to
function ranOver(ex: Fields): { from?: string; to?: string; range?: string } {
    const r = ex.range;
    if (r && typeof r === "object") {
        const w = r as { from?: unknown; to?: unknown };
        if (typeof w.from === "string" && typeof w.to === "string")
            return {
                from: w.from.replace(" ", "T"),
                to: w.to.replace(" ", "T"),
            };
    }
    return {
        from: typeof ex.from === "string" ? ex.from : undefined,
        to: typeof ex.to === "string" ? ex.to : undefined,
        range: typeof r === "string" ? r : undefined,
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
    return { instance: e.step?.instance ?? "", query, ...ranOver(ex) };
}
// a request's whole path, in the Logs screen's Traces tab
export function fullRequestOf(e: InvEvidence) {
    const id = e.step?.requested?.trace_id;
    if (e.kind !== "trace_request" || typeof id !== "string" || !id)
        return null;
    return {
        instance: e.step?.instance ?? "",
        query: "*",
        trace: id,
        ...ranOver(e.step?.executed ?? {}),
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

// ------------------------------------------------------------------ evidence numbers, time and tokens
// the evidence's place in this investigation, "E-003": one counter per investigation, so a resumed run continues it
export function evidenceNo(snap: Pick<InvSnapshot, "evidence">, id: string) {
    const at = snap.evidence.findIndex((e) => e.id === id);
    return at < 0 ? id : `E-${String(at + 1).padStart(3, "0")}`;
}
// more than three tokens: the first two and "+n"
export function tokensOf(ids: string[]) {
    return ids.length > 3
        ? { shown: ids.slice(0, 2), more: ids.length - 2 }
        : { shown: ids, more: 0 };
}
// 1200 -> "+1.2 s"; the sign is kept because a service can go wrong before the request starts
export function offsetText(ms: number): string {
    const sign = ms < 0 ? "-" : "+",
        a = Math.abs(ms);
    return `${sign}${a < 1000 ? `${Math.round(a)} ms` : `${(a / 1000).toFixed(a < 10000 ? 1 : 0)} s`}`;
}
export const msText = (ms: number) => offsetText(ms).replace(/^\+/, "");
// "12 s" for a step that is still running
export function elapsedText(from: string | null, now = Date.now()) {
    const ms = from ? now - Date.parse(from) : NaN;
    if (Number.isNaN(ms) || ms < 0) return "";
    const s = Math.round(ms / 1000);
    return s < 60
        ? `${s} s`
        : s < 3600
          ? `${Math.floor(s / 60)} min ${s % 60} s`
          : `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
}

// ------------------------------------------------------------------ the task flow
// One node per check, and one for each step that ran with no check recorded. Checks of one branch sit side by side
// and meet again at the next node; with no branch recorded, nodes whose steps overlapped in time sit side by side.
export interface FlowNode {
    key: string;
    check: string | null;
    title: string;
    steps: InvStep[];
    state: CheckState;
    tools: string[];
    evidence: string[];
    result: string;
    note?: string;
    started: string | null;
    ended: string | null; // null while a step runs
}
export interface FlowRow {
    key: string;
    caption?: string; // the branch's question
    nodes: FlowNode[];
}
export interface Flow {
    rows: FlowRow[];
    asks: InvItem[]; // what the agent waits for: always the last node
}
export const MAX_PARALLEL = 3;
export const MAX_VISIBLE_NODES = 12;
// a count from the first evidence of a check, when the agent wrote no result sentence
function evidenceCount(snap: InvSnapshot, ids: string[]): string {
    const row = snap.evidence.find((e) => ids.includes(e.id));
    return row && (row.exact_count !== null || row.returned !== null)
        ? countText(row)
        : "";
}
const stamp = (iso: string | null) => (iso ? Date.parse(iso) : NaN);
export function flowOf(snap: InvSnapshot, now = Date.now()): Flow {
    const ws = workspaceOf(snap);
    const node = (
        key: string,
        check: string | null,
        title: string,
        steps: InvStep[],
        result: string,
        note?: string,
    ): FlowNode => {
        const evidence = evidenceOfSteps(steps),
            starts = steps
                .map((s) => stamp(s.started))
                .filter((t) => !Number.isNaN(t)),
            running = steps.some((s) => s.state === "running"),
            ends = steps
                .map((s) => stamp(s.ended))
                .filter((t) => !Number.isNaN(t));
        return {
            key,
            check,
            title,
            steps,
            state: checkState(steps),
            tools: [...new Set(steps.map((s) => s.operation))],
            evidence,
            result: result || evidenceCount(snap, evidence),
            note,
            started: starts.length
                ? (steps.find((s) => stamp(s.started) === Math.min(...starts))
                      ?.started ?? null)
                : null,
            ended:
                running || !ends.length
                    ? null
                    : (steps.find((s) => stamp(s.ended) === Math.max(...ends))
                          ?.ended ?? null),
        };
    };
    const checkNode = (c: CheckView) =>
        node(
            c.item.id,
            c.item.id,
            bodyText(c.item, "purpose"),
            c.steps,
            String(resultsOf(snap, c.item.id)[0]?.data.result ?? ""),
        );
    const claimed = new Set<string>();
    type Unit = { at: number; order: number; row: FlowRow; free: boolean };
    const units: Unit[] = [];
    let order = 0;
    const startOf = (n: FlowNode) => {
        const t = stamp(n.started);
        return Number.isNaN(t) ? Infinity : t;
    };
    // steps are stored in the order they began, and start times only have whole seconds: of two checks that began in
    // the same second, the one whose step came first goes first
    const place = new Map(snap.steps.map((s, i) => [s.id, i]));
    const seqOf = (n: FlowNode) =>
        Math.min(...n.steps.map((s) => place.get(s.id) ?? Infinity));
    const byStart = (x: FlowNode, y: FlowNode) =>
        startOf(x) - startOf(y) || seqOf(x) - seqOf(y);
    for (const b of ws.branches) {
        const nodes = b.checks
            .filter((c) => !claimed.has(c.item.id))
            .map((c) => (claimed.add(c.item.id), checkNode(c)))
            .sort(byStart);
        if (nodes.length)
            units.push({
                at: startOf(nodes[0]),
                order: order++,
                free: false,
                row: {
                    key: `b:${b.item.id}`,
                    caption: bodyText(b.item, "question"),
                    nodes,
                },
            });
    }
    const free: FlowNode[] = [
        ...ws.loose.filter((c) => !claimed.has(c.item.id)).map(checkNode),
        ...ws.adHoc.map((s) =>
            node(
                `s:${s.id}`,
                null,
                String(s.requested.query ?? s.operation),
                [s],
                "",
                "no check recorded",
            ),
        ),
    ].sort(byStart);
    // overlapping in time is the only evidence two checks ran in parallel
    let current: { row: FlowRow; end: number } | null = null;
    for (const n of free) {
        const begin = startOf(n),
            finish = n.ended
                ? stamp(n.ended)
                : n.state === "running"
                  ? now
                  : begin;
        if (
            current &&
            Number.isFinite(begin) &&
            begin < current.end &&
            current.row.nodes.length < MAX_PARALLEL
        ) {
            current.row.nodes.push(n);
            current.end = Math.max(current.end, finish);
            continue;
        }
        const row: FlowRow = { key: n.key, nodes: [n] };
        units.push({ at: begin, order: order++, free: true, row });
        current = Number.isFinite(begin) ? { row, end: finish } : null;
    }
    units.sort(
        (a, b) =>
            a.at - b.at ||
            seqOf(a.row.nodes[0]) - seqOf(b.row.nodes[0]) ||
            a.order - b.order,
    );
    return {
        rows: units.map((u) => u.row),
        asks: itemsOf(snap, "evidence_request").filter(
            (r) => r.data.status === "requested",
        ),
    };
}
export const nodesIn = (rows: FlowRow[]) =>
    rows.reduce((n, r) => n + r.nodes.length, 0);
// With more than a dozen nodes the oldest finished ones fold into "n earlier checks"; a row with a node that is
// running, failed or not yet run stays out of the fold
export function foldEarlier(rows: FlowRow[], keep = MAX_VISIBLE_NODES) {
    let left = nodesIn(rows),
        at = 0;
    while (
        at < rows.length &&
        left > keep &&
        rows[at].nodes.every((n) => n.state === "done")
    ) {
        left -= rows[at].nodes.length;
        at++;
    }
    return { earlier: rows.slice(0, at), rest: rows.slice(at) };
}

// ------------------------------------------------------------------ working explanations
export interface Explained {
    key: string;
    state: "open" | "supported" | "ruled_out" | "undetermined";
    word: string;
    text: string;
    evidence: string[];
}
export const EXPLAINED_WORD: Record<Explained["state"], string> = {
    open: "checking",
    supported: "supported",
    ruled_out: "ruled out in scope",
    undetermined: "undetermined",
};
// what the agent assessed or opened; a hypothesis it also assessed is shown once, as the assessment
export function explainedOf(snap: InvSnapshot): Explained[] {
    const out: Explained[] = itemsOf(snap, "assessment").map((a) => {
        const state =
            a.data.assessment === "supported"
                ? "supported"
                : a.data.assessment === "contradicted_in_scope"
                  ? "ruled_out"
                  : "undetermined";
        return {
            key: a.id,
            state,
            word: EXPLAINED_WORD[state],
            text: bodyText(a, "claim"),
            evidence: [...new Set(citesOf(a))],
        };
    });
    const said = new Set(out.map((x) => x.text.trim().toLowerCase()));
    for (const h of itemsOf(snap, "hypothesis")) {
        const text = bodyText(h, "statement");
        if (said.has(text.trim().toLowerCase())) continue;
        out.push({
            key: h.id,
            state: "open",
            word: EXPLAINED_WORD.open,
            text,
            evidence: [...new Set(citesOf(h))],
        });
    }
    return out;
}
// why the state says "Interrupted": how the last attempt ended
export function interruptedWhy(snap: InvSnapshot): string {
    if (snap.state !== "interrupted") return "";
    const ended = [...snap.attempts].reverse().find((a) => a.end_reason);
    return (ended?.end_reason ?? "").replace(/_/g, " ");
}
// the record says the analysis is finished, and whether a fix was checked
export const analysisComplete = (snap: Pick<InvSnapshot, "state">) =>
    snap.state === "completed";
export const fixNotVerified = (
    snap: Pick<InvSnapshot, "state" | "verification">,
) =>
    snap.state === "completed" &&
    ["not_requested", "pending", "inconclusive"].includes(snap.verification);

// ------------------------------------------------------------------ the evidence inspector
// "req-7f3a" for a request, else the kind: what the evidence is about
export function aboutOf(e: InvEvidence): string {
    const id = e.step?.requested?.trace_id;
    return typeof id === "string" && id ? id : e.kind;
}
// the absolute window the query ran over: what the step recorded, else the window of the scope it ran under
export function windowOfEvidence(e: InvEvidence, snap: InvSnapshot): string {
    const ex = e.step?.executed ?? {},
        range = ex.range as
            | { from?: unknown; to?: unknown }
            | string
            | undefined,
        pick = (from: unknown, to: unknown) =>
            typeof from === "string" && typeof to === "string"
                ? windowText({ from, to })
                : "";
    const got =
        (range && typeof range === "object"
            ? pick(range.from, range.to)
            : "") || pick(ex.from, ex.to);
    if (got) return got;
    const rev = snap.scope_revisions.find((r) => r.rev === e.scope_rev);
    return rev ? pick(rev.from, rev.to) : "";
}
export const ERROR_WORDS = new Set([
    "ERROR",
    "ERR",
    "FATAL",
    "CRITICAL",
    "CRIT",
    "SEVERE",
    "PANIC",
    "EMERG",
    "EMERGENCY",
    "ALERT",
]);
export function isErrorLevel(level: unknown): boolean {
    if (typeof level === "number") return level <= 3;
    const text = String(level ?? "")
        .trim()
        .toUpperCase();
    return /^\d+$/.test(text) ? Number(text) <= 3 : ERROR_WORDS.has(text);
}
const SYSLOG = [
    "EMERG",
    "ALERT",
    "CRIT",
    "ERROR",
    "WARN",
    "NOTICE",
    "INFO",
    "DEBUG",
];
// a level in words: the number a syslog source sends, or the word as given
export function levelWord(level: unknown): string {
    const text = String(level ?? "").trim();
    return /^\d+$/.test(text)
        ? (SYSLOG[Number(text)] ?? text)
        : text.toUpperCase();
}
export interface ExcerptRow {
    offset: string;
    service: string;
    level: string;
    message: string;
    error: boolean;
    first: boolean; // the first observed error of the request
}
const offsetOf = (x: Fields, base: number | null): string => {
    const t = typeof x.t === "string" ? x.t : "";
    if (t) return t;
    const at = typeof x.ts === "string" ? Date.parse(x.ts) : NaN;
    return base !== null && !Number.isNaN(at) ? offsetText(at - base) : "";
};
// a trace's lines for the inspector, at most `max`: the error lines, earliest first; with no error the first and last
// line when the record holds the whole request, else the first two
export function traceExcerpts(
    e: Pick<InvEvidence, "excerpts" | "summary">,
    max = 3,
): ExcerptRow[] {
    const lines = e.excerpts.filter(
            (x): x is Fields => !!x && typeof x === "object",
        ),
        t0 = lines.length ? Date.parse(String(lines[0].ts ?? "")) : NaN,
        rows = lines.map((x) => ({
            offset: offsetOf(x, Number.isNaN(t0) ? null : t0),
            service: String(x.service ?? x.source ?? ""),
            level: levelWord(x.level),
            message: String(x.message ?? ""),
            error: "level" in x && isErrorLevel(x.level),
            first: false,
        }));
    const errors = rows.filter((r) => r.error);
    if (errors.length) {
        errors[0].first = true;
        return errors.slice(0, max);
    }
    const whole = rows.length === Number(e.summary.lines ?? rows.length);
    return whole && rows.length > 1
        ? [rows[0], rows[rows.length - 1]]
        : rows.slice(0, Math.min(2, max));
}
// Traces return the earliest lines; searches can use any sort order or page. Only for evidence that is lines:
// the rows of a histogram or a grouping say nothing about how many lines matched
export function cutText(
    e: Pick<InvEvidence, "kind" | "exact_count" | "returned">,
) {
    return ["search_logs", "trace_request"].includes(e.kind) &&
        e.exact_count !== null &&
        e.returned !== null &&
        e.returned < e.exact_count
        ? `Showing ${e.kind === "trace_request" ? "the earliest " : ""}${e.returned.toLocaleString("en-US")} of ${e.exact_count.toLocaleString("en-US")} lines.`
        : "";
}
// one line on what the evidence is made of; a request's chart says it in its own legend
export function coverageLine(kind: string, hasChart: boolean): string {
    if (kind === "trace_request")
        return hasChart
            ? ""
            : "Log observations · incomplete service coverage possible";
    if (kind === "search_logs") return "Log lines · a page of the matches";
    if (
        ["count_logs", "top_values", "log_histogram", "error_summary"].includes(
            kind,
        )
    )
        return "Counts computed by Graylog over the saved window";
    return "";
}

// ------------------------------------------------------------------ the series and their layout
// The series the server keeps with an evidence record (`summary.charts`). Records saved before it was kept have no
// trace series, and none is guessed from the excerpts.
export interface HistogramChart {
    type: "histogram";
    interval?: string | null;
    points: [string, number][];
    source_buckets?: number; // when grouped, each bar is the maximum of adjacent original buckets
    extent?: [string, string]; // the first and last original bucket labels
    peak?: { at: string; count: number | null };
    onset?: { at: string; count: number | null };
}
export interface TraceObservation {
    ms: number;
    level: string;
}
export interface TraceService {
    service: string;
    first_ms: number;
    last_ms: number;
    lines: number;
    errors?: number;
    observations: TraceObservation[];
}
export interface TraceChart {
    type: "trace";
    duration_ms: number | null;
    truncated: boolean;
    services: TraceService[];
    first_error?: { service: string; ms: number; message: string };
}
export type Chart = HistogramChart | TraceChart;
export function chartsOf(
    summary: Fields,
    kind = "",
    excerpts: unknown[] = [],
): { trace: TraceChart | null; histogram: HistogramChart | null } {
    const kept = Array.isArray(summary.charts)
        ? (summary.charts as Chart[])
        : [];
    const trace = kept.find(
        (c): c is TraceChart =>
            kind === "trace_request" &&
            c.type === "trace" &&
            Array.isArray((c as TraceChart).services) &&
            (c as TraceChart).services.length > 0,
    );
    // records saved before the series were kept still hold a histogram's buckets as their excerpts
    let histogram = kept.find(
        (c): c is HistogramChart =>
            kind === "log_histogram" &&
            c.type === "histogram" &&
            Array.isArray(c.points) &&
            c.points.length > 0,
    );
    if (!histogram && kind === "log_histogram") {
        const points = excerpts.filter(
            (x): x is [string, number] =>
                Array.isArray(x) &&
                x.length === 2 &&
                typeof x[0] === "string" &&
                typeof x[1] === "number",
        );
        if (points.length > 1 && points.length === excerpts.length)
            histogram = {
                type: "histogram",
                interval: summary.interval as string | undefined,
                points,
                peak: summary.peak as HistogramChart["peak"],
                onset: summary.onset as HistogramChart["onset"],
            };
    }
    return { trace: trace ?? null, histogram: histogram ?? null };
}

// ticks at 1, 2 or 5 x 10^n ms, the first step that leaves at most five intervals; the axis ends on the tick that
// holds the last observation, so it is never padded by more than one step
export function axisOf(end: number) {
    const e = Math.max(end, 1);
    let step = 1;
    search: for (let pow = 1; ; pow *= 10)
        for (const m of [1, 2, 5]) {
            step = m * pow;
            if (e / step <= 5) break search;
        }
    const n = Math.ceil(e / step),
        max = n * step,
        unit = max >= 1000 ? "s" : "ms",
        label = (v: number) =>
            unit === "s" ? `${Number((v / 1000).toFixed(2))} s` : `${v} ms`;
    return {
        max,
        ticks: Array.from({ length: n + 1 }, (_, i) => ({
            ms: i * step,
            x: (i * step) / max,
            label: label(i * step),
        })),
    };
}
export const MAX_TRACE_ROWS = 8;
export const MAX_ERROR_DOTS = 3;
export const NAME_CHARS = 14;
export const ROW_H = 44;
export const BUBBLE_H = 32;
export const BUBBLE_TEXT = "First observed error";
const CHAR_PX = 6;
export interface TraceDot {
    ms: number;
    x: number; // 0..1 along the axis
    error: boolean;
    count: number; // error lines this dot stands for when there are more than three
    label: { text: string; side: "below" | "above" } | null;
}
export interface TraceRow {
    service: string;
    name: string;
    first: number; // x of the first and last observation: the dotted line between them
    last: number;
    top: number; // y of the row, in the units of ROW_H
    height: number;
    dots: TraceDot[];
    bubble: number | null; // x of the dot the "First observed error" bubble points at
}
function dotsOf(s: TraceService): TraceDot[] {
    const errors = s.observations.filter((o) => o.level === "error");
    const plain = s.observations.filter((o) => o.level !== "error");
    const many =
        (s.errors ?? errors.length) > MAX_ERROR_DOTS && errors.length > 0;
    const marks = [
        ...plain.map((o) => ({ ms: o.ms, error: false, count: 1 })),
        ...(many
            ? [
                  {
                      ms: errors[0].ms,
                      error: true,
                      count: s.errors ?? errors.length,
                  },
              ]
            : errors.map((o) => ({ ms: o.ms, error: true, count: 1 }))),
    ].sort((a, b) => a.ms - b.ms);
    // two lines at one offset are one dot, and an error shows through
    const out: typeof marks = [];
    for (const m of marks) {
        const last = out[out.length - 1];
        if (last && last.ms === m.ms) {
            last.error = last.error || m.error;
            last.count = Math.max(last.count, m.count);
        } else out.push({ ...m });
    }
    return out.map((m) => ({ ...m, x: 0, label: null }));
}
// the offset under a dot; where two would touch the later one goes above its dot, and where that touches too only
// the first and last offsets of the row are shown
export function placeLabels(dots: TraceDot[], plot: number, quiet: boolean) {
    for (const d of dots) d.label = null;
    if (quiet || !dots.length) return;
    const wide = (d: TraceDot) =>
            (msText(d.ms).length +
                (d.count > 1 ? String(d.count).length + 2 : 0)) *
                CHAR_PX +
            4,
        span = (d: TraceDot) => {
            const w = wide(d),
                a = Math.min(
                    Math.max(d.x * plot - w / 2, 0),
                    Math.max(0, plot - w),
                );
            return [a, a + w] as const;
        },
        text = (d: TraceDot) =>
            msText(d.ms) + (d.count > 1 ? ` ×${d.count}` : "");
    const run = (list: TraceDot[]) => {
        const used = {
            below: [] as (readonly [number, number])[],
            above: [] as (readonly [number, number])[],
        };
        const clear = (side: "below" | "above", a: number, b: number) =>
            used[side].every(([x, y]) => b + 4 <= x || a >= y + 4);
        const put: [TraceDot, "below" | "above"][] = [];
        for (const d of list) {
            const [a, b] = span(d),
                side = clear("below", a, b)
                    ? "below"
                    : clear("above", a, b)
                      ? "above"
                      : null;
            if (!side) return null;
            used[side].push([a, b]);
            put.push([d, side]);
        }
        return put;
    };
    const all =
        run(dots) ??
        run(dots.length > 1 ? [dots[0], dots[dots.length - 1]] : dots);
    for (const [d, side] of all ?? [[dots[0], "below" as const]])
        d.label = { text: text(d), side };
}
export function traceView(c: TraceChart, plot = 380) {
    const axis = axisOf(
            Math.max(
                0,
                ...c.services.flatMap((s) => [
                    s.last_ms,
                    ...s.observations.map((o) => o.ms),
                ]),
            ),
        ),
        // with more than eight services, the ones with errors first, then the earliest; drawn in order of first line
        chosen = [...c.services]
            .sort(
                (a, b) =>
                    Number(!(a.errors ?? 0)) - Number(!(b.errors ?? 0)) ||
                    a.first_ms - b.first_ms,
            )
            .slice(0, MAX_TRACE_ROWS)
            .sort((a, b) => a.first_ms - b.first_ms),
        flat = c.services.every(
            (s) =>
                s.first_ms === c.services[0].first_ms &&
                s.last_ms === c.services[0].first_ms,
        ),
        err = c.first_error;
    let top = 0;
    const rows: TraceRow[] = chosen.map((s) => {
        const dots = dotsOf(s);
        for (const d of dots) d.x = d.ms / axis.max;
        placeLabels(dots, plot, flat);
        const bubble =
            err && err.service === s.service
                ? (dots.find((d) => d.error && d.ms === err.ms)?.x ?? null)
                : null;
        const height = ROW_H + (bubble !== null ? BUBBLE_H : 0),
            row = {
                service: s.service,
                name:
                    s.service.length > NAME_CHARS
                        ? `${s.service.slice(0, NAME_CHARS - 1)}…`
                        : s.service,
                first: s.first_ms / axis.max,
                last: s.last_ms / axis.max,
                top,
                height,
                dots,
                bubble,
            };
        top += height;
        return row;
    });
    const end = Math.max(
        ...c.services.map((s) => s.last_ms),
        c.duration_ms ?? 0,
    );
    return {
        axis,
        rows,
        height: top,
        hidden: c.services.length - chosen.length,
        single: c.services.length === 1,
        label: `${c.services.length} ${c.services.length === 1 ? "service" : "services"} over ${msText(end)}${err ? `; first error in ${err.service} at ${msText(err.ms)}` : ""}`,
        table: c.services.flatMap((s) =>
            s.observations.map((o) => ({
                service: s.service,
                offset: msText(o.ms),
                level: o.level,
            })),
        ),
    };
}
// where the bubble sits across the plot: centred on its dot, nudged left or right to stay inside
export function bubbleAt(dotX: number, plot: number, width: number) {
    return Math.min(
        Math.max(dotX * plot - width / 2, 0),
        Math.max(0, plot - width),
    );
}
