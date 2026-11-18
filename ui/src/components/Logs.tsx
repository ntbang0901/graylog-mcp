import { useEffect, useRef, useState } from "preact/hooks";
import { currentPage, instances, label, openEnv, state, toast } from "../store";
import {
    LOG_EVERY,
    LOG_RANGES,
    explorerPath,
    graylogSearchUrl,
    localInput,
    requestOf,
    type DashboardAnswer,
    type ExplorerView,
    type LogTab,
    type MessagesAnswer,
    type TraceAnswer,
    type TracesAnswer,
} from "../explorer";
import {
    answerOf,
    currentWindow,
    defaultInstance,
    every,
    favourites,
    inflight,
    isCurrent,
    isSaved,
    knownInstance,
    load,
    loadRecent,
    openSaved,
    recent,
    schedule,
    setEvery,
    stop,
    toggleFavourite,
    update,
    updated,
    view,
} from "../explorer-store";
import { Dashboard } from "./LogsDashboard";
import { MessagesTable, TraceView, TracesList } from "./LogsLists";
import { Icon, Page, Panel, Refresh, Skeleton, Tabs } from "./UI";

const TABS: [LogTab, string][] = [
    ["dashboard", "Dashboard"],
    ["traces", "By trace"],
    ["all", "All logs"],
];
// the bar's fields as typed; they become the view on Search. range is "custom" for from … to …
interface Draft {
    instance: string;
    q: string;
    streams: string;
    range: string;
    from: string;
    to: string;
}
const draftOf = (v: ExplorerView): Draft => ({
    instance: v.instance || "",
    q: v.q,
    streams: v.streams,
    range: v.from && v.to ? "custom" : v.range,
    from: v.from ? localInput(v.from) : "",
    to: v.to ? localInput(v.to) : "",
});
function submit(d: Draft) {
    const v = view.value;
    let range = d.range,
        from = "",
        to = "";
    if (d.range === "custom") {
        const f = new Date(d.from),
            t = new Date(d.to);
        if (isNaN(+f) || isNaN(+t) || f >= t) {
            toast("Pick a start before the end", "fail");
            return;
        }
        // the inputs hold minutes: an untouched one keeps the view's exact instant
        from = d.from === localInput(v.from) ? v.from : f.toISOString();
        to = d.to === localInput(v.to) ? v.to : t.toISOString();
        range = "";
    }
    const before = explorerPath(v);
    update({
        instance: d.instance || null,
        q: d.q.trim(),
        streams: d.streams.trim(),
        range,
        from,
        to,
        offset: 0,
        trace: null,
    });
    // Search on the same view reads Graylog again instead of showing the kept answer
    if (explorerPath(view.value) === before) void load(true);
}

