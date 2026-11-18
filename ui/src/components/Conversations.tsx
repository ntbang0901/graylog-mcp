// Activity's Conversations tab: the turns of every conversation, then one conversation (or one turn) call by call.
import { Fragment } from "preact";
import { useEffect, useLayoutEffect, useRef, useState } from "preact/hooks";
import {
    activityTab,
    currentEnv,
    currentPage,
    playgroundRuns,
    state,
} from "../store";
import { pathOf } from "../router";
import {
    CONV_RANGES,
    STATUS_WHY,
    callKey,
    closeConversation,
    convDetail,
    convDetailError,
    convExpanded,
    convFilters,
    convList,
    convListError,
    convOpen,
    convShort,
    exportConversation,
    fmtDur,
    isMiss,
    loadConversationDetail,
    loadConversations,
    openConversation,
    scheduleRefresh,
    statusOf,
    stopRefresh,
    syncActivityAddress,
    takeReveal,
    toggleCall,
    turnMatches,
} from "../activity";
import type { Call, ConversationTurn, Json, TurnRow } from "../scripts/types";
import {
    StatusBadge,
    askText,
    fmtMs,
    plainClick,
    playCall,
} from "./ActivityParts";
import { JsonTree } from "./JsonTree";
import { Marks } from "./Marks";
import {
    AsyncButton,
    Icon,
    JsonText,
    Segment,
    Skeleton,
    num,
    plural,
    whenShort,
} from "./UI";

const toolAbbr = (t: string) =>
    t
        .split("_")
        .map((w) => w[0] || "")
        .join("")
        .slice(0, 3);
function ToolChips({ tools }: { tools: string[] }) {
    const shown = tools.slice(0, 14);
    return (
        <span class="tchips">
            {shown.map((t) => (
                <abbr class="tchip" title={t}>
                    {toolAbbr(t)}
                </abbr>
            ))}
            {tools.length > shown.length && (
                <span class="muted">+{tools.length - shown.length}</span>
            )}
        </span>
    );
}
const RANGE_TEXT: Record<string, string> = {
    "24h": "24 hours",
    "7d": "7 days",
    "30d": "30 days",
    "90d": "90 days",
};

