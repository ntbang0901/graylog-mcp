import { after, test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import ts from "typescript";

// The Logs screen's address and request logic, loaded as the real modules; neither needs a browser.
const output = await mkdtemp(join(tmpdir(), "graylog-admin-explorer-"));
for (const name of ["explorer", "router"]) {
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
    await writeFile(
        join(output, `${name}.mjs`),
        outputText.replaceAll('"./router"', '"./router.mjs"'),
    );
}
const router = await import(pathToFileURL(join(output, "router.mjs")).href);
const explorer = await import(pathToFileURL(join(output, "explorer.mjs")).href);
after(() => rm(output, { recursive: true, force: true }));
const view = (url) =>
    explorer.viewOfRoute(router.readRoute(new URL(url, "http://admin.test")));

test("every Logs view keeps its address", () => {
    for (const url of [
        "/logs/traces?instance=main&from=2026-01-01T10%3A00%3A00.000Z&to=2026-01-01T11%3A00%3A00.000Z&trace=t1",
        "/logs/all?instance=acme&q=level%3A3&streams=a%2Cb&range=4h&offset=200",
        "/logs/dashboard?range=1h",
        "/logs/traces?instance=acme&range=24h",
    ]) {
        const v = view(url);
        assert.equal(explorer.explorerPath(v), url);
        assert.deepEqual(view(explorer.explorerPath(v)), v);
    }
});

test("a ui link from the server opens its trace over its time range", () => {
    const v = view(
        "/logs/traces?instance=main&from=2026-01-01T10:00:00.000Z&to=2026-01-01T11:00:00.000Z&trace=t1",
    );
    assert.equal(v.tab, "traces");
    assert.equal(v.trace, "t1");
    assert.equal(v.instance, "main");
    assert.equal(v.range, "");
    assert.equal(v.from, "2026-01-01T10:00:00.000Z");
    assert.equal(explorer.slotOf(v), "trace");
    assert.match(explorer.requestOf(v).url, /^\/api\/explorer\/trace\/t1\?/);
});

test("a trace or an offset only stays on the tab it belongs to", () => {
    const v = view("/logs/nope?trace=x&offset=-5");
    assert.equal(v.tab, "dashboard");
    assert.equal(v.trace, null);
    assert.equal(v.offset, 0);
});

test("All logs asks for one page at the view's offset", () => {
    const url = explorer.requestOf(
        view("/logs/all?instance=acme&range=4h&offset=200"),
    ).url;
    const q = new URL(url, "http://admin.test").searchParams;
    assert.equal(q.get("offset"), "200");
    assert.equal(q.get("limit"), String(explorer.LOG_PAGE));
    assert.equal(q.get("refresh"), null);
});

test("a broken favourites entry is dropped, not thrown", () => {
    assert.deepEqual(explorer.parseSaved("{oops"), []);
    assert.equal(
        explorer.parseSaved('[{"name":"n","q":"a","range":"1h"},3,null]')
            .length,
        1,
    );
});
