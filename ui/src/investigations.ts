// The Investigations screen's state: the list, the open investigation and its live stream, the chosen
// branch/check/evidence, and their address ROOT/investigations/<id>/<branch>/<check>/<evidence>.
import { signal } from "@preact/signals";
import { api, base, errorText, token } from "./api";
import { currentPage } from "./store";
import {
    pagePath,
    pathOf,
    registerScreen,
    setAddress,
    type How,
    type Route,
} from "./router";
import {
    advance,
    backoff,
    selectionOf,
    selectionPath,
    type Selection,
} from "./investigations-model";
import { readEvents } from "./sse";
import type {
    InvEvent,
    InvEvidence,
    InvSnapshot,
    InvSummary,
} from "./scripts/types";

export const STATES = [
    "all",
    "running",
    "awaiting_input",
    "interrupted",
    "completed",
    "cancelled",
];
export const invEnabled = signal(true);
// what decided it: "env" (GRAYLOG_MCP_INVESTIGATIONS, which the page cannot change), "setting" or "default"
export const invSource = signal("default");
// set when the switch was just changed: running servers read it at start
export const invRestart = signal(false);
export const invList = signal<InvSummary[] | null>(null);
export const invError = signal("");
export const invFilter = signal({ state: "all", instance: "" });
export const invSnap = signal<InvSnapshot | null>(null);
export const invSel = signal<Selection>({
    id: null,
    branch: null,
    check: null,
    evidence: null,
});
export const invEvidence = signal<InvEvidence | null>(null);
// "live": the stream is open; "connecting": being opened or opened again; "off": it broke and will be retried
export const invLive = signal<"live" | "connecting" | "off" | "idle">("idle");
// the last event seen on this investigation: where a reconnected stream resumes. The snapshot's own cursor is
// where it was read, so the later of the two is what has been taken in.
let seen = 0;

// ------------------------------------------------------------------ address
function investigationsPath() {
    const q = new URLSearchParams(),
        sel = invSel.value;
    if (!sel.id) {
        const f = invFilter.value;
        if (f.state !== "all") q.set("state", f.state);
        if (f.instance) q.set("instance", f.instance);
    }
    return pathOf("investigations", selectionPath(sel), q);
}
function applyInvestigations(r: Route) {
    const sel = selectionOf(r.sub);
    if (!sel.id)
        invFilter.value = {
            state: STATES.includes(r.query.get("state") ?? "")
                ? (r.query.get("state") as string)
                : "all",
            instance: r.query.get("instance") ?? "",
        };
    void select(sel, "none");
}
registerScreen("investigations", {
    path: investigationsPath,
    apply: applyInvestigations,
});
function syncAddress(how: How) {
    if (currentPage.value === "investigations")
        setAddress(pagePath("investigations"), how);
}

// ------------------------------------------------------------------ list
export async function loadInvestigations() {
    try {
        const f = invFilter.value,
            q = new URLSearchParams();
        if (f.state !== "all") q.set("state", f.state);
        if (f.instance) q.set("instance", f.instance);
        const got = await api<{
            enabled: boolean;
            source: string;
            investigations: InvSummary[];
        }>(`/api/investigations?${q}`);
        invEnabled.value = got.enabled;
        invSource.value = got.source;
        invList.value = got.investigations;
        invError.value = "";
    } catch (e) {
        invError.value = errorText(e);
    }
}
export function setInvFilter(patch: Partial<typeof invFilter.value>) {
    invFilter.value = { ...invFilter.value, ...patch };
    syncAddress("replace");
    void loadInvestigations();
}