// ------------------------------------------------------------------ the list
function TurnLine({ t }: { t: TurnRow }) {
    const cold = t.tier === "cold",
        open = () => openConversation(t.conversation, t.turn);
    return (
        <tr
            class="go"
            tabIndex={0}
            aria-label={`${convShort(t.conversation)}, ${new Date(t.last_ts * 1000).toLocaleString()}, ${plural(t.calls, "call")}`}
            onClick={open}
            onKeyDown={(e) => {
                if (e.key === "Enter" && e.target === e.currentTarget) {
                    e.preventDefault();
                    open();
                }
            }}
        >
            <td
                class="mono"
                title={new Date(t.first_ts * 1000).toLocaleString()}
            >
                {cold
                    ? new Date(t.last_ts * 1000).toLocaleDateString()
                    : whenShort(t.first_ts)}
            </td>
            <td class="num mono">
                {cold ? "-" : fmtDur(t.last_ts - t.first_ts)}
            </td>
            <td class="mono">
                <span class="cid">{convShort(t.conversation)}</span>
            </td>
            <td>{t.repo || "-"}</td>
            <td>{(t.instances || []).join(", ") || "-"}</td>
            <td>{t.client || "-"}</td>
            <td class="num mono">{t.agents ? num(t.agents) : "-"}</td>
            <td class="num mono">{num(t.calls)}</td>
            <td>
                <span class="flags">
                    {!!t.running && <StatusBadge status="running" />}
                    {!!t.errors && (
                        <span class="badge fail">
                            {num(t.errors)}{" "}
                            {t.errors === 1 ? "error" : "errors"}
                        </span>
                    )}
                    {!!t.misses && (
                        <span class="badge warn">
                            {num(t.misses)} found nothing
                        </span>
                    )}
                    {!!t.truncated && (
                        <span
                            class="badge"
                            title="Results cut to the size budget"
                        >
                            {num(t.truncated)} cut
                        </span>
                    )}
                    {cold ? (
                        <span
                            class="badge"
                            title="Only a summary of these calls is kept"
                        >
                            summary
                        </span>
                    ) : (
                        t.tier === "summary" && (
                            <span
                                class="badge"
                                title="Previews and requests are no longer kept for this turn"
                            >
                                no detail
                            </span>
                        )
                    )}
                </span>
            </td>
            <td>
                <ToolChips tools={t.tools || []} />
            </td>
        </tr>
    );
}
function ConversationList() {
    const f = convFilters.value,
        L = convList.value,
        input = useRef<HTMLInputElement>(null),
        typing = useRef<ReturnType<typeof setTimeout> | undefined>(undefined),
        [text, setText] = useState(f.q);
    // the address changed the filter (Back): the box follows unless it is being typed in
    useEffect(() => {
        if (document.activeElement !== input.current) setText(f.q);
    }, [f.q]);
    useEffect(() => () => clearTimeout(typing.current), []);
    const set = (patch: Partial<typeof f>, how: "push" | "replace") => {
        convFilters.value = { ...convFilters.value, ...patch };
        syncActivityAddress(how);
    };
    const rows = L ? L.turns.filter((t) => turnMatches(t, f.status)) : [];
    return (
        <div id="convList">
            <div class="fbar">
                <label class="search">
                    <Icon name="search" />
                    <input
                        id="cq"
                        ref={input}
                        type="search"
                        placeholder="Filter by repository, tool, query…"
                        autoComplete="off"
                        aria-label="Filter conversations"
                        value={text}
                        onInput={(e) => {
                            const v = e.currentTarget.value;
                            setText(v);
                            clearTimeout(typing.current);
                            typing.current = setTimeout(
                                () => set({ q: v.trim() }, "replace"),
                                300,
                            );
                        }}
                    />
                </label>
                <select
                    class="sel auto-width"
                    id="crange"
                    aria-label="Conversations from"
                    value={f.range}
                    onChange={(e) =>
                        set({ range: e.currentTarget.value }, "push")
                    }
                >
                    {CONV_RANGES.map((r) => (
                        <option value={r}>{RANGE_TEXT[r]}</option>
                    ))}
                </select>
                <Segment
                    id="cstat"
                    label="Outcome"
                    options={[
                        ["all", "All"],
                        ["error", "Errors"],
                        ["nothing", "Found nothing"],
                        ["running", "Running"],
                    ]}
                    value={f.status}
                    onChange={(status) => set({ status }, "push")}
                />
            </div>
            <div class="cmeta">
                <span id="ccount" aria-live="polite">
                    {L &&
                        `Showing ${num(rows.length)} of ${num(L.total)} turns${L.total > L.turns.length ? ` · the latest ${L.turns.length} are listed` : ""}`}
                </span>
                <span class="spacer" />
                <span class="keyhint">
                    <span class="kbd">J</span> <span class="kbd">K</span> move ·{" "}
                    <span class="kbd">Enter</span> open
                </span>
            </div>
            <div id="clist">
                {!L ? (
                    convListError.value ? (
                        <div class="inline-msg">
                            Could not load the conversations:{" "}
                            {convListError.value}.
                        </div>
                    ) : (
                        <Skeleton />
                    )
                ) : !rows.length ? (
                    <div class="allclear">
                        {!L.enabled
                            ? "// recording is off"
                            : L.turns.length
                              ? "// no turn matches these filters"
                              : "// no conversation in this range yet"}
                    </div>
                ) : (
                    <div class="table-wrap">
                        <table class="convtable">
                            <thead>
                                <tr>
                                    <th>When</th>
                                    <th class="num">Took</th>
                                    <th>Conversation</th>
                                    <th>Repository</th>
                                    <th>Environment</th>
                                    <th>Client</th>
                                    <th class="num">Agents</th>
                                    <th class="num">Calls</th>
                                    <th>Outcome</th>
                                    <th>Tools</th>
                                </tr>
                            </thead>
                            <tbody>
                                {rows.map((t) => (
                                    <TurnLine
                                        key={`${t.conversation}:${t.turn}`}
                                        t={t}
                                    />
                                ))}
                            </tbody>
                        </table>
                    </div>
                )}
            </div>
        </div>
    );
}

