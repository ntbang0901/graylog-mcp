// The Activity screen's views and their address: the Dashboard, the Conversations (a list of turns, or one
// conversation), the Calls and what was Learned.
import { signal } from "@preact/signals";
import { api, base, errorText, token } from "./api";
import {
    activityTab,
    currentEnv,
    currentPage,
    filters,
    playgroundRuns,
    range,
} from "./store";
import {
    pagePath,
    pathOf,
    registerScreen,
    setAddress,
    type How,
    type Route,
} from "./router";
import type {
    Call,
    ConversationDetail,
    ConversationList,
    TurnRow,
} from "./scripts/types";

export const ACT_TABS = ["dashboard", "conversations", "calls", "learned"];
export const CALL_STATUSES = ["all", "nothing", "error"];
export const TURN_STATUSES = ["all", "error", "nothing", "running"];
export const CONV_RANGES = ["24h", "7d", "30d", "90d"];
const oneOf = (v: string | null | undefined, allowed: string[], or: string) =>
    v && allowed.includes(v) ? v : or;

// ------------------------------------------------------------------ calls and conversations
const utcDay = (ts: number) => new Date(ts * 1000).toISOString().slice(0, 10);
// the server groups the calls of earlier versions, which have no conversation, by day (usage_conversations)
export const conversationOf = (c: Call) =>
    c.conversation || `legacy-${utcDay(c.ts)}`;
export const convShort = (id: string) =>
    id === "playground" || id === "explorer" || id.startsWith("legacy-")
        ? id
        : id.slice(0, 6);
// a call without a call_id (earlier versions) is found by the millisecond it started
export const callKey = (c: Call) =>
    c.call_id || `at:${Math.round(c.ts * 1000)}`;
