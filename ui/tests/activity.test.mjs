import { after, beforeEach, test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import ts from "typescript";

// The Activity screen's state and address, loaded in Node the way store.test.mjs loads the store.
const output = await mkdtemp(join(tmpdir(), "graylog-admin-activity-"));
const modules = ["store", "api", "router", "activity"];
for (const name of modules) {
    const source = await readFile(
        new URL(`../src/${name}.ts`, import.meta.url),
        "utf8",
    );
    const { outputText } = ts.transpileModule(source, {
        compilerOptions: {
            module: ts.ModuleKind.ESNext,
            target: ts.ScriptTarget.ES2022,
        },
    });
    let javascript = outputText.replaceAll(
        '"@preact/signals"',
        JSON.stringify(import.meta.resolve("@preact/signals")),
    );
    for (const other of modules)
        javascript = javascript.replaceAll(`"./${other}"`, `"./${other}.mjs"`);
    await writeFile(join(output, `${name}.mjs`), javascript);
}
const store = await import(pathToFileURL(join(output, "store.mjs")).href);
const router = await import(pathToFileURL(join(output, "router.mjs")).href);
const activity = await import(pathToFileURL(join(output, "activity.mjs")).href);
const originalFetch = globalThis.fetch;
let pushed = [];
// an address bar: what the page writes is what it reads back
function goTo(url) {
    const u = new URL(url, "http://example.com");
    globalThis.location.pathname = u.pathname;
    globalThis.location.search = u.search;
}
after(async () => {
    globalThis.fetch = originalFetch;
    delete globalThis.location;
    delete globalThis.history;
    await rm(output, { recursive: true, force: true });
});
beforeEach(() => {
    globalThis.location = { pathname: "/activity", search: "", hash: "" };
    pushed = [];
    globalThis.history = {
        replaceState(_s, _t, url) {
            goTo(url);
        },
        pushState(_s, _t, url) {
            pushed.push(url);
            goTo(url);
        },
    };
    store.currentPage.value = "home";
    store.dirty.value = null;
    store.currentEnv.value = null;
    store.range.value = "24h";
    store.playgroundRuns.value = false;
    store.activityTab.value = "dashboard";
    store.filters.value = { q: "", tool: "", status: "all", bucket: null };
    activity.convFilters.value = { range: "7d", q: "", status: "all" };
    activity.convOpen.value = null;
    activity.convDetail.value = null;
    activity.convDetailError.value = "";
});

test("every Activity address reads back to the same view", () => {
    for (const url of [
        "/activity",
        "/activity?range=7d&playground=1",
        "/activity/calls?tool=search_logs&status=error&q=timeout",
        "/activity/calls?range=7d&status=nothing",
        "/activity/conversations",
        "/activity/conversations?playground=1&range=30d&status=running&q=acme",
        "/activity/conversations/0123456789ab?turn=1700000000000&call=abcdef123456",
        "/activity/conversations/legacy-2026-11-01?at=1700000000123",
        "/activity/learned?range=7d",
    ]) {
        goTo(url);
        router.applyRoute(router.readRoute());
        assert.equal(router.pagePath("activity"), url);
    }
});

test("an address the page did not write falls back to defaults", () => {
    goTo("/activity/nope?status=bogus&range=1y");
    router.applyRoute(router.readRoute());
    assert.equal(store.activityTab.value, "dashboard");
    assert.equal(store.range.value, "24h");
    assert.equal(router.pagePath("activity"), "/activity");
    goTo("/activity/conversations?range=1y&status=bogus");
    router.applyRoute(router.readRoute());
    assert.deepEqual(activity.convFilters.value, {
        range: "7d",
        q: "",
        status: "all",
    });
    goTo("/activity/calls?status=running&q=ACME");
    router.applyRoute(router.readRoute());
    assert.equal(store.filters.value.status, "all");
    assert.equal(store.filters.value.q, "acme");
});

test("opening a call lands on its conversation and a turn link keeps the conversation", () => {
    store.currentPage.value = "activity";
    const call = { ts: 1700000000.123, tool: "search_logs", ok: true };
    assert.equal(
        activity.callHref(call),
        "/activity/conversations/legacy-2023-11-14?at=1700000000123",
    );
    activity.openCallInConversation({
        ...call,
        conversation: "0123456789ab",
        call_id: "abcdef123456",
    });
    assert.equal(
        location.pathname + location.search,
        "/activity/conversations/0123456789ab?call=abcdef123456",
    );
    assert.deepEqual([...activity.convExpanded.value], ["abcdef123456"]);
    assert.equal(activity.takeReveal(), true);
    assert.equal(activity.takeReveal(), false);
    activity.openConversation("0123456789ab", "1700000000000");
    assert.equal(
        location.search,
        "?turn=1700000000000",
        "a turn opened from the conversation shows no call",
    );
    activity.closeConversation();
    assert.equal(
        location.pathname + location.search,
        "/activity/conversations",
    );
    assert.equal(activity.convOpen.value, null);
});

test("the address is written only while Activity is on screen", () => {
    goTo("/fields");
    activity.setActivityTab("calls");
    assert.equal(location.pathname, "/fields");
    store.currentPage.value = "activity";
    activity.setActivityTab("learned");
    assert.equal(location.pathname, "/activity/learned");
});

test("the Overview's links into Activity write the address of the list they show", async () => {
    await store.openCalls("nothing");
    assert.equal(store.currentPage.value, "activity");
    assert.equal(store.activityTab.value, "calls");
    assert.deepEqual(pushed, ["/activity/calls?status=nothing"]);
});

test("Playground runs are counted on Activity only, and a page change reloads only when the source changes", async () => {
    const urls = [];
    globalThis.fetch = async (url) => {
        urls.push(url);
        return Response.json({ range_hours: 24 });
    };
    store.currentPage.value = "activity";
    store.playgroundRuns.value = true;
    await store.loadDashboard();
    await store.loadDashboard(false);
    store.currentPage.value = "home";
    await store.loadDashboard(false);
    await store.loadDashboard(false);
    assert.deepEqual(urls, [
        "/api/usage?range=24h&source=all",
        "/api/usage?range=24h&source=mcp",
    ]);
});

test("the conversation list asks for its filters once, and again when forced", async () => {
    const urls = [];
    globalThis.fetch = async (url) => {
        urls.push(url);
        return Response.json({ enabled: true, total: 0, turns: [] });
    };
    activity.convFilters.value = { range: "30d", q: "acme", status: "error" };
    store.currentEnv.value = "acme/prod";
    store.playgroundRuns.value = true;
    await activity.loadConversations();
    await activity.loadConversations();
    await activity.loadConversations(true);
    const want =
        "/api/conversations?range=30d&source=all&q=acme&instance=acme%2Fprod";
    assert.deepEqual(urls, [want, want]);
    assert.equal(activity.convList.value.total, 0);
});

test("a conversation answered after another was opened is not shown", async () => {
    const pending = [];
    globalThis.fetch = (url) =>
        new Promise((resolve) => pending.push({ url, resolve }));
    activity.convOpen.value = {
        id: "0123456789ab",
        turn: null,
        call: null,
        at: null,
    };
    const first = activity.loadConversationDetail();
    activity.convOpen.value = {
        id: "ba9876543210",
        turn: "1700000000000",
        call: null,
        at: null,
    };
    const second = activity.loadConversationDetail();
    assert.equal(
        pending[1].url,
        "/api/conversations/ba9876543210?turn=1700000000000",
    );
    pending[1].resolve(
        Response.json({ conversation: "ba9876543210", turns: [], cold: null }),
    );
    await second;
    pending[0].resolve(
        Response.json({ conversation: "0123456789ab", turns: [], cold: null }),
    );
    await first;
    assert.equal(activity.convDetail.value.conversation, "ba9876543210");
    activity.stopRefresh();
});

test("turns filter by outcome and durations read short", () => {
    const t = { errors: 0, misses: 2, running: 1 };
    assert.equal(activity.turnMatches(t, "all"), true);
    assert.equal(activity.turnMatches(t, "error"), false);
    assert.equal(activity.turnMatches(t, "nothing"), true);
    assert.equal(activity.turnMatches(t, "running"), true);
    assert.equal(activity.fmtDur(0.25), "250ms");
    assert.equal(activity.fmtDur(4.25), "4.3s");
    assert.equal(activity.fmtDur(125), "2m 5s");
    assert.equal(activity.fmtDur(3725), "1h 2m");
    assert.equal(activity.statusOf({ ok: false }), "error");
    assert.equal(
        activity.statusOf({ ok: false, status: "cancelled" }),
        "cancelled",
    );
});