function Bar({ v }: { v: ExplorerView }) {
    const [draft, setDraft] = useState(() => draftOf(v)),
        fromInput = useRef<HTMLInputElement>(null),
        path = explorerPath(v);
    useEffect(() => setDraft(draftOf(v)), [path]);
    const set = (patch: Partial<Draft>) => setDraft({ ...draft, ...patch }),
        known = !!knownInstance(v.instance),
        saved = isSaved(v),
        favs = favourites.value,
        agent = recent.value || [],
        ranges =
            draft.range === "custom" || LOG_RANGES.includes(draft.range)
                ? LOG_RANGES
                : [...LOG_RANGES, draft.range];
    return (
        <form
            class="fbar lg-bar"
            role="search"
            aria-label="Which logs"
            onSubmit={(e) => {
                e.preventDefault();
                submit(draft);
            }}
        >
            <select
                class="sel auto-width"
                aria-label="Environment"
                value={draft.instance}
                onChange={(e) =>
                    submit({ ...draft, instance: e.currentTarget.value })
                }
            >
                {!known && v.instance && (
                    <option value={v.instance}>
                        {v.instance} (not in this config)
                    </option>
                )}
                {instances.value.map((e) => (
                    <option value={e.name}>{label(e)}</option>
                ))}
            </select>
            <label class="search lg-q">
                <Icon name="search" />
                <input
                    type="search"
                    placeholder="Graylog query; empty = every log"
                    autoComplete="off"
                    spellcheck={false}
                    aria-label="Query"
                    value={draft.q}
                    onInput={(e) => set({ q: e.currentTarget.value })}
                />
            </label>
            <input
                class="lg-streams"
                type="text"
                placeholder="streams (comma-separated)"
                autoComplete="off"
                aria-label="Streams, comma-separated names or ids"
                value={draft.streams}
                onInput={(e) => set({ streams: e.currentTarget.value })}
            />
            <select
                class="sel auto-width"
                aria-label="Time range"
                value={draft.range}
                onChange={(e) => {
                    const r = e.currentTarget.value;
                    if (r !== "custom") {
                        submit({ ...draft, range: r });
                        return;
                    }
                    const w = currentWindow() || [
                        new Date(Date.now() - 3600000),
                        new Date(),
                    ];
                    set({
                        range: r,
                        from: localInput(w[0]),
                        to: localInput(w[1]),
                    });
                    requestAnimationFrame(() => fromInput.current?.focus());
                }}
            >
                {ranges.map((r) => (
                    <option value={r}>last {r}</option>
                ))}
                <option value="custom">from … to …</option>
            </select>
            {draft.range === "custom" && (
                <span class="lg-custom">
                    <input
                        ref={fromInput}
                        type="datetime-local"
                        aria-label="From"
                        value={draft.from}
                        onInput={(e) => set({ from: e.currentTarget.value })}
                    />
                    <span class="muted">to</span>
                    <input
                        type="datetime-local"
                        aria-label="To"
                        value={draft.to}
                        onInput={(e) => set({ to: e.currentTarget.value })}
                    />
                </span>
            )}
            <button class="btn sm primary" type="submit">
                Search logs
            </button>
            <span class="spacer" />
            <select
                class="sel auto-width"
                aria-label="Open a favourite view"
                value=""
                onChange={(e) => {
                    const f = favs[Number(e.currentTarget.value)];
                    e.currentTarget.value = "";
                    if (f) openSaved(f);
                }}
            >
                <option value="">
                    Favourites{favs.length ? ` (${favs.length})` : ""}
                </option>
                {favs.map((f, i) => (
                    <option value={String(i)}>{f.name}</option>
                ))}
            </select>
            <button
                class="btn sm ghost"
                type="button"
                aria-pressed={saved}
                title={
                    saved
                        ? "Remove this view from the favourites"
                        : "Keep this view in this browser's favourites"
                }
                onClick={toggleFavourite}
            >
                {saved ? "★ Saved" : "☆ Save view"}
            </button>
            <select
                class="sel auto-width"
                aria-label="Open a recent search of the agent"
                value=""
                onFocus={() => void loadRecent()}
                onPointerDown={() => void loadRecent()}
                onChange={(e) => {
                    const r = agent[Number(e.currentTarget.value)];
                    e.currentTarget.value = "";
                    if (r) openSaved(r);
                }}
            >
                <option value="">
                    Recent from the agent
                    {agent.length ? ` (${agent.length})` : ""}
                </option>
                {agent.map((r, i) => (
                    <option value={String(i)}>{r.name}</option>
                ))}
            </select>
        </form>
    );
}

