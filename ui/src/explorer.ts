// The Logs screen's view and the answers of /api/explorer/*. No DOM and no store here: the address round trip and
// the agent's recent searches are plain functions of their input.
import { pathOf, type Route } from "./router";
import type { Call, Fields } from "./scripts/types";

export const LOG_TABS = ["dashboard", "traces", "all"] as const;
export type LogTab = (typeof LOG_TABS)[number];
export const LOG_RANGES = ["15m", "1h", "4h", "24h", "7d"];
export const LOG_EVERY = [0, 30, 60, 300]; // seconds; never more often than every 30 s
export const LOG_PAGE = 100;
export const LOG_TRACES = 50; // the traces the explorer lists by default
export const LOG_WINDOW = 10000; // offset + limit the explorer serves (OpenSearch's max_result_window)

// range is "" exactly when from and to are both set
export interface ExplorerView {
    tab: LogTab;
    instance: string | null;
    q: string;
    streams: string;
    range: string;
    from: string;
    to: string;
    trace: string | null;
    offset: number;
}
export const DEFAULT_VIEW: ExplorerView = {
    tab: "dashboard",
    instance: null,
    q: "",
    streams: "",
    range: "1h",
    from: "",
    to: "",
    trace: null,
    offset: 0,
};
// a favourite or a recent search of the agent
export interface SavedView {
    name: string;
    instance: string | null;
    q: string;
    streams: string;
    range: string;
    from: string;
    to: string;
    tab: LogTab | null;
    trace?: string | null;
}

export interface ExplorerFields {
    service: string | null;
    endpoint: string | null;
    status: string | null;
    latency: string | null;
    trace: string | null;
}
interface Header {
    instance: string;
    range: { from: string; to: string };
    query: string;
    fields: ExplorerFields;
}
export interface AppRow {
    app: string;
    count: number;
    business_errors: number;
    technical_errors: number;
}
export interface EndpointRow {
    endpoint: string;
    count: number;
    business_errors: number | null;
    technical_errors: number;
    error_pct: number | null;
    last_ts: number | null;
    latency_ms?: Record<"p50" | "p95" | "p99", number | null>;
    slow?: Record<string, number>;
}
export interface DashboardAnswer extends Header {
    total: number;
    apps: AppRow[] | null;
    status: Record<string, number> | null;
    histogram: {
        interval_s: number;
        buckets: { t: number; count: number; errors: number }[];
    };
    endpoints: {
        rows: EndpointRow[];
        more: boolean;
        covered?: number;
        without_field: number;
    } | null;
    slow_ms: number[];
    error_queries: { business: string | null; technical: string | null };
}
export type ExplorerMessage = Fields & {
    ts?: string;
    ref?: string;
    level?: unknown;
    source?: string;
    message?: string;
    full_message?: string;
};
export interface MessagesAnswer extends Header {
    total: number;
    offset: number;
    limit: number;
    messages: ExplorerMessage[];
}
export interface TraceRow {
    trace: string;
    count: number;
    first_ts: number | null;
    last_ts: number | null;
    duration_ms: number | null;
    worst_level: number | null;
    services: string[];
    first_endpoint: string | null;
}
export interface TracesAnswer extends Header {
    traces: TraceRow[];
    hint?: string;
}
export interface TraceAnswer extends Header {
    trace: string;
    total: number;
    shown: number;
    duration_ms: number | null;
    messages: (ExplorerMessage & {
        offset_ms: number | null;
        service: string | null;
    })[];
}
export interface Answers {
    dashboard: DashboardAnswer;
    traces: TracesAnswer;
    trace: TraceAnswer;
    all: MessagesAnswer;
}
export type Slot = keyof Answers;

