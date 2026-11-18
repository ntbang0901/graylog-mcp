import { after, beforeEach, test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import ts from "typescript";

// Load the real TypeScript modules in Node; shared state must require no browser globals.
const output = await mkdtemp(join(tmpdir(), "graylog-admin-store-"));
for (const name of ["store", "api", "router"]) {
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
    const javascript = outputText
        .replaceAll('"./store"', '"./store.mjs"')
        .replaceAll('"./api"', '"./api.mjs"')
        .replaceAll('"./router"', '"./router.mjs"')
        .replaceAll(
            '"@preact/signals"',
            JSON.stringify(import.meta.resolve("@preact/signals")),
        );
    await writeFile(join(output, `${name}.mjs`), javascript);
}
const store = await import(pathToFileURL(join(output, "store.mjs")).href);
const originalFetch = globalThis.fetch;
after(async () => {
    globalThis.fetch = originalFetch;
    delete globalThis.location;
    delete globalThis.history;
    await rm(output, { recursive: true, force: true });
});
beforeEach(() => {
    store.state.value = null;
    store.checks.value = null;
    store.currentEnv.value = null;
    store.currentPage.value = "home";
    store.dirty.value = null;
    store.confirmation.value = null;
    store.usage.value = null;
    store.stats.value = null;
    store.range.value = "24h";
    globalThis.location = { pathname: "/admin/", search: "" };
    globalThis.history = { replaceState() {}, pushState() {} };
});

test("shared state imports without DOM and derives environment health", () => {
    assert.equal(globalThis.document, undefined);
    assert.deepEqual(store.instances.value, []);
    store.state.value = {
        instances: [{ name: "acme/prod", group: "acme", environment: "prod" }],
    };
    store.currentEnv.value = "acme/prod";
    assert.equal(store.context.value.name, "acme/prod");
    store.checks.value = [
        {
            instance: "acme/prod",
            status: "fail",
            name: "connection",
            detail: "fake failure",
        },
    ];
    assert.equal(store.failing.value, 1);
    store.checks.value = [
        {
            instance: "acme/prod",
            status: "ok",
            name: "connection",
            detail: "connected",
        },
    ];
    assert.equal(store.failing.value, 0);
});

test("navigation keeps edits after cancellation and discards only after acceptance", async () => {
    let discards = 0;
    const edits = {
        what: "log fields",
        save() {},
        discard() {
            discards++;
        },
    };
    store.dirty.value = edits;
    const cancelled = store.navigate("activity");
    assert.equal(store.confirmation.value.danger, true);
    store.resolveConfirm(false);
    assert.equal(await cancelled, false);
    assert.equal(store.currentPage.value, "home");
    assert.equal(store.dirty.value, edits);
    assert.equal(discards, 0);
    const accepted = store.navigate("activity");
    store.resolveConfirm(true);
    assert.equal(await accepted, true);
    assert.equal(store.currentPage.value, "activity");
    assert.equal(store.dirty.value, null);
    assert.equal(discards, 1);
});

test("deleting the selected environment falls back to all and keeps /admin API paths", async () => {
    const paths = [];
    store.currentEnv.value = "acme/deleted";
    store.token.value = "fake-admin-token";
    globalThis.fetch = async (url, init) => {
        paths.push(url);
        assert.equal(init.headers["X-Admin-Token"], "fake-admin-token");
        return Response.json(
            url.endsWith("/api/state")
                ? { instances: [] }
                : { scopes: [{ scope: "global", label: "All" }] },
        );
    };
    await store.loadState();
    assert.equal(store.currentEnv.value, null);
    assert.deepEqual(paths, [
        "/admin/api/state",
        "/admin/api/scope?scope=global",
    ]);
});

test("a slow response from the previous range cannot replace the current dashboard", async () => {
    const pending = [];
    globalThis.fetch = (url) =>
        new Promise((resolve) => pending.push({ url, resolve }));
    const first = store.loadDashboard();
    store.range.value = "7d";
    const second = store.loadDashboard();
    pending[1].resolve(Response.json({ range_hours: 168 }));
    await second;
    assert.equal(store.usage.value.range_hours, 168);
    pending[0].resolve(Response.json({ range_hours: 24 }));
    await first;
    assert.equal(store.usage.value.range_hours, 168);
});
