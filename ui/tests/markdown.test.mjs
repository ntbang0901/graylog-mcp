import { after, test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import ts from "typescript";

// The ticket text of a trace and of a message, loaded as the real module.
const output = await mkdtemp(join(tmpdir(), "graylog-admin-markdown-"));
const source = await readFile(
    new URL("../src/markdown.ts", import.meta.url),
    "utf8",
);
const { outputText } = ts.transpileModule(source, {
    compilerOptions: {
        module: ts.ModuleKind.ESNext,
        target: ts.ScriptTarget.ES2022,
    },
});
await writeFile(join(output, "markdown.mjs"), outputText);
const md = await import(pathToFileURL(join(output, "markdown.mjs")).href);
after(() => rm(output, { recursive: true, force: true }));

test("a table cell keeps to one line and its pipes", () => {
    assert.equal(md.cell("a | b\nc\r\nd"), "a \\| b<br>c<br>d");
    assert.equal(md.cell({ code: "ORD-1001" }), '{"code":"ORD-1001"}');
    assert.equal(md.cell(null), "");
    assert.equal(
        md.cell("![x](https://t.example.com/p.png) <img src=x> `a`"),
        "!\\[x\\](https://t.example.com/p.png) \\<img src=x\\> \\`a\\`",
    );
    const row = md.table(["Field", "Value"], [["order", "ORD-1001 | paid"]]);
    assert.equal(
        row,
        "| Field | Value |\n| --- | --- |\n| order | ORD-1001 \\| paid |",
    );
    // every row has as many cells as the head
    for (const line of row.split("\n"))
        assert.equal(line.split(/(?<!\\)\|/).length, 4);
});

test("a very long value is cut", () => {
    const long = "x".repeat(md.MAX_CELL + 50),
        cut = md.cell(long);
    assert.equal(cut.length, md.MAX_CELL);
    assert.ok(cut.endsWith("…"));
    assert.equal(md.cap("short", 10), "short");
});

test("a trace becomes a heading, its facts and a table of its lines", () => {
    const out = md.traceMarkdown({
        trace: "req_aaa111",
        instance: "acme-prod",
        range: { from: "2026-01-01 10:00", to: "2026-01-01 11:00" },
        total: 3,
        duration_ms: 42,
        lines: [
            {
                time: "2026-01-01 10:00:00.000",
                offset_ms: 0,
                service: "gateway",
                level: "info",
                message: "POST /orders ORD-1001 received",
            },
            {
                time: "2026-01-01 10:00:00.042",
                offset_ms: 42,
                service: "payment",
                level: "error",
                message: "charge refused | code 51\n  at Bank.charge",
            },
        ],
    });
    assert.match(out, /^## Trace req\\_aaa111\n/);
    assert.match(out, /- \*\*Environment:\*\* acme-prod/);
    assert.match(out, /- \*\*Messages:\*\* 2 of 3/);
    assert.match(out, /\| Time \| Offset \| Service \| Level \| Message \|/);
    assert.match(
        out,
        /\| payment \| error \| charge refused \\\| code 51<br> {2}at Bank\.charge \|/,
    );
    assert.match(out, /\| \+42 ms \|/);
});

test("a message keeps its text in a fence it cannot close, and its fields as a table", () => {
    const out = md.messageMarkdown({
        instance: "acme-prod",
        ts: "2026-01-01 10:00:00.042",
        source: "pay-1.example.com",
        service: { field: "service", value: "payment" },
        level: "error",
        message: "refused ```quoted``` text\nsecond line",
        fields: { order_id: "ORD-1001", note: "a | b" },
    });
    assert.match(out, /^## Log message\n/);
    assert.match(out, /- \*\*Service \(service\):\*\* payment/);
    assert.match(out, /````text\nrefused ```quoted``` text\nsecond line\n````/);
    assert.match(out, /\| order\\_id \| ORD-1001 \|/); // renders as order_id, never as emphasis
    assert.match(out, /\| note \| a \\\| b \|/);
});

test("a message without fields or a source leaves them out", () => {
    const out = md.messageMarkdown({
        instance: "acme-dev",
        level: "info",
        message: "x".repeat(md.MAX_MESSAGE + 10),
        fields: {},
    });
    assert.doesNotMatch(out, /Source|\| Field/);
    assert.ok(out.includes("x".repeat(md.MAX_MESSAGE - 1) + "…"));
});