// ------------------------------------------------------------------ the address: /logs/<tab>?instance=&q=&streams=&range= | from=&to=[&trace=][&offset=]
// what every explorer request and the address share
export function explorerQuery(v: ExplorerView): URLSearchParams {
    const q = new URLSearchParams();
    if (v.instance) q.set("instance", v.instance);
    if (v.q) q.set("q", v.q);
    if (v.streams) q.set("streams", v.streams);
    if (v.from && v.to) {
        q.set("from", v.from);
        q.set("to", v.to);
    } else q.set("range", v.range || "1h");
    return q;
}
export function explorerPath(v: ExplorerView): string {
    const q = explorerQuery(v);
    if (v.tab === "traces" && v.trace) q.set("trace", v.trace);
    if (v.tab === "all" && v.offset) q.set("offset", String(v.offset));
    return pathOf("logs", [v.tab], q);
}
// a trace belongs to the traces tab and an offset to the all tab, so a view and its address always agree
export function normalView(v: ExplorerView): ExplorerView {
    const custom = !!(v.from && v.to);
    return {
        ...v,
        from: custom ? v.from : "",
        to: custom ? v.to : "",
        range: custom ? "" : v.range || "1h",
        trace: v.tab === "traces" ? v.trace || null : null,
        offset: v.tab === "all" ? Math.max(0, v.offset) : 0,
    };
}
export function viewOfRoute(r: Route): ExplorerView {
    const q = r.query,
        tab = r.sub[0] || "dashboard";
    return normalView({
        tab: (LOG_TABS as readonly string[]).includes(tab)
            ? (tab as LogTab)
            : "dashboard",
        instance: q.get("instance") || null,
        q: q.get("q") || "",
        streams: q.get("streams") || "",
        range: q.get("range") || "1h",
        from: q.get("from") || "",
        to: q.get("to") || "",
        trace: q.get("trace"),
        offset: parseInt(q.get("offset") || "0", 10) || 0,
    });
}
export function slotOf(v: ExplorerView): Slot {
    return v.tab === "traces" ? (v.trace ? "trace" : "traces") : v.tab;
}
// the request of a view, without refresh=1: its text is also the key an answer is kept under
export function requestOf(v: ExplorerView): { slot: Slot; url: string } {
    const slot = slotOf(v),
        q = explorerQuery(v);
    if (slot === "all") {
        q.set("offset", String(v.offset));
        q.set("limit", String(LOG_PAGE));
    }
    const path = {
        dashboard: "dashboard",
        traces: "traces",
        trace: `trace/${encodeURIComponent(v.trace || "")}`,
        all: "messages",
    }[slot];
    return { slot, url: `/api/explorer/${path}?${q}` };
}
// narrow the query: an extra clause ANDed to it, for a click on a status, an API or an application
export const drillQuery = (q: string, clause: string) =>
    q && q !== "*" ? `(${q}) AND ${clause}` : clause;