// ------------------------------------------------------------------ one conversation
function Requests({ c }: { c: Call }) {
    if (Array.isArray(c.spans) && c.spans.length) {
        const total = Math.max(
            1,
            c.ms || 0,
            ...c.spans.map((x) => (x.start_ms || 0) + (x.ms || 0)),
        );
        return (
            <div class="wfall">
                {c.spans.map((x) => {
                    const left = (100 * (x.start_ms || 0)) / total,
                        width = Math.max(0.5, (100 * (x.ms || 0)) / total),
                        wait = x.ms
                            ? Math.min(100, (100 * (x.wait_ms || 0)) / x.ms)
                            : 0,
                        ok = typeof x.status === "number" && x.status < 400;
                    return (
                        <div class="wrow">
                            <span
                                class="mono truncate"
                                title={`${x.method || ""} ${x.path || ""}${x.query ? `\n${x.query}` : ""}`}
                            >
                                {x.method || ""} {x.path || ""}
                            </span>
                            <span class="wfbar">
                                <span
                                    class={ok ? undefined : "bad"}
                                    style={{
                                        left: `${left.toFixed(2)}%`,
                                        width: `${Math.min(width, 100 - left).toFixed(2)}%`,
                                    }}
                                >
                                    <i
                                        title={`waited ${x.wait_ms || 0} ms for a free connection`}
                                        style={{ width: `${wait.toFixed(1)}%` }}
                                    />
                                </span>
                            </span>
                            <span class={`mono small ${ok ? "muted" : "bad"}`}>
                                {x.status ?? "-"}
                            </span>
                            <span class="mono small muted num">
                                {fmtMs(x.ms || 0)}
                                {(x.attempts || 0) > 1
                                    ? ` · ${x.attempts} tries`
                                    : ""}
                            </span>
                        </div>
                    );
                })}
                {!!c.spans_dropped && (
                    <div class="muted small">
                        {plural(c.spans_dropped, "more request")} not kept
                    </div>
                )}
            </div>
        );
    }
    const m = c.span_summary;
    if (m)
        return (
            <>
                <dl class="kv">
                    <dt>requests</dt>
                    <dd>
                        {num(m.n)}
                        {m.failed ? `, ${num(m.failed)} failed` : ""}
                    </dd>
                    <dt>time in requests</dt>
                    <dd>
                        {fmtMs(m.total_ms)}
                        {m.wait_ms ? ` (${fmtMs(m.wait_ms)} waiting)` : ""}
                    </dd>
                    {m.slowest && (
                        <>
                            <dt>slowest</dt>
                            <dd>
                                {m.slowest.path || ""} · {fmtMs(m.slowest.ms)}
                            </dd>
                        </>
                    )}
                </dl>
                <p class="muted small">
                    Only a summary of the requests is kept for this day.
                </p>
            </>
        );
    return (
        <p class="muted small">
            {c.requests
                ? `${plural(c.requests, "request")} sent; their detail is not kept.`
                : "No request to Graylog."}
        </p>
    );
}
function Preview({ c }: { c: Call }) {
    if (!c.preview)
        return statusOf(c) === "ok" ? (
            <p class="muted small">
                The result is not kept (only for the last days, and not with
                GRAYLOG_MCP_USAGE=calls).
            </p>
        ) : null;
    let tree: Json | undefined;
    try {
        tree = JSON.parse(c.preview) as Json;
    } catch {
        // cut at 2,000 characters, so no longer JSON: shown as text
    }
    return (
        <>
            <div class="glabel">
                what the model received
                {c.preview_truncated ? " · first 2,000 characters" : ""}
            </div>
            {tree !== undefined ? (
                <div class="tree">
                    <JsonTree value={tree} />
                </div>
            ) : (
                <pre class="json">
                    <Marks text={c.preview} />
                </pre>
            )}
        </>
    );
}
function CallDetail({ c, status }: { c: Call; status: string }) {
    const kv: [string, string][] = [
        ["started", new Date(c.ts * 1000).toLocaleString()],
        ["environment", c.instance || "-"],
        [
            "status",
            `${status}${STATUS_WHY[status] ? ` · ${STATUS_WHY[status]}` : ""}`,
        ],
    ];
    if (c.error) kv.push(["error", c.error]);
    if (c.chars !== undefined)
        kv.push([
            "model received",
            `${num(c.chars)} chars${c.truncated ? " · cut to the size budget" : ""}`,
        ]);
    if (c.agent) kv.push(["agent", `${c.agent_type || "subagent"} ${c.agent}`]);
    if (c.client) kv.push(["client", c.client]);
    return (
        <div class="tc-det">
            <div class="dwrap">
                <dl class="kv">
                    {kv.map(([k, v]) => (
                        <>
                            <dt>{k}</dt>
                            <dd>{v}</dd>
                        </>
                    ))}
                </dl>
                <div class="blocks">
                    <div>
                        <div class="glabel">arguments</div>
                        <pre class="json">
                            <JsonText value={c.args} />
                        </pre>
                    </div>
                    <div>
                        <div class="glabel">requests to Graylog</div>
                        <Requests c={c} />
                    </div>
                    <div>
                        <Preview c={c} />
                    </div>
                </div>
                <div class="dact">
                    <button
                        class="btn primary"
                        type="button"
                        onClick={() => void playCall(c)}
                    >
                        Open in Playground
                    </button>
                </div>
            </div>
        </div>
    );
}
function CallLine({
    c,
    start,
    span,
    now,
}: {
    c: Call;
    start: number;
    span: number;
    now: number;
}) {
    const s = statusOf(c),
        ends = s === "running" ? now : c.ts + (c.ms || 0) / 1000,
        left = (100 * (c.ts - start)) / span,
        width = Math.max(0.4, (100 * (ends - c.ts)) / span),
        key = callKey(c),
        open = convExpanded.value.has(key),
        ask = askText(c);
    return (
        <div
            class={`tcall${open ? " open" : ""}`}
            tabIndex={0}
            role="button"
            aria-expanded={open}
            data-key={key}
            onClick={(e) => {
                // selecting text in the detail does not close it
                if (!(e.target as HTMLElement).closest(".tc-det"))
                    toggleCall(key);
            }}
            onKeyDown={(e) => {
                if (
                    (e.key === "Enter" || e.key === " ") &&
                    e.target === e.currentTarget
                ) {
                    e.preventDefault();
                    toggleCall(key);
                }
            }}
        >
            <div class="tc-head">
                <span class="mono muted">{c.seq ?? ""}</span>
                <span class="ctool">{c.tool}</span>
                <span class="cwhat" title={ask}>
                    {ask}
                </span>
                <span class="cst">
                    <StatusBadge status={s} />
                    {isMiss(c) && <span class="badge warn">found nothing</span>}
                    {c.truncated && <span class="badge">cut</span>}
                </span>
                <span class="tbar">
                    <span
                        class={
                            s === "running"
                                ? "run"
                                : s === "ok"
                                  ? undefined
                                  : "bad"
                        }
                        style={{
                            left: `${left.toFixed(2)}%`,
                            width: `${Math.min(width, 100 - left).toFixed(2)}%`,
                        }}
                    />
                </span>
                <span class="mono muted num">
                    {s === "running"
                        ? `${fmtDur(now - c.ts)}…`
                        : c.ms == null
                          ? "-"
                          : fmtMs(c.ms)}
                </span>
            </div>
            {open && <CallDetail c={c} status={s} />}
        </div>
    );
}
function TurnCard({
    t,
    k,
    now,
    oneTurn,
}: {
    t: ConversationTurn;
    k: number;
    now: number;
    oneTurn: boolean;
}) {
    const start = t.first_ts,
        end = Math.max(t.last_ts, t.running ? now : 0),
        span = Math.max(0.001, end - start),
        types: Record<string, string> = {};
    for (const c of t.calls)
        if (c.agent && c.agent_type) types[c.agent] = c.agent_type;
    const groups = new Map<string, Call[]>();
    for (const c of t.calls) {
        const name = c.agent
            ? `${types[c.agent] || "subagent"} · ${c.agent.slice(0, 6)}`
            : "main agent";
        groups.set(name, [...(groups.get(name) || []), c]);
    }
    const byAgent = groups.size > 1 || t.calls.some((c) => c.agent),
        meta = [
            whenShort(start),
            fmtDur(t.last_ts - start),
            plural(t.calls.length, "call"),
            t.repo,
            (t.instances || []).join(", "),
            t.client,
        ].filter(Boolean);
    const link = new URLSearchParams(t.turn ? { turn: t.turn } : {});
    return (
        <section class="card turn">
            <div class="card-head">
                <h2>Turn {k + 1}</h2>
                <span class="muted small mono">{meta.join(" · ")}</span>
                <span class="spacer" />
                {t.tier === "summary" && (
                    <span
                        class="badge"
                        title="Previews and requests are no longer kept"
                    >
                        requests summed up
                    </span>
                )}
                {!oneTurn && t.turn && (
                    <a
                        class="small"
                        href={pathOf(
                            "activity",
                            ["conversations", t.conversation],
                            link,
                        )}
                        onClick={(e) => {
                            if (!plainClick(e)) return;
                            e.preventDefault();
                            openConversation(t.conversation, t.turn);
                        }}
                    >
                        Link to this turn
                    </a>
                )}
            </div>
            <div class="tline">
                {[...groups].map(([name, calls]) => (
                    <Fragment key={name}>
                        {byAgent && <div class="agent glabel">{name}</div>}
                        {calls.map((c) => (
                            <CallLine
                                key={callKey(c)}
                                c={c}
                                start={start}
                                span={span}
                                now={now}
                            />
                        ))}
                    </Fragment>
                ))}
            </div>
        </section>
    );
}
function ConversationView() {
    const open = convOpen.value!,
        d = convDetail.value,
        now = Date.now() / 1000;
    return (
        <div id="convDetail">
            <div class="convhead">
                <button
                    class="btn sm ghost"
                    type="button"
                    onClick={closeConversation}
                >
                    ← Conversations
                </button>
                <h2 class="mono">
                    {convShort(open.id)}
                    {open.turn && <span class="muted small"> · one turn</span>}
                </h2>
                <span class="spacer" />
                {open.turn && (
                    <button
                        class="btn sm ghost"
                        type="button"
                        onClick={() => openConversation(open.id)}
                    >
                        Whole conversation
                    </button>
                )}
                <AsyncButton
                    class="btn sm"
                    type="button"
                    disabled={!d}
                    run={exportConversation}
                >
                    Download JSON
                </AsyncButton>
            </div>
            {!d ? (
                convDetailError.value ? (
                    <div class="inline-msg">{convDetailError.value}</div>
                ) : (
                    <Skeleton />
                )
            ) : !d.turns.length && !d.cold ? (
                <div class="allclear">
                    // nothing of this conversation is kept
                </div>
            ) : (
                <>
                    {d.turns.map((t, k) => (
                        <TurnCard
                            key={t.turn}
                            t={t}
                            k={k}
                            now={now}
                            oneTurn={!!open.turn}
                        />
                    ))}
                    {d.cold && (
                        <section class="card">
                            <div class="card-head">
                                <h2>Older turns, as a summary</h2>
                            </div>
                            <p class="muted small">
                                Calls older than {d.detail_days} days are folded
                                into one line per conversation:{" "}
                                {plural(d.cold.calls, "call")},{" "}
                                {plural(d.cold.errors, "error")},{" "}
                                {d.cold.misses} found nothing, from{" "}
                                {new Date(
                                    d.cold.first_ts * 1000,
                                ).toLocaleString()}{" "}
                                to{" "}
                                {new Date(
                                    d.cold.last_ts * 1000,
                                ).toLocaleString()}
                                .
                            </p>
                            <ToolChips tools={d.cold.tools || []} />
                        </section>
                    )}
                </>
            )}
        </div>
    );
}

