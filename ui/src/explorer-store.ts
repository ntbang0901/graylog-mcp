// The Logs screen's state: its view (the address), the explorer's answers per tab, and the automatic refresh.
import { signal } from "@preact/signals";
import { api, errorText, storage } from "./api";
import { pagePath, registerScreen, setAddress, type How } from "./router";
import {
    currentEnv,
    currentPage,
    instances,
    navigate,
    state,
    toast,
    usage,
} from "./store";
import {
    DEFAULT_VIEW,
    LOG_EVERY,
    drillQuery,
    explorerPath,
    normalView,
    parseSaved,
    recentViews,
    requestOf,
    savedOf,
    viewKey,
    viewOfRoute,
    viewOfSaved,
    windowOf,
    type Answers,
    type ExplorerView,
    type SavedView,
    type Slot,
} from "./explorer";
import type { Usage } from "./scripts/types";

export interface Answer<S extends Slot = Slot> {
    key: string;
    data: Answers[S] | null;
    error: string;
}
export const view = signal<ExplorerView>(DEFAULT_VIEW);
export const answers = signal<{ [S in Slot]?: Answer<S> }>({});
export const inflight = signal(false);
export const updated = signal<Date | null>(null);
// the keys of the rows shown open; an open row pauses the refresh, so it does not move under the reader
export const openRows = signal<ReadonlySet<string>>(new Set());
export const sort = signal<{ key: string; dir: 1 | -1 }>({
    key: "count",
    dir: -1,
});
export const slow = signal<number | null>(null);
const EVERY_KEY = "gm-logs-every",
    FAV_KEY = "gm-logs-favs";
const storedEvery = Number(storage.get(EVERY_KEY));
export const every = signal(LOG_EVERY.includes(storedEvery) ? storedEvery : 0);
export const favourites = signal<SavedView[]>(parseSaved(storage.get(FAV_KEY)));
export const recent = signal<SavedView[] | null>(null);

registerScreen("logs", {
    path: () => explorerPath(view.value),
    apply: (r) => {
        view.value = viewOfRoute(r);
        openRows.value = new Set();
    },
});

export const isCurrent = () => currentPage.value === "logs";
export function knownInstance(name: string | null) {
    return instances.value.find((e) => e.name === name) ?? null;
}
// the global environment picker's environment, else the default one
export function defaultInstance() {
    const envs = instances.value;
    return (
        knownInstance(currentEnv.value)?.name ||
        (envs.find((e) => e.default) || envs[0])?.name ||
        null
    );
}
// every change of view goes through here: the address follows while the screen is shown, and open rows close
export function update(patch: Partial<ExplorerView>, how: How = "push") {
    view.value = normalView({ ...view.value, ...patch });
    openRows.value = new Set();
    if (isCurrent()) setAddress(pagePath("logs"), how);
}
export function answerOf<S extends Slot>(slot: S): Answer<S> | undefined {
    return answers.value[slot] as Answer<S> | undefined;
}
function setAnswer<S extends Slot>(slot: S, a: Answer<S>) {
    answers.value = { ...answers.value, [slot]: a };
}

// ------------------------------------------------------------------ loading
let seq = 0,
    pending = "",
    timer: ReturnType<typeof setTimeout> | undefined;
// refresh: the screen's own reload (Refresh, the timer): it keeps the answer shown until the new one comes, sends
// refresh=1 so it is not recorded as Activity, and is skipped while a request is out. seq makes sure an older
// request finishing late never replaces the answer of a newer one.
export async function load(refresh = false) {
    clearTimeout(timer);
    const v = view.value;
    if (!isCurrent() || !state.value || !knownInstance(v.instance)) return;
    const { slot, url } = requestOf(v),
        had = answerOf(slot);
    if (!refresh && had?.key === url && (had.data || pending === url)) {
        if (had.data) schedule();
        return;
    }
    // one request at a time: a slow Graylog is not asked again before it answers
    if (refresh && inflight.value) return;
    const keep = refresh && had?.key === url ? had : null;
    setAnswer(slot, { key: url, data: keep?.data ?? null, error: "" });
    inflight.value = true;
    pending = url;
    const mine = ++seq;
    try {
        const got = await api<Answers[typeof slot]>(
            refresh ? `${url}&refresh=1` : url,
        );
        if (mine !== seq) return;
        setAnswer(slot, { key: url, data: got, error: "" });
        updated.value = new Date();
    } catch (e) {
        if (mine !== seq) return;
        setAnswer(slot, {
            key: url,
            data: keep?.data ?? null,
            error: errorText(e),
        });
    } finally {
        if (mine === seq) {
            inflight.value = false;
            pending = "";
        }
    }
    schedule();
}
// the next automatic refresh: only on this screen, with the browser tab visible and no row open to read
export function schedule() {
    clearTimeout(timer);
    const s = every.value;
    if (
        !s ||
        !isCurrent() ||
        document.visibilityState !== "visible" ||
        openRows.value.size
    )
        return;
    timer = setTimeout(() => void load(true), Math.max(30, s) * 1000);
}
export function stop() {
    clearTimeout(timer);
}
export function setEvery(s: number) {
    every.value = LOG_EVERY.includes(s) ? s : 0;
    storage.set(EVERY_KEY, String(every.value));
    schedule();
}
export function toggleRow(key: string) {
    const next = new Set(openRows.value);
    if (!next.delete(key)) next.add(key);
    openRows.value = next;
    schedule();
}

// ------------------------------------------------------------------ actions on the view
export function currentWindow() {
    return windowOf(answerOf(requestOf(view.value).slot)?.data, view.value);
}
export function drill(clause: string, tab?: ExplorerView["tab"]) {
    update({
        q: drillQuery(view.value.q, clause),
        ...(tab ? { tab } : {}),
        offset: 0,
        trace: null,
    });
}
export function narrowTo(from: Date, to: Date) {
    update({
        from: from.toISOString(),
        to: to.toISOString(),
        range: "",
        offset: 0,
    });
}
export function openSaved(s: SavedView) {
    update(viewOfSaved(s, view.value));
}
export function isSaved(v: ExplorerView) {
    const cur = viewKey(v);
    return favourites.value.some((f) => viewKey(f) === cur);
}
export function toggleFavourite() {
    const v = view.value,
        cur = viewKey(v),
        list = favourites.value.filter((f) => viewKey(f) !== cur);
    if (list.length === favourites.value.length) list.unshift(savedOf(v));
    favourites.value = list.slice(0, 30);
    storage.set(FAV_KEY, JSON.stringify(favourites.value));
}
export async function loadRecent() {
    if (recent.value) return;
    try {
        const u =
            usage.value ||
            (await api<Usage>("/api/usage?range=24h&source=mcp"));
        recent.value = recentViews(u.recent || []);
    } catch (e) {
        recent.value = [];
        toast(
            `Could not read the agent's recent searches: ${errorText(e)}`,
            "fail",
        );
    }
}
// the All logs tab of another screen's lines, e.g. the log drawer's
export async function openInLogs(v: {
    instance: string;
    query: string;
    range?: string;
    from?: string;
    to?: string;
}) {
    const patch: Partial<ExplorerView> = {
        tab: "all",
        instance: v.instance,
        q: v.query === "*" ? "" : v.query,
        streams: "",
        range: v.range || "24h",
        from: v.from || "",
        to: v.to || "",
        trace: null,
        offset: 0,
    };
    if (isCurrent()) update(patch);
    else {
        view.value = normalView({ ...view.value, ...patch });
        openRows.value = new Set();
        await navigate("logs");
    }
}