export const fieldOf = (f: string) =>
    f.replace(/([+\-!(){}[\]^"~*?:\\/ ]|&&|\|\|)/g, "\\$1");

// ------------------------------------------------------------------ time
// the range the answer covered, as instants (the server writes it in the instance's zone), else the view's own
export function windowOf(
    answer: { range?: { from: string; to: string } } | null | undefined,
    v: ExplorerView,
): [Date, Date] | null {
    const parse = (s: string) => new Date(String(s).replace(" ", "T"));
    if (answer?.range) {
        const f = parse(answer.range.from),
            t = parse(answer.range.to);
        if (!isNaN(+f) && !isNaN(+t)) return [f, t];
    }
    if (v.from && v.to) return [new Date(v.from), new Date(v.to)];
    return null;
}
// <input type="datetime-local"> holds local time without a zone
export function localInput(iso: string | Date): string {
    const d = new Date(iso);
    if (isNaN(+d)) return "";
    const p = (n: number) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}
export function graylogSearchUrl(
    url: string | undefined,
    q: string,
    w: [Date, Date] | null,
): string {
    if (!w || !url) return "";
    const base = url.replace(/\/+$/, "").replace(/\/api$/, "");
    const p = new URLSearchParams({
        q,
        rangetype: "absolute",
        from: w[0].toISOString(),
        to: w[1].toISOString(),
    });
    return `${base}/search?${p}`;
}
export const fmtSpan = (s: number) =>
    s < 60
        ? `${+s.toFixed(1)} s`
        : s < 3600
          ? `${+(s / 60).toFixed(1)} min`
          : s < 86400
            ? `${+(s / 3600).toFixed(1)} h`
            : `${+(s / 86400).toFixed(1)} d`;
export const fmtLimit = (ms: number) =>
    ms >= 1000 ? fmtSpan(ms / 1000) : `${ms} ms`;
export const fmtMs = (ms: number) =>
    ms < 1000 ? `${Math.round(ms)} ms` : fmtSpan(ms / 1000);
export const firstLine = (s: unknown) => String(s ?? "").split("\n", 1)[0];

// ------------------------------------------------------------------ favourites and the agent's recent searches
export const viewKey = (v: Omit<SavedView, "name" | "tab">) =>
    JSON.stringify([
        v.instance || "",
        v.q || "",
        v.streams || "",
        v.range || "",
        v.from || "",
        v.to || "",
    ]);
export function savedOf(v: ExplorerView): SavedView {
    const name = `${v.q || "every log"} · ${v.range || "fixed range"}${v.instance ? ` · ${v.instance}` : ""}`;
    return {
        name: name.slice(0, 80),
        instance: v.instance,
        q: v.q,
        streams: v.streams,
        range: v.range,
        from: v.from,
        to: v.to,
        tab: v.tab,
    };
}
// a favourite comes from this browser's storage: anything that is not a view is dropped
export function parseSaved(text: string | null): SavedView[] {
    let list: unknown;
    try {
        list = JSON.parse(text || "[]");
    } catch {
        return [];
    }
    if (!Array.isArray(list)) return [];
    const str = (x: unknown) => (typeof x === "string" ? x : "");
    return list
        .filter((f): f is Fields => !!f && typeof f === "object")
        .map((f) => ({
            name: str(f.name) || "view",
            instance: str(f.instance) || null,
            q: str(f.q),
            streams: str(f.streams),
            range: str(f.range),
            from: str(f.from),
            to: str(f.to),
            tab: (LOG_TABS as readonly string[]).includes(f.tab)
                ? (f.tab as LogTab)
                : null,
            trace: str(f.trace) || null,
        }));
}
export function viewOfSaved(s: SavedView, current: ExplorerView): ExplorerView {
    return normalView({
        tab: s.tab || current.tab,
        instance: s.instance || current.instance,
        q: s.q,
        streams: s.streams,
        range: s.range,
        from: s.from,
        to: s.to,
        trace: s.trace || null,
        offset: 0,
    });
}
const AGENT_TOOLS = [
    "search_logs",
    "aggregate",
    "log_histogram",
    "error_summary",
    "compare_periods",
    "root_cause",
    "trace_request",
    "scan",
];
export function recentViews(calls: Call[]): SavedView[] {
    const seen = new Set<string>(),
        out: SavedView[] = [];
    for (const c of calls) {
        const a = c.args;
        if (!AGENT_TOOLS.includes(c.tool) || !a || !(a.query || a.trace_id))
            continue;
        const custom = !!(a.from_time && a.to_time),
            trace = a.trace_id ? String(a.trace_id) : null,
            q = a.query && a.query !== "*" ? String(a.query) : "",
            v: SavedView = {
                name: `${c.tool}: ${trace || q}`.slice(0, 80),
                instance: c.instance || a.instance || null,
                q,
                streams: Array.isArray(a.streams) ? a.streams.join(",") : "",
                range: custom ? "" : a.range || "1h",
                from: custom ? String(a.from_time) : "",
                to: custom ? String(a.to_time) : "",
                tab: trace ? "traces" : "all",
                trace,
            };
        const k = viewKey(v) + (trace || "");
        if (seen.has(k)) continue;
        seen.add(k);
        out.push(v);
        if (out.length === 12) break;
    }
    return out;
}
