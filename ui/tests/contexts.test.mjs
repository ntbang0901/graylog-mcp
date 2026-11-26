import { after, test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import ts from "typescript";

const output = await mkdtemp(join(tmpdir(), "graylog-contexts-"));
const source = await readFile(
    new URL("../src/contexts-model.ts", import.meta.url),
    "utf8",
);
const compiled = ts.transpileModule(source, {
    compilerOptions: {
        module: ts.ModuleKind.ESNext,
        target: ts.ScriptTarget.ES2022,
    },
});
await writeFile(join(output, "model.mjs"), compiled.outputText);
const model = await import(pathToFileURL(join(output, "model.mjs")).href);
after(() => rm(output, { recursive: true, force: true }));

const answer = () => ({
    instance: "main",
    query: "service:payment",
    streams: ["stream-a"],
    timezone: "Asia/Ho_Chi_Minh",
    range: {
        from: "2026-11-26 14:05:00+07:00",
        to: "2026-11-26 14:20:00+07:00",
    },
    fields: { trace: "request_id" },
    total: 200,
    messages: [{ ref: "index/id", message: "Timeout" }],
    offset: 100,
});

test("capture uses the executed query and resolved absolute window", () => {
    const d = answer();
    const captured = model.searchItem(d, {
        tab: "all",
        q: "unsubmitted query",
        range: "15m",
    });
    assert.equal(captured.scope.query, "service:payment");
    assert.equal(captured.scope.from_time, "2026-11-26T07:05:00.000Z");
    assert.equal(captured.scope.to_time, "2026-11-26T07:20:00.000Z");
    assert.equal(captured.scope.timezone, "Asia/Ho_Chi_Minh");
    assert.deepEqual(captured.scope.streams, ["stream-a"]);
    assert.match(captured.limits.join(" "), /1 messages displayed of 200/);
    assert.equal(captured.data.offset, 100);
    d.query = "different";
    d.messages[0].message = "Refreshed";
    assert.equal(captured.scope.query, "service:payment");
    assert.equal(captured.data.messages[0].message, "Timeout");
});

test("trace preserves identity, scope and partial coverage", () => {
    const captured = model.traceItem({ ...answer(), trace: "req-a", shown: 1 });
    assert.equal(captured.scope.trace_id, "req-a");
    assert.equal(captured.scope.trace_field, "request_id");
    assert.match(captured.limits.join(" "), /1 of 200/);
    assert.equal(captured.data.trace, "req-a");
});

test("a log keeps its exact reference and independently captured scope", () => {
    const scope = model.scopeOf(answer());
    const captured = model.logItem(
        { ref: "index/id", message: "Exception\n at app" },
        scope,
    );
    scope.query = "changed";
    assert.equal(captured.scope.query, "service:payment");
    assert.equal(captured.scope.ref, "index/id");
    assert.equal(captured.title, "Exception");
    assert.equal(captured.data.message.message, "Exception\n at app");
});

test("deduplication preserves different windows and changed evidence", () => {
    const first = model.searchItem(answer(), { tab: "all" });
    const same = model.searchItem(answer(), { tab: "all" });
    assert.equal(model.selectionKey(first), model.selectionKey(same));
    const other = answer();
    other.range.to = "2026-11-26T14:30:00+07:00";
    assert.notEqual(
        model.selectionKey(first),
        model.selectionKey(model.searchItem(other, { tab: "all" })),
    );
    const refreshed = answer();
    refreshed.total = 201;
    assert.notEqual(
        model.selectionKey(first),
        model.selectionKey(model.searchItem(refreshed, { tab: "all" })),
    );
});

test("large evidence is bounded and explicitly labelled, while scope remains intact", () => {
    const d = answer();
    d.messages = Array.from({ length: 100 }, (_, i) => ({
        ref: `index/${i}`,
        message: "越".repeat(10000),
    }));
    const item = model.searchItem(d, { tab: "all" });
    assert.ok(
        new TextEncoder().encode(JSON.stringify(item.data)).length <= 16000,
    );
    assert.match(item.limits.join(" "), /shortened/);
    assert.equal(item.scope.query, "service:payment");
});