export function callHref(c: Call) {
    const q = new URLSearchParams();
    if (c.call_id) q.set("call", c.call_id);
    else q.set("at", String(Math.round(c.ts * 1000)));
    return pathOf("activity", ["conversations", conversationOf(c)], q);
}
export const statusOf = (c: Call) => c.status || (c.ok ? "ok" : "error");
export const isMiss = (c: Call) => c.ok && c.results === 0;
export const STATUS_CLASS: Record<string, string> = {
    ok: "ok",
    running: "info",
    cancelled: "warn",
    interrupted: "warn",
    refused: "fail",
    error: "fail",
};
export const STATUS_WHY: Record<string, string> = {
    running: "still running",
    cancelled: "the client stopped waiting for it",
    interrupted: "its server stopped before it ended",
    refused: "refused before it ran",
    error: "failed",
};
export function fmtDur(s: number) {
    s = Math.max(0, s);
    return s < 1
        ? `${Math.round(s * 1000)}ms`
        : s < 60
          ? `${s.toFixed(s < 10 ? 1 : 0)}s`
          : s < 3600
            ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`
            : `${Math.floor(s / 3600)}h ${Math.round((s / 60) % 60)}m`;
}
export function turnMatches(t: TurnRow, status: string) {
    return status === "error"
        ? t.errors > 0
        : status === "nothing"
          ? t.misses > 0
          : status === "running"
            ? t.running > 0
            : true;
}

// ------------------------------------------------------------------ conversations: state and loading
export interface OpenConversation {
    id: string;
    turn: string | null;
    call: string | null;
    at: string | null;
}
export const convFilters = signal({ range: "7d", q: "", status: "all" });
export const convOpen = signal<OpenConversation | null>(null);
export const convList = signal<ConversationList | null>(null);
export const convListError = signal("");
export const convDetail = signal<ConversationDetail | null>(null);
export const convDetailError = signal("");
// the calls shown with their detail, by callKey
export const convExpanded = signal<Set<string>>(new Set());
let reveal = false;
// true once after a conversation is opened on one of its calls: the page then scrolls to it
export function takeReveal() {
    const r = reveal;
    reveal = false;
    return r;
}

const sourceParam = () => (playgroundRuns.value ? "all" : "mcp");
let listKey = "";
export async function loadConversations(force = false) {
    const f = convFilters.value,
        q = new URLSearchParams({ range: f.range, source: sourceParam() });
    if (f.q) q.set("q", f.q);
    if (currentEnv.value) q.set("instance", currentEnv.value);
    const key = q.toString();
    // the same list is loading or loaded already
    if (!force && key === listKey && !convListError.value) return;
    listKey = key;
    convList.value = null;
    convListError.value = "";
    try {
        const got = await api<ConversationList>(`/api/conversations?${key}`);
        if (listKey === key) convList.value = got;
    } catch (e) {
        if (listKey === key) convListError.value = errorText(e);
    }
}

const detailPath = (o: OpenConversation, suffix = "") =>
    `/api/conversations/${encodeURIComponent(o.id)}${suffix}${o.turn ? `?turn=${encodeURIComponent(o.turn)}` : ""}`;
const sameView = (a: OpenConversation | null, b: OpenConversation | null) =>
    !!a && !!b && a.id === b.id && a.turn === b.turn;
let refreshTimer: ReturnType<typeof setTimeout> | undefined,
    detailLoading: OpenConversation | null = null;
export async function loadConversationDetail() {
    stopRefresh();
    const open = convOpen.value;
    if (!open || sameView(detailLoading, open)) return;
    detailLoading = open;
    try {
        const got = await api<ConversationDetail>(detailPath(open));
        if (!sameView(convOpen.value, open)) return;
        convDetail.value = got;
        convDetailError.value = "";
    } catch (e) {
        if (!sameView(convOpen.value, open)) return;
        convDetail.value = null;
        convDetailError.value = errorText(e);
    } finally {
        if (detailLoading === open) detailLoading = null;
    }
    scheduleRefresh();
}
// a turn with a call still running is read again every 5 seconds, while it is on screen
export function scheduleRefresh() {
    stopRefresh();
    if (
        convDetail.value?.turns.some((t) => t.running) &&
        convOpen.value &&
        currentPage.value === "activity" &&
        activityTab.value === "conversations" &&
        typeof document !== "undefined" &&
        document.visibilityState === "visible"
    )
        refreshTimer = setTimeout(() => void loadConversationDetail(), 5000);
}
export function stopRefresh() {
    clearTimeout(refreshTimer);
    refreshTimer = undefined;
}

// the token goes in a header, so the file is fetched here and handed to the browser as a download
export async function exportConversation() {
    const open = convOpen.value;
    if (!open) return;
    const res = await fetch(base() + detailPath(open, "/export"), {
        headers: { "X-Admin-Token": token.value },
    });
    if (!res.ok) {
        const data = (await res.json().catch(() => ({}))) as {
            error?: string;
        };
        throw new Error(data.error || res.statusText);
    }
    const name =
            /filename="([^"]+)"/.exec(
                res.headers.get("content-disposition") || "",
            )?.[1] || "conversation.json",
        href = URL.createObjectURL(await res.blob()),
        a = Object.assign(document.createElement("a"), {
            href,
            download: name,
        });
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(href), 1000);
}

// reload: drop what is shown even when it is the same conversation (a user's click); the address changing to the
// same view (Back to another call of it) keeps it
function showConversation(open: OpenConversation | null, reload: boolean) {
    if (reload || !sameView(convOpen.value, open)) {
        convDetail.value = null;
        convDetailError.value = "";
    }
    convOpen.value = open;
    const key = open?.call || (open?.at ? `at:${open.at}` : null);
    convExpanded.value = new Set(key ? [key] : []);
    reveal = !!key;
    if (!open) stopRefresh();
}

// ------------------------------------------------------------------ the address
// ROOT/activity[/<tab>[/<conversation>]]?<filters>; the dashboard is the bare /activity
export function activityPath() {
    const q = new URLSearchParams(),
        tab = activityTab.value;
    if (range.value !== "24h" && tab !== "conversations")
        q.set("range", range.value);
    if (playgroundRuns.value) q.set("playground", "1");
    if (tab === "calls") {
        const f = filters.value;
        if (f.tool) q.set("tool", f.tool);
        if (f.status !== "all") q.set("status", f.status);
        if (f.q) q.set("q", f.q);
    }
    if (tab === "conversations") {
        const open = convOpen.value;
        if (open) {
            if (open.turn) q.set("turn", open.turn);
            if (open.call) q.set("call", open.call);
            else if (open.at) q.set("at", open.at);
            return pathOf("activity", ["conversations", open.id], q);
        }
        const f = convFilters.value;
        if (f.range !== "7d") q.set("range", f.range);
        if (f.status !== "all") q.set("status", f.status);
        if (f.q) q.set("q", f.q);
    }
    return pathOf("activity", tab === "dashboard" ? [] : [tab], q);
}
export function applyActivity(r: Route) {
    const q = r.query,
        tab = oneOf(r.sub[0], ACT_TABS, "dashboard");
    activityTab.value = tab;
    playgroundRuns.value = q.get("playground") === "1";
    if (tab !== "conversations")
        range.value = q.get("range") === "7d" ? "7d" : "24h";
    if (tab === "calls")
        filters.value = {
            ...filters.value,
            tool: q.get("tool") || "",
            status: oneOf(q.get("status"), CALL_STATUSES, "all"),
            q: (q.get("q") || "").toLowerCase(),
        };
    if (tab === "conversations") {
        const id = r.sub[1] || null,
            call = id ? q.get("call") : null;
        // an open conversation keeps the list's filters it was opened from
        if (!id)
            convFilters.value = {
                range: oneOf(q.get("range"), CONV_RANGES, "7d"),
                status: oneOf(q.get("status"), TURN_STATUSES, "all"),
                q: q.get("q") || "",
            };
        showConversation(
            id
                ? {
                      id,
                      turn: q.get("turn"),
                      call,
                      at: call ? null : q.get("at"),
                  }
                : null,
            false,
        );
    }
}
registerScreen("activity", { path: activityPath, apply: applyActivity });

// only while Activity is on screen: elsewhere the page's address belongs to another screen
export function syncActivityAddress(how: How = "push") {
    if (currentPage.value === "activity") setAddress(pagePath("activity"), how);
}
export function setActivityTab(tab: string, how: How = "push") {
    activityTab.value = oneOf(tab, ACT_TABS, "dashboard");
    syncActivityAddress(how);
}
export function openConversation(
    id: string,
    turn: string | null = null,
    call: string | null = null,
    at: string | null = null,
) {
    showConversation({ id, turn, call, at: call ? null : at }, true);
    activityTab.value = "conversations";
    syncActivityAddress("push");
}
export function openCallInConversation(c: Call) {
    openConversation(
        conversationOf(c),
        null,
        c.call_id || null,
        c.call_id ? null : String(Math.round(c.ts * 1000)),
    );
}
export function closeConversation() {
    showConversation(null, true);
    syncActivityAddress("push");
}
export function toggleCall(key: string) {
    const next = new Set(convExpanded.value);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    convExpanded.value = next;
}
