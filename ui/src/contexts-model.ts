// Snapshots are built from the answer on screen, never from an unexecuted search draft.
import type {
    Answers,
    ExplorerMessage,
    ExplorerView,
    TraceAnswer,
} from "./explorer";

export interface ContextScope {
    instance: string;
    query: string;
    streams: string[];
    from_time: string;
    to_time: string;
    timezone: string;
    ref?: string;
    trace_id?: string;
    trace_field?: string;
}
export interface ContextItem {
    id?: string;
    kind: "search" | "log" | "trace";
    title: string;
    scope: ContextScope;
    captured_at: string;
    data: Record<string, unknown>;
    limits: string[];
}
export interface ContextDraft {
    title: string;
    question: string;
    items: ContextItem[];
}
export interface SavedContext extends ContextDraft {
    id: string;
    created: string;
    prompt: string;
    provenance: string;
}
export interface ContextSummary {
    id: string;
    title: string;
    question: string;
    created: string;
    item_count: number;
}
type Answer = Answers[keyof Answers];

export function scopeOf(d: Answer): ContextScope {
    return {
        instance: d.instance,
        query: d.query || "*",
        streams: d.streams?.length ? [...d.streams] : ["*"],
        from_time: new Date(d.range.from.replace(" ", "T")).toISOString(),
        to_time: new Date(d.range.to.replace(" ", "T")).toISOString(),
        timezone: d.timezone || "UTC",
    };
}

// Keep each selection small enough to combine several. The limit is visible in the saved evidence.
export function boundedData(data: Record<string, unknown>) {
    let shortened = false;
    const bound = (value: unknown, depth = 0): unknown => {
        if (depth > 8) {
            shortened = true;
            return "[Nested value omitted]";
        }
        if (typeof value === "string" && value.length > 4000) {
            shortened = true;
            return value.slice(0, 4000) + "…";
        }
        if (Array.isArray(value)) {
            if (value.length > 20) shortened = true;
            return value.slice(0, 20).map((v) => bound(v, depth + 1));
        }
        if (value && typeof value === "object") {
            const entries = Object.entries(value);
            if (entries.length > 60) shortened = true;
            return Object.fromEntries(
                entries.slice(0, 60).map(([k, v]) => [k, bound(v, depth + 1)]),
            );
        }
        return value;
    };
    const result = bound(data) as Record<string, unknown>;
    if (new TextEncoder().encode(JSON.stringify(result)).length > 16000) {
        shortened = true;
        // Keep the beginning as a labelled excerpt; refs and scope live outside this data.
        return {
            data: { excerpt: JSON.stringify(result).slice(0, 3500) },
            shortened,
        };
    }
    return { data: result, shortened };
}

function item(
    kind: ContextItem["kind"],
    title: string,
    scope: ContextScope,
    data: Record<string, unknown>,
    limits: string[] = [],
): ContextItem {
    const bounded = boundedData(data);
    return {
        kind,
        title: title.trim().slice(0, 160) || "Selected evidence",
        scope: structuredClone(scope),
        captured_at: new Date().toISOString(),
        data: bounded.data,
        limits: [
            ...limits,
            ...(bounded.shortened
                ? [
                      "Evidence excerpt shortened; use the saved scope and refs to fetch more.",
                  ]
                : []),
        ],
    };
}

export function searchItem(d: Answer, v: ExplorerView): ContextItem {
    const limits = [
        "Snapshot of the displayed answer, not every log matching this search.",
    ];
    if ("messages" in d)
        limits.push(
            `${d.messages.length} messages displayed of ${d.total} matching messages.`,
        );
    if ("traces" in d)
        limits.push(
            `${d.traces.length} recent traces displayed; older traces may be absent.`,
        );
    return item(
        "search",
        `Search: ${d.query || "every log"}`,
        scopeOf(d),
        {
            view: v.tab,
            ...("offset" in d ? { offset: d.offset } : {}),
            ...d,
        },
        limits,
    );
}

export function logItem(
    m: ExplorerMessage,
    scope: ContextScope,
    limits: string[] = [],
): ContextItem {
    return item(
        "log",
        String(m.message || m.ref || "Selected log").split("\n")[0],
        {
            ...scope,
            ...(m.ref ? { ref: m.ref } : {}),
        },
        { message: m },
        limits,
    );
}

export function traceItem(d: TraceAnswer): ContextItem {
    return item(
        "trace",
        `Trace ${d.trace}`,
        {
            ...scopeOf(d),
            trace_id: d.trace,
            ...(d.fields.trace ? { trace_field: d.fields.trace } : {}),
        },
        { ...d },
        [
            `${d.shown} of ${d.total} trace messages displayed.`,
            "Trace coverage is limited to this query and window.",
        ],
    );
}

export function selectionKey(i: ContextItem) {
    // Different scopes stay distinct, and a refreshed search can be captured again.
    return JSON.stringify([i.kind, i.scope, i.data]);
}