// ------------------------------------------------------------------ one investigation
export async function loadSnapshot(id: string) {
    try {
        const snap = await api<InvSnapshot>(
            `/api/investigations/${encodeURIComponent(id)}`,
        );
        if (invSel.value.id !== id) return;
        invSnap.value = snap;
        seen = Math.max(seen, snap.cursor);
        invError.value = "";
    } catch (e) {
        if (invSel.value.id === id) invError.value = errorText(e);
    }
}
export async function loadEvidence(id: string, evidence: string) {
    try {
        const got = await api<InvEvidence>(
            `/api/investigations/${encodeURIComponent(id)}/evidence/${encodeURIComponent(evidence)}`,
        );
        const s = invSel.value;
        if (s.id === id && s.evidence === evidence) invEvidence.value = got;
    } catch (e) {
        invError.value = errorText(e);
        invEvidence.value = null;
    }
}
export async function select(sel: Selection, how: How = "push") {
    const was = invSel.value;
    invSel.value = sel;
    syncAddress(how);
    if (!sel.id) {
        stopStream();
        invSnap.value = null;
        invEvidence.value = null;
        return;
    }
    if (was.id !== sel.id) {
        seen = 0;
        invSnap.value = null;
        invEvidence.value = null;
        await loadSnapshot(sel.id);
        startStream(sel.id);
    }
    if (sel.evidence) {
        if (invEvidence.value?.id !== sel.evidence)
            await loadEvidence(sel.id, sel.evidence);
    } else invEvidence.value = null;
}
export const open = (id: string) =>
    select({ id, branch: null, check: null, evidence: null });
export const close = () =>
    select({ id: null, branch: null, check: null, evidence: null });
export const chooseBranch = (branch: string | null) =>
    select({ ...invSel.value, branch, check: null, evidence: null });
export const chooseCheck = (check: string | null) =>
    select({ ...invSel.value, check, evidence: null });
export const chooseEvidence = (evidence: string | null) =>
    select({ ...invSel.value, evidence });

// ------------------------------------------------------------------ live stream
let controller: AbortController | null = null;
let retry: ReturnType<typeof setTimeout> | undefined;
let refresh: ReturnType<typeof setTimeout> | undefined;
export function stopStream() {
    controller?.abort();
    controller = null;
    clearTimeout(retry);
    clearTimeout(refresh);
    invLive.value = "idle";
}
// the snapshot is the truth; events say it moved, so they trigger one fetch however many arrive together
function soon(id: string) {
    clearTimeout(refresh);
    refresh = setTimeout(() => {
        void loadSnapshot(id).then(() => {
            const s = invSel.value;
            if (s.id === id && s.evidence) void loadEvidence(id, s.evidence);
        });
    }, 150);
}
export function startStream(id: string) {
    stopStream();
    const mine = new AbortController();
    controller = mine;
    let attempt = 0;
    const run = async () => {
        if (mine.signal.aborted) return;
        invLive.value = "connecting";
        let resync = false;
        try {
            await readEvents(
                `${base()}/api/investigations/${encodeURIComponent(id)}/events?after=${seen}`,
                token.value,
                (f) => {
                    attempt = 0;
                    invLive.value = "live";
                    if (f.event === "resync") resync = true;
                    if (f.event !== "event") return;
                    const got = advance(seen, [JSON.parse(f.data) as InvEvent]);
                    if (got.gap) resync = true;
                    seen = got.cursor;
                    if (got.fresh.length || got.gap) soon(id);
                },
                mine.signal,
                () => {
                    attempt = 0;
                    invLive.value = "live";
                },
            );
        } catch (e) {
            if (mine.signal.aborted) return;
            invError.value = errorText(e);
        }
        if (mine.signal.aborted) return;
        invLive.value = "off";
        if (resync) await loadSnapshot(id);
        retry = setTimeout(() => void run(), backoff(attempt++) * 1000);
    };
    void run();
}

// the record as a Markdown file: fetched with the token (a link could not carry it) and saved by the browser
export async function downloadExport(snap: InvSnapshot) {
    try {
        const res = await fetch(
            `${base()}/api/investigations/${encodeURIComponent(snap.id)}/export`,
            { headers: { "X-Admin-Token": token.value } },
        );
        if (!res.ok) throw new Error(res.statusText);
        const url = URL.createObjectURL(await res.blob()),
            a = document.createElement("a");
        a.href = url;
        a.download = `${snap.label}.md`;
        a.click();
        URL.revokeObjectURL(url);
    } catch (e) {
        invError.value = errorText(e);
    }
}

// the page's switch; agents' servers read it when they start
export async function setInvestigations(enabled: boolean) {
    try {
        const got = await api<{ enabled: boolean; source: string }>(
            "/api/investigations/switch",
            "POST",
            { enabled },
        );
        invEnabled.value = got.enabled;
        invSource.value = got.source;
        invRestart.value = true;
        invError.value = "";
        await loadInvestigations();
    } catch (e) {
        invError.value = errorText(e);
    }
}
