import { formatDateTime, formatTime, parseTime, viewZone } from "../time";
import { useEffect, useLayoutEffect, useRef, useState } from "preact/hooks";
import { api, errorText } from "../api";
import { instances, label, navigate, playground, toast } from "../store";
import {
    type LogsView,
    levelOf,
    logsBase,
    logsRevision,
    logsView,
    openLogs,
    phrase,
} from "../logs";
import { openInLogs } from "../explorer-store";
import { messageMarkdown } from "../markdown";
import type { Fields, LogLine } from "../scripts/types";
import { revealRows } from "../scripts/motion";
import { AsyncButton, Close, Dialog, Icon, Segment, num } from "./UI";
import { contextOpen, contextDraft, openContext } from "../contexts";
import type { ContextScope } from "../contexts-model";
import { AddLogToContext } from "./ContextActions";
interface SearchResult {
    range?: { from: string; to: string };
    query?: string;
    messages?: LogLine[];
    next_offset?: number | null;
    total?: number;
    hint?: string;
    suggestions?: { query: string; count?: number }[];
    rewritten?: { why?: string[]; ran: string };
}
function searchArgs(v: LogsView, narrow: string, offset = 0, scoped = true) {
    const parts = [
            scoped && v.query !== "*" ? v.query : "",
            v.errorsOnly && v.errorQuery ? v.errorQuery : "",
            narrow.trim(),
        ].filter(Boolean),
        query =
            parts.length > 1
                ? parts.map((p) => `(${p})`).join(" AND ")
                : parts[0] || "*";
    return {
        query,
        ...(v.from
            ? { from_time: v.from, to_time: v.to }
            : { range: v.range || "24h" }),
        limit: 50,
        offset,
        dedup_lines: false,
        streams: ["*"],
        fields: [
            "timestamp",
            "level",
            "source",
            "message",
            ...(v.column && v.column !== "source" ? [v.column] : []),
        ],
    };
}
// what the Markdown says above its table of fields
const MD_SKIP = [
    "ref",
    "streams",
    "message",
    "full_message",
    "timestamp",
    "ts",
    "source",
    "level",
];
function LogRow({
    m,
    index,
    view,
    onSearch,
    scope,
}: {
    m: LogLine;
    index: number;
    view: LogsView;
    onSearch: (term: string) => void;
    scope: ContextScope | null;
}) {
    const [open, setOpen] = useState(false),
        [detail, setDetail] = useState<Fields | null>(null),
        [cutFields, setCutFields] = useState<string[]>([]),
        [error, setError] = useState("");
    const lv = levelOf(m.level),
        who = String((view.column && m[view.column]) || m.source || "");
    useEffect(() => {
        let live = true;
        if (open && !detail)
            void api<{
                result: {
                    message: Fields;
                    cut_fields?: Record<string, number>;
                };
            }>("/api/run", "POST", {
                tool: "get_message",
                instance: view.instance,
                args: { ref: m.ref },
            })
                .then((r) => {
                    if (live) {
                        setDetail(r.result.message);
                        setCutFields(Object.keys(r.result.cut_fields || {}));
                    }
                })
                .catch((e) => {
                    if (live) setError(errorText(e));
                });
        return () => {
            live = false;
        };
    }, [open]);
    const trace =
        detail &&
        Object.keys(detail).find(
            (k) =>
                /^(trace_?id|traceId|correlation_?id|request_?id|x_request_id)$/.test(
                    k,
                ) && detail[k],
        );
    return (
        <div class={`lrow${open ? " open" : ""}`}>
            <button
                class="lhead"
                aria-expanded={open}
                aria-controls={`ld-${index}`}
                onClick={() => {
                    setError("");
                    setOpen(!open);
                }}
            >
                <time
                    class="lt mono"
                    dateTime={m.ts.replace(" ", "T")}
                    title={formatTime(m.ts, "YYYY-MM-DD HH:mm:ss.SSS [UTC]Z")}
                >
                    {formatTime(
                        m.ts,
                        view.range === "7d" || view.from
                            ? "MM-DD · HH:mm:ss"
                            : "HH:mm:ss",
                    )}
                </time>
                <span class={`lv ${lv.cls}`}>{lv.word}</span>
                <span class="lsvc truncate" title={who}>
                    {who}
                </span>
                <span class="lmsg">{m.message || ""}</span>
            </button>
            <div class="ldetail" id={`ld-${index}`} hidden={!open}>
                {open &&
                    (error ? (
                        <div class="inline-msg">{error}</div>
                    ) : !detail ? (
                        <div class="skeleton lskel" />
                    ) : (
                        <>
                            <pre class="lfull">
                                {String(
                                    detail.full_message || detail.message || "",
                                )}
                            </pre>
                            <dl class="lfields">
                                {Object.entries(detail)
                                    .filter(
                                        ([k]) =>
                                            ![
                                                "ref",
                                                "streams",
                                                "message",
                                                "full_message",
                                            ].includes(k),
                                    )
                                    .map(([k, v]) => (
                                        <>
                                            <dt class="mono">{k}</dt>
                                            <dd class="mono">
                                                {typeof v === "object" ||
                                                v == null ||
                                                v === "" ? (
                                                    typeof v === "object" ? (
                                                        JSON.stringify(v)
                                                    ) : (
                                                        String(v ?? "")
                                                    )
                                                ) : (
                                                    <button
                                                        class="lval"
                                                        title={`Search ${k}:${phrase(v)}`}
                                                        onClick={() =>
                                                            onSearch(
                                                                `${k}:${phrase(v)}`,
                                                            )
                                                        }
                                                    >
                                                        {String(v)}
                                                    </button>
                                                )}
                                            </dd>
                                        </>
                                    ))}
                            </dl>
                            <div class="lacts">
                                {scope && (
                                    <AddLogToContext
                                        m={{ ...m, ...detail }}
                                        scope={scope}
                                        loaded
                                        cutFields={cutFields}
                                    />
                                )}
                                {trace && (
                                    <button
                                        class="btn sm"
                                        onClick={() =>
                                            openLogs({
                                                instance: view.instance,
                                                column: view.column,
                                                title: `Trace ${detail[trace]}`,
                                                query: `${trace}:${phrase(detail[trace])}`,
                                                range: "7d",
                                            })
                                        }
                                    >
                                        Follow {trace}
                                    </button>
                                )}
                                <button
                                    class="btn sm"
                                    onClick={() => {
                                        const at = parseTime(m.ts)?.valueOf();
                                        if (at == null) return;
                                        openLogs({
                                            instance: view.instance,
                                            column: view.column,
                                            title: `Around ${formatTime(m.ts)}`,
                                            when: `${formatDateTime(m.ts)} ± 30 s`,
                                            query: "*",
                                            from: new Date(
                                                at - 30000,
                                            ).toISOString(),
                                            to: new Date(
                                                at + 30000,
                                            ).toISOString(),
                                        });
                                    }}
                                >
                                    Lines around it (±30 s)
                                </button>
                                <AsyncButton
                                    class="btn sm ghost"
                                    run={async () => {
                                        await navigator.clipboard.writeText(
                                            m.ref,
                                        );
                                        toast("Copied");
                                    }}
                                >
                                    Copy ref
                                </AsyncButton>
                                <AsyncButton
                                    class="btn sm ghost"
                                    title="Copy the message and its fields for a ticket"
                                    run={async () => {
                                        await navigator.clipboard.writeText(
                                            messageMarkdown({
                                                instance: view.instance,
                                                ts: m.ts,
                                                source: detail.source,
                                                service:
                                                    view.column &&
                                                    view.column !== "source" &&
                                                    detail[view.column] != null
                                                        ? {
                                                              field: view.column,
                                                              value: detail[
                                                                  view.column
                                                              ],
                                                          }
                                                        : null,
                                                level: levelOf(detail.level)
                                                    .word,
                                                message:
                                                    detail.full_message ||
                                                    detail.message ||
                                                    "",
                                                fields: Object.fromEntries(
                                                    Object.entries(
                                                        detail,
                                                    ).filter(
                                                        ([k]) =>
                                                            !MD_SKIP.includes(
                                                                k,
                                                            ),
                                                    ),
                                                ),
                                            }),
                                        );
                                        toast("Copied as Markdown");
                                    }}
                                >
                                    Copy as Markdown
                                </AsyncButton>
                            </div>
                        </>
                    ))}
            </div>
        </div>
    );
}
export function LogsDrawer() {
    const view = logsView.value,
        rev = logsRevision.value,
        [query, setQuery] = useState(""),
        [narrow, setNarrow] = useState(""),
        [scoped, setScoped] = useState(true),
        [lines, setLines] = useState<LogLine[]>([]),
        [result, setResult] = useState<SearchResult | null>(null),
        [captureScope, setCaptureScope] = useState<ContextScope | null>(null),
        [error, setError] = useState(""),
        [loading, setLoading] = useState(false),
        list = useRef<HTMLDivElement>(null),
        seq = useRef(0);
    useEffect(() => {
        setQuery("");
        setNarrow("");
        setScoped(true);
    }, [rev]);
    useEffect(() => {
        const t = setTimeout(() => setNarrow(query), 450);
        return () => clearTimeout(t);
    }, [query]);
    const load = async (more = false) => {
        if (!view) return;
        const id = ++seq.current;
        setLoading(true);
        setError("");
        if (!more) {
            setLines([]);
            setResult(null);
            setCaptureScope(null);
            if (list.current) list.current.scrollTop = 0;
        }
        try {
            const r = (
                await api<{ result: SearchResult }>("/api/run", "POST", {
                    tool: "search_logs",
                    instance: view.instance,
                    args: {
                        ...searchArgs(
                            view,
                            narrow,
                            more ? result?.next_offset || 0 : 0,
                            scoped,
                        ),
                        ...(more && captureScope
                            ? {
                                  range: null,
                                  from_time: captureScope.from_time,
                                  to_time: captureScope.to_time,
                              }
                            : {}),
                    },
                })
            ).result;
            if (id !== seq.current) return;
            if (!more && r.range) {
                setCaptureScope({
                    instance: view.instance,
                    query:
                        r.rewritten?.ran ||
                        r.query ||
                        searchArgs(view, narrow, 0, scoped).query,
                    streams: ["*"],
                    from_time: new Date(
                        r.range.from.replace(" ", "T"),
                    ).toISOString(),
                    to_time: new Date(
                        r.range.to.replace(" ", "T"),
                    ).toISOString(),
                    timezone:
                        r.range.from.match(/(Z|[+-]\d{2}:\d{2})$/)?.[1] ||
                        "UTC",
                });
            }
            setLines((prev) =>
                more ? [...prev, ...(r.messages || [])] : r.messages || [],
            );
            setResult(r);
        } catch (e) {
            if (id !== seq.current) return;
            if (more) toast(errorText(e), "fail");
            else setError(errorText(e));
        } finally {
            if (id === seq.current) setLoading(false);
        }
    };
    useEffect(() => {
        if (view) void load();
        else seq.current++;
        return () => {
            seq.current++;
        };
    }, [view, narrow, rev, scoped]);
    useLayoutEffect(() => {
        if (list.current && lines.length)
            revealRows([...list.current.querySelectorAll(".lrow")].slice(-50));
    }, [lines]);
    const close = () => {
            logsView.value = null;
        },
        when = view
            ? view.when ||
              (view.from
                  ? `${formatDateTime(view.from)} to ${formatDateTime(view.to)}`
                  : `last ${view.range === "7d" ? "7 days" : "24 hours"}`)
            : "";
    const effective = view ? searchArgs(view, narrow, 0, scoped).query : "",
        fix = result?.suggestions?.[0];
    return (
        <Dialog
            id="logsDlg"
            class="sheet"
            aria-labelledby="logsTitle"
            open={!!view && !contextOpen.value}
            onClose={() => {
                if (!contextOpen.peek()) close();
            }}
        >
            <div class="dhead">
                <div class="min0">
                    <h2 id="logsTitle">{view?.title || "Logs"}</h2>
                    <div class="muted small truncate" id="logsWhere">
                        {view && (
                            <>
                                {label(
                                    instances.value.find(
                                        (e) => e.name === view.instance,
                                    ) ||
                                        ({
                                            name: view.instance,
                                            group: null,
                                        } as never),
                                )}{" "}
                                · {when} · {viewZone()} ·{" "}
                                <span class="mono">{effective}</span>
                            </>
                        )}
                    </div>
                </div>
                <span class="spacer" />
                <Close onClick={close} />
            </div>
            <div class="logs-bar">
                <select
                    id="logsEnv"
                    class="logs-env"
                    aria-label="Environment"
                    hidden={instances.value.length < 2}
                    value={view?.instance || ""}
                    onChange={(e) => {
                        if (view)
                            logsView.value = {
                                ...view,
                                ...logsBase(e.currentTarget.value),
                                instance: e.currentTarget.value,
                            };
                    }}
                >
                    {instances.value.map((e) => (
                        <option value={e.name}>{label(e)}</option>
                    ))}
                </select>
                {view?.errorQuery && (
                    <Segment
                        label="Levels"
                        options={[
                            ["all", "Every level"],
                            ["err", "Errors only"],
                        ]}
                        value={view.errorsOnly ? "err" : "all"}
                        onChange={(v) => {
                            logsView.value = {
                                ...view,
                                errorsOnly: v === "err",
                            };
                        }}
                    />
                )}
                {view && view.query !== "*" && (
                    <button
                        class={`btn sm ghost logs-scope${scoped ? "" : " off"}`}
                        title={
                            scoped
                                ? "Search the whole time range, without this filter"
                                : "Put the filter back"
                        }
                        onClick={() => setScoped(!scoped)}
                    >
                        {scoped ? (
                            <>
                                <span class="mono truncate">{view.query}</span>{" "}
                                ✕
                            </>
                        ) : (
                            "Filter removed · restore"
                        )}
                    </button>
                )}
                <form
                    class="logs-q"
                    id="logsForm"
                    role="search"
                    onSubmit={(e) => {
                        e.preventDefault();
                        if (narrow === query) void load();
                        else setNarrow(query);
                    }}
                >
                    <Icon name="search" />
                    <input
                        id="logsQ"
                        type="search"
                        placeholder="Narrow down: words, or field:value"
                        aria-label="Narrow down the logs"
                        autoComplete="off"
                        spellcheck={false}
                        value={query}
                        onInput={(e) => setQuery(e.currentTarget.value)}
                    />
                </form>
            </div>
            <div class="logs-meta">
                <span id="logsMeta" aria-live="polite">
                    {loading ? (
                        "Reading Graylog…"
                    ) : error ? (
                        ""
                    ) : !lines.length ? (
                        "0 lines"
                    ) : (
                        <>
                            <b>{num(lines.length)}</b>
                            {result?.total != null
                                ? ` of ${num(result.total)}`
                                : ""}{" "}
                            lines, newest first
                            {result?.rewritten && (
                                <>
                                    {" "}
                                    ·{" "}
                                    <span
                                        class="warn-text"
                                        title={result.rewritten.why?.join("; ")}
                                    >
                                        ran{" "}
                                        <span class="mono">
                                            {result.rewritten.ran}
                                        </span>
                                    </span>
                                </>
                            )}
                        </>
                    )}
                </span>
                <span class="spacer" />
                <button class="btn sm" type="button" onClick={openContext}>
                    Review context
                    {contextDraft.value.items.length
                        ? ` (${contextDraft.value.items.length})`
                        : ""}
                </button>
                <button
                    class="btn sm ghost"
                    id="logsPg"
                    title="Run this search in the Playground"
                    onClick={async () => {
                        if (!view) return;
                        const v = view,
                            a = searchArgs(v, narrow, 0, scoped);
                        close();
                        if (await navigate("playground"))
                            playground.value = {
                                tool: "search_logs",
                                instance: v.instance,
                                args: { ...a, limit: 20, dedup_lines: true },
                            };
                    }}
                >
                    Open in Playground
                </button>
                <button
                    class="btn sm ghost"
                    id="logsExplore"
                    title="Read these lines on the Logs screen"
                    onClick={() => {
                        if (!view) return;
                        const v = view;
                        close();
                        void openInLogs({
                            instance: v.instance,
                            query: searchArgs(v, narrow, 0, scoped).query,
                            range: v.range,
                            from: v.from,
                            to: v.to,
                        });
                    }}
                >
                    Open in Logs
                </button>
            </div>
            <div class="logs-list" id="logsList" ref={list}>
                {error ? (
                    <div class="inline-msg">{error}</div>
                ) : loading && !lines.length ? (
                    Array.from({ length: 6 }, () => (
                        <div class="skeleton lskel" />
                    ))
                ) : !lines.length ? (
                    <div class="empty">
                        <h3>No log line matches</h3>
                        <div>
                            {result?.hint ||
                                "Try a wider range, or clear the filter above."}
                        </div>
                        {fix && (
                            <div class="actions">
                                <button
                                    class="btn"
                                    onClick={() => {
                                        if (view) {
                                            logsView.value = {
                                                ...view,
                                                query: fix.query,
                                                errorsOnly: false,
                                            };
                                            setQuery("");
                                            setNarrow("");
                                        }
                                    }}
                                >
                                    Try <span class="mono">{fix.query}</span>
                                    {fix.count != null
                                        ? ` · ${num(fix.count)}`
                                        : ""}
                                </button>
                            </div>
                        )}
                    </div>
                ) : (
                    <>
                        {view &&
                            lines.map((m, i) => (
                                <LogRow
                                    key={`${rev}:${m.ref}:${i}`}
                                    m={m}
                                    index={i}
                                    view={view!}
                                    scope={loading ? null : captureScope}
                                    onSearch={(term) => {
                                        const next = query.trim()
                                            ? `${query.trim()} AND ${term}`
                                            : term;
                                        setQuery(next);
                                        setNarrow(next);
                                    }}
                                />
                            ))}
                        {result?.next_offset != null && (
                            <div class="lmore">
                                <AsyncButton
                                    class="btn sm"
                                    id="logsMore"
                                    disabled={loading}
                                    run={() => load(true)}
                                >
                                    Load 50 more
                                </AsyncButton>
                            </div>
                        )}
                    </>
                )}
            </div>
        </Dialog>
    );
}