export function Conversations() {
    const page = currentPage.value,
        tab = activityTab.value,
        open = convOpen.value,
        f = convFilters.value,
        ready = !!state.value,
        here = ready && page === "activity" && tab === "conversations",
        box = useRef<HTMLDivElement>(null);
    // the list follows its filters, the environment picked above the page and the Playground switch
    useEffect(() => {
        if (here && !open) void loadConversations();
    }, [here, !open, f.range, f.q, currentEnv.value, playgroundRuns.value]);
    useEffect(() => {
        if (!here || !open) return stopRefresh();
        if (!convDetail.value && !convDetailError.value)
            void loadConversationDetail();
        else scheduleRefresh();
    }, [here, open, convDetail.value, convDetailError.value]);
    useEffect(() => {
        const seen = () => {
            if (document.visibilityState === "visible") scheduleRefresh();
            else stopRefresh();
        };
        document.addEventListener("visibilitychange", seen);
        return () => {
            document.removeEventListener("visibilitychange", seen);
            stopRefresh();
        };
    }, []);
    // opened from a link to one call: show it, once (not again on each refresh)
    useLayoutEffect(() => {
        if (!convDetail.value || !takeReveal()) return;
        const row = box.current?.querySelector<HTMLElement>(".tcall.open");
        if (row) {
            row.scrollIntoView({ block: "center" });
            row.focus({ preventScroll: true });
        }
    }, [convDetail.value, open]);
    useEffect(() => {
        const key = (e: KeyboardEvent) => {
            if (
                e.defaultPrevented ||
                currentPage.value !== "activity" ||
                activityTab.value !== "conversations" ||
                e.ctrlKey ||
                e.metaKey ||
                e.altKey ||
                (e.target as HTMLElement).closest(
                    "input,textarea,select,[contenteditable]",
                ) ||
                document.querySelector("dialog[open]")
            )
                return;
            const k = e.key.toLowerCase();
            if (k === "escape" && convOpen.value) {
                e.preventDefault();
                closeConversation();
            } else if (k === "j" || k === "k") {
                e.preventDefault();
                const rows = [
                    ...(box.current?.querySelectorAll<HTMLElement>(
                        convOpen.value ? ".tcall" : "tr.go",
                    ) || []),
                ];
                if (!rows.length) return;
                const cur = rows.indexOf(document.activeElement as HTMLElement),
                    d = k === "j" ? 1 : -1,
                    n =
                        cur < 0
                            ? d > 0
                                ? 0
                                : rows.length - 1
                            : Math.max(0, Math.min(rows.length - 1, cur + d));
                rows[n].focus();
                rows[n].scrollIntoView({ block: "nearest" });
            }
        };
        document.addEventListener("keydown", key);
        return () => document.removeEventListener("keydown", key);
    }, []);
    return (
        <div ref={box}>
            {open ? <ConversationView /> : <ConversationList />}
        </div>
    );
}