function Body({ v, top }: { v: ExplorerView; top: () => void }) {
    const s = state.value;
    if (!s) return <Skeleton />;
    if (!knownInstance(v.instance)) {
        if (v.instance)
            return (
                <div class="banner warn">
                    <Icon name="warn" />
                    <div class="text">
                        Environment <span class="mono">{v.instance}</span> is
                        not in this admin page's config ({s.config_path}). Pick
                        an environment above, or open the admin page of the
                        config that has it.
                    </div>
                </div>
            );
        if (instances.value.length) return <Skeleton />;
        return (
            <div class="inline-msg">
                No environment yet.{" "}
                <button
                    type="button"
                    class="lg-link"
                    onClick={() => openEnv(null)}
                >
                    Add one
                </button>{" "}
                to read its logs.
            </div>
        );
    }
    const { slot, url } = requestOf(v),
        a = answerOf(slot),
        mine = a?.key === url ? a : null;
    if (mine?.error && !mine.data)
        return (
            <div class="banner fail">
                <Icon name="fail" />
                <div class="text">{mine.error}</div>
            </div>
        );
    if (!mine?.data) return <Skeleton />;
    const d = mine.data;
    return (
        <>
            {mine.error && (
                <div class="inline-msg lg-stale">
                    The last refresh failed: {mine.error}. Showing the previous
                    answer.
                </div>
            )}
            {slot === "dashboard" ? (
                <Dashboard d={d as DashboardAnswer} />
            ) : slot === "traces" ? (
                <TracesList d={d as TracesAnswer} />
            ) : slot === "trace" ? (
                <TraceView d={d as TraceAnswer} />
            ) : (
                <MessagesTable d={d as MessagesAnswer} top={top} />
            )}
        </>
    );
}

export function Logs() {
    const v = view.value,
        active = currentPage.value === "logs" && !!state.value,
        path = explorerPath(v),
        body = useRef<HTMLDivElement>(null);
    useEffect(() => {
        if (!active) {
            stop();
            return;
        }
        // the screen's own environment starts as the global picker's; written into the address from then on
        if (!v.instance) {
            const name = defaultInstance();
            if (name) {
                update({ instance: name }, "replace");
                return;
            }
        }
        void load();
    }, [active, path, instances.value]);
    useEffect(() => {
        const visible = () => {
                if (document.visibilityState === "visible") schedule();
                else stop();
            },
            key = (e: KeyboardEvent) => {
                if (
                    e.key !== "Escape" ||
                    e.defaultPrevented ||
                    !isCurrent() ||
                    !view.value.trace ||
                    (e.target as HTMLElement).closest(
                        "input,textarea,select",
                    ) ||
                    document.querySelector("dialog[open]")
                )
                    return;
                e.preventDefault();
                update({ trace: null });
            };
        document.addEventListener("visibilitychange", visible);
        document.addEventListener("keydown", key);
        return () => {
            document.removeEventListener("visibilitychange", visible);
            document.removeEventListener("keydown", key);
        };
    }, []);
    const inst = knownInstance(v.instance),
        graylog = graylogSearchUrl(inst?.url, v.q, currentWindow()),
        at = updated.value?.toLocaleTimeString([], { hour12: false }),
        busy = inflight.value;
    return (
        <Page
            id="logs"
            tools={
                <>
                    <span class="muted small mono" aria-live="polite">
                        {at
                            ? `Updated ${at}${busy ? " · loading…" : ""}`
                            : busy
                              ? "loading…"
                              : ""}
                    </span>
                    <select
                        class="sel auto-width"
                        aria-label="Auto refresh"
                        value={String(every.value)}
                        onChange={(e) =>
                            setEvery(Number(e.currentTarget.value))
                        }
                    >
                        {LOG_EVERY.map((s) => (
                            <option value={String(s)}>
                                {s
                                    ? `every ${s < 60 ? `${s} s` : `${s / 60} min`}`
                                    : "Auto refresh off"}
                            </option>
                        ))}
                    </select>
                    <Refresh run={() => load(true)} />
                    {graylog && (
                        <a
                            class="btn sm ghost"
                            href={graylog}
                            target="_blank"
                            rel="noopener noreferrer"
                        >
                            Open in Graylog ↗
                        </a>
                    )}
                </>
            }
        >
            <Bar v={v} />
            <Tabs
                id="lgTabs"
                label="Log views"
                options={TABS}
                value={v.tab}
                onChange={(t) => update({ tab: t as LogTab })}
            />
            <div ref={body}>
                {TABS.map(([t]) => (
                    <Panel tabs="lgTabs" id={t} value={v.tab}>
                        {t === v.tab && (
                            <Body
                                v={v}
                                top={() =>
                                    body.current?.scrollIntoView({
                                        block: "start",
                                    })
                                }
                            />
                        )}
                    </Panel>
                ))}
            </div>
        </Page>
    );
}
