import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import ts from "typescript";

// the page reads times in the browser's zone: pin one so the expectations do not depend on the machine
process.env.TZ = "Asia/Ho_Chi_Minh";

// The model has no page, store or network in it: it loads in Node as it is.
const output = await mkdtemp(join(tmpdir(), "graylog-admin-inv-"));
const source = await readFile(
    new URL("../src/investigations-model.ts", import.meta.url),
    "utf8",
);
await writeFile(
    join(output, "model.mjs"),
    ts.transpileModule(source, {
        compilerOptions: {
            module: ts.ModuleKind.ESNext,
            target: ts.ScriptTarget.ES2022,
        },
    }).outputText,
);
const m = await import(pathToFileURL(join(output, "model.mjs")).href);

const ev = (seq, type = "step_finished") => ({
    seq,
    type,
    ref: null,
    ts: 0,
    data: {},
});
const step = (id, check, attempt = "a1", state = "succeeded") => ({
    id,
    check,
    attempt,
    state,
    operation: "search_logs",
    requested: {},
    executed: null,
    evidence: [],
});
const item = (kind, id, data) => ({ kind, id, revision: 1, author: "x", data });
const snap = (
    items,
    steps,
    attempts = [{ id: "a1", n: 1, current: true }],
) => ({ items, steps, attempts });

test("frames: complete frames come out, a partial one stays, comments and unknown lines are ignored", () => {
    const got = m.parseFrames(
        ': keep-alive\n\nid: 3\nevent: event\ndata: {"seq":3}\n\nevent: end\ndata: {"cursor":3}\n\nevent: ev',
    );
    assert.deepEqual(got.frames, [
        { event: "event", id: "3", data: '{"seq":3}' },
        { event: "end", id: null, data: '{"cursor":3}' },
    ]);
    assert.equal(got.rest, "event: ev");
    const more = m.parseFrames(got.rest + "ent\ndata: x\n\n");
    assert.deepEqual(more.frames, [{ event: "event", id: null, data: "x" }]);
    assert.equal(
        m.parseFrames("event: a\r\ndata: 1\r\n\r\n").frames[0].data,
        "1",
    );
});

test("advance: each event once and in order; a missing one is a gap, not a guess", () => {
    assert.deepEqual(
        m.advance(2, [ev(4), ev(3)]).fresh.map((e) => e.seq),
        [3, 4],
    );
    assert.equal(m.advance(2, [ev(4), ev(3)]).cursor, 4);
    const again = m.advance(4, [ev(3), ev(4)]);
    assert.deepEqual(
        [again.fresh.length, again.cursor, again.gap],
        [0, 4, false],
    );
    const gap = m.advance(2, [ev(3), ev(5)]);
    assert.deepEqual(
        [gap.fresh.map((e) => e.seq), gap.cursor, gap.gap],
        [[3], 3, true],
    );
    assert.equal(m.advance(0, [ev(2)]).gap, true);
});

test("backoff doubles to a ceiling", () => {
    assert.deepEqual([0, 1, 2, 3, 9].map(m.backoff), [1, 2, 4, 8, 30]);
});

test("states are in words and the ones that want a person are flagged", () => {
    assert.equal(m.stateWord("awaiting_input"), "Needs your input");
    assert.equal(m.stateWord("running"), "Investigating");
    assert.equal(m.stateWord("novel"), "novel");
    assert.deepEqual(
        ["running", "awaiting_input", "interrupted", "completed"].map((state) =>
            m.needsAttention({ state }),
        ),
        [false, true, true, false],
    );
});

test("counts say exact apart from shown", () => {
    assert.equal(
        m.countText({ exact_count: 12345, returned: 20, sampled: true }),
        "12,345 matched · 20 shown",
    );
    assert.equal(
        m.countText({ exact_count: 3, returned: 3, sampled: false }),
        "3 matched",
    );
    assert.equal(
        m.countText({ exact_count: null, returned: 7, sampled: false }),
        "7 shown, total not given",
    );
    assert.equal(
        m.countText({ exact_count: null, returned: null, sampled: false }),
        "no count",
    );
});

test("lease text counts down and says when it ran out", () => {
    const now = Date.parse("2026-11-19T10:00:00Z");
    assert.equal(m.leaseText("2026-11-19T10:09:10Z", now), "10 min left");
    assert.equal(m.leaseText("2026-11-19T09:59:00Z", now), "expired");
    assert.equal(m.leaseText(null, now), "");
});

test("a check sits under its branches, a step without a check is ad hoc", () => {
    const ws = m.workspaceOf(
        snap(
            [
                item("branch", "b1", { question: "q1" }),
                item("branch", "b2", { question: "q2" }),
                item("check", "c1", { purpose: "p", branches: ["b1", "b2"] }),
                item("check", "c2", { purpose: "lone" }),
                item("assessment", "a1", {
                    claim: "c",
                    assessment: "supported",
                    branch: "b1",
                }),
            ],
            [step("s1", "c1"), step("s2", null), step("s3", "gone")],
        ),
    );
    assert.deepEqual(
        ws.branches.map((b) => [b.item.id, b.checks.map((c) => c.item.id)]),
        [
            ["b1", ["c1"]],
            ["b2", ["c1"]],
        ],
    );
    assert.deepEqual(
        ws.branches[0].checks[0].steps.map((s) => s.id),
        ["s1"],
    );
    assert.deepEqual(
        ws.loose.map((c) => c.item.id),
        ["c2"],
    );
    assert.deepEqual(
        ws.adHoc.map((s) => s.id),
        ["s2", "s3"],
    ); // a check that does not exist explains nothing
    assert.deepEqual(
        m.openQuestions(ws).map((b) => b.item.id),
        ["b2"],
    );
});

test("steps are grouped by attempt in the order the attempts began", () => {
    const groups = m.stepsByAttempt(
        snap(
            [],
            [
                step("s1", null, "a2"),
                step("s2", null, "a1"),
                step("s3", null, "a2"),
            ],
            [
                { id: "a1", n: 1 },
                { id: "a2", n: 2 },
            ],
        ),
    );
    assert.deepEqual(
        groups.map((g) => [g.n, g.steps.map((s) => s.id)]),
        [
            [1, ["s2"]],
            [2, ["s1", "s3"]],
        ],
    );
});

test("the query as asked beside the one that ran", () => {
    const e = (requested, executed) => ({ step: { requested, executed } });
    assert.deepEqual(m.queryPair(e({ query: "a" }, { query: "a" })), {
        asked: "a",
        ran: "a",
        differs: false,
    });
    assert.deepEqual(
        m.queryPair(e({ query: "a" }, { effective_query: "(a) AND b" })),
        {
            asked: "a",
            ran: "(a) AND b",
            differs: true,
        },
    );
    assert.deepEqual(m.queryPair({ step: null }), {
        asked: null,
        ran: null,
        differs: false,
    });
});

test("only a search's evidence opens in Logs, with the window that ran", () => {
    const e = (kind, executed) => ({
        kind,
        step: { instance: "main", requested: { query: "x" }, executed },
    });
    assert.deepEqual(
        m.logsViewOf(e("search_logs", { from: "f", to: "t", query: "y" })),
        {
            instance: "main",
            query: "y",
            from: "f",
            to: "t",
            range: undefined,
        },
    );
    assert.equal(m.logsViewOf(e("scan", {})), null);
});

test("the address round-trips and drops trailing parts", () => {
    assert.deepEqual(
        m.selectionPath({ id: null, branch: "b", check: null, evidence: null }),
        [],
    );
    assert.deepEqual(
        m.selectionPath({ id: "i", branch: null, check: null, evidence: null }),
        ["i"],
    );
    assert.deepEqual(
        m.selectionPath({ id: "i", branch: null, check: null, evidence: "e" }),
        ["i", "-", "-", "e"],
    );
    assert.deepEqual(m.selectionOf(["i", "-", "c"]), {
        id: "i",
        branch: null,
        check: "c",
        evidence: null,
    });
    assert.deepEqual(m.selectionOf([]), {
        id: null,
        branch: null,
        check: null,
        evidence: null,
    });
});

test("evidence cited by two records is shared; one cited once is not", () => {
    const shared = m.sharedEvidence(
        snap(
            [
                item("assessment", "a1", { supporting: ["e1", "e2"] }),
                item("assessment", "a2", { contradicting: ["e1"] }),
                item("check_result", "r1", { evidence: ["e2", "e2"] }),
            ],
            [],
        ),
    );
    assert.deepEqual(
        [...shared.entries()],
        [
            ["e1", ["a1", "a2"]],
            ["e2", ["a1", "r1"]],
        ],
    );
});

test("a rate is only shown over a known denominator", () => {
    const got = m.cohortsOf(
        item("comparison", "m1", {
            cohorts: [
                { name: "v1", count: 3, denominator: 100, rate: 0.03 },
                { name: "v2", count: 9 },
                { name: "v3", count: 1, denominator: 50 },
            ],
        }),
    );
    assert.deepEqual(
        got.map((c) => [c.eligible, c.rate]),
        [
            [100, "3.0%"],
            [null, "unknown denominator"],
            [50, ""],
        ],
    );
});

test("a branch with a request waiting is blocked, a received one is not", () => {
    const blocked = m.blockedBranches(
        snap(
            [
                item("evidence_request", "q1", {
                    status: "requested",
                    branch: "b1",
                }),
                item("evidence_request", "q2", {
                    status: "received",
                    branch: "b2",
                }),
                item("evidence_request", "q3", { status: "requested" }),
            ],
            [],
        ),
    );
    assert.deepEqual([...blocked], ["b1"]);
});

const stp = (check, state, evidence = []) => ({
    check,
    state,
    evidence,
    requested: {},
});

test("a check is done when a step gave evidence, failed when every step ended without", () => {
    assert.equal(m.checkState([]), "not_run");
    assert.equal(m.checkState([stp("c", "running")]), "running");
    assert.equal(
        m.checkState([stp("c", "failed"), stp("c", "succeeded")]),
        "done",
    );
    assert.equal(
        m.checkState([stp("c", "failed"), stp("c", "cancelled")]),
        "failed",
    );
    assert.equal(
        m.checkState([stp("c", "failed"), stp("c", "running")]),
        "running",
    );
});

test("evidence of a check's steps is listed once", () => {
    assert.deepEqual(
        m.evidenceOfSteps([
            stp("c", "succeeded", ["e1", "e2"]),
            stp("c", "succeeded", ["e2"]),
        ]),
        ["e1", "e2"],
    );
});

test("an evidence record says what cites it: supports, contradicts, or only looked at it", () => {
    const snap = {
        items: [
            item("assessment", "a1", {
                assessment: "supported",
                evidence: ["e1"],
            }),
            item("assessment", "a2", {
                assessment: "contradicted_in_scope",
                contradicting: ["e1"],
            }),
            item("assessment", "a3", {
                assessment: "undetermined",
                evidence: ["e1"],
            }),
            item("assessment", "a4", {
                assessment: "supported",
                supporting: ["e9"],
            }),
        ],
    };
    const got = m.citedBy(snap, "e1");
    assert.deepEqual(
        got.supports.map((a) => a.id),
        ["a1"],
    );
    assert.deepEqual(
        got.contradicts.map((a) => a.id),
        ["a2"],
    );
    assert.deepEqual(
        got.noted.map((a) => a.id),
        ["a3"],
    );
    assert.deepEqual(
        m.citingAssessments(snap, ["e9"]).map((a) => a.id),
        ["a4"],
    );
});

test("the next thing is the user's question first, else the first check that has not run", () => {
    const base = {
        steps: [stp("c1", "succeeded")],
        items: [
            item("check", "c1", { purpose: "first" }),
            item("check", "c2", { purpose: "second" }),
        ],
    };
    assert.deepEqual(m.nextCheck(base), { kind: "check", text: "second" });
    const asked = {
        ...base,
        items: [
            ...base.items,
            item("evidence_request", "r1", {
                question: "Which deploy?",
                status: "requested",
            }),
        ],
    };
    assert.deepEqual(m.nextCheck(asked), {
        kind: "blocked",
        text: "Which deploy?",
    });
    assert.equal(
        m.nextCheck({
            steps: [stp("c1", "succeeded")],
            items: [base.items[0]],
        }),
        null,
    );
});

test("results are the sentences written for one check", () => {
    const snap = {
        items: [
            item("check_result", "r1", { check: "c1", result: "a" }),
            item("check_result", "r2", { check: "c2", result: "b" }),
        ],
    };
    assert.deepEqual(
        m.resultsOf(snap, "c2").map((r) => r.id),
        ["r2"],
    );
});

test("a report is findings, limits and next checks, or a sentence", () => {
    assert.deepEqual(
        m.reportOf({
            outcome: "cause_undetermined",
            report: { findings: ["a", "b"], limits: ["l"], next_checks: ["n"] },
        }),
        {
            outcome: "cause_undetermined",
            findings: ["a", "b"],
            limits: ["l"],
            next: ["n"],
            text: "",
        },
    );
    assert.equal(m.reportOf({ outcome: "x", report: "plain" }).text, "plain");
    assert.equal(m.reportOf({ report: { summary: "s" } }).text, "s");
    assert.deepEqual(m.reportOf({}).findings, []);
});

// ------------------------------------------------------------------ the Investigations page (ui-spec.md)
const SCOPE = {
    rev: 1,
    from: "2026-11-18T02:30:00.000Z",
    to: "2026-11-18T02:45:00.000Z",
    timezone: "Asia/Ho_Chi_Minh",
};
const timed = (
    id,
    check,
    started,
    ended,
    state = "succeeded",
    evidence = [],
) => ({
    ...step(id, check, "a1", state),
    started,
    ended,
    evidence,
});
const flowSnap = (items, steps, evidence = []) => ({
    ...snap(items, steps),
    scope: SCOPE,
    evidence,
});

test("the window is read in the browser's zone, with the date once when both ends share a day", () => {
    assert.equal(m.windowText(SCOPE), "18 Nov 09:30 → 09:45 GMT+7");
    assert.equal(
        m.windowText({ ...SCOPE, to: "2026-11-19T02:45:00.000Z" }),
        "18 Nov 09:30 → 19 Nov 09:45 GMT+7",
    );
    // a zone this browser does not know reads as UTC, and says so
    assert.equal(m.zoned(SCOPE.from, "Nowhere/Land").zone, "UTC");
    // the browser's zone is the default: the instance's is not used
    assert.match(m.viewZone(), /^Asia\/(Ho_Chi_Minh|Saigon)$/); // the name ICU gives the zone varies
    assert.equal(
        m.windowText({ ...SCOPE, timezone: "America/New_York" }),
        "18 Nov 09:30 → 09:45 GMT+7",
    );
    // a check that ran today shows the time alone; an earlier day shows the day, and the seconds stay in the full text
    const now = Date.parse("2026-11-18T08:00:00Z");
    assert.deepEqual(m.ranAt("2026-11-18T07:12:48Z", now), {
        text: "14:12",
        full: "18 Nov 14:12:48 GMT+7",
    });
    assert.equal(m.ranAt("2026-11-17T07:12:48Z", now).text, "17 Nov 14:12");
    assert.equal(m.ranAt(null, now), null);
    assert.equal(
        m.zoned("2026-11-18 09:46:09.000+07:00", "Asia/Ho_Chi_Minh").seconds,
        "09:46:09",
    );
    assert.equal(m.zoned(null, "UTC"), null);
});

test("evidence is numbered by its place in the investigation, and many tokens fold into +n", () => {
    const s = { evidence: [{ id: "ev_a" }, { id: "ev_b" }] };
    assert.equal(m.evidenceNo(s, "ev_b"), "E-002");
    assert.equal(m.evidenceNo(s, "ev_zz"), "ev_zz");
    assert.deepEqual(m.tokensOf(["a", "b", "c"]), {
        shown: ["a", "b", "c"],
        more: 0,
    });
    assert.deepEqual(m.tokensOf(["a", "b", "c", "d", "e"]), {
        shown: ["a", "b"],
        more: 3,
    });
});

test("ticks are 1, 2 or 5 times a power of ten with at most five intervals, and the axis ends on a tick", () => {
    const at = (end) => m.axisOf(end);
    assert.deepEqual(
        at(300).ticks.map((t) => t.ms),
        [0, 100, 200, 300],
    );
    assert.equal(at(300).ticks[1].label, "100 ms");
    assert.equal(at(270).max, 300);
    assert.equal(at(1000).max, 1000);
    assert.deepEqual(
        at(1000).ticks.map((t) => t.label),
        ["0 s", "0.2 s", "0.4 s", "0.6 s", "0.8 s", "1 s"],
    );
    assert.equal(at(0).max, 1); // everything at one offset: 0 -> 1 ms
    for (const end of [1, 7, 60, 120, 450, 999, 12345, 90000])
        assert.ok(
            at(end).ticks.length - 1 <= 5 &&
                at(end).max >= end &&
                at(end).max - end < at(end).ticks[1].ms,
            `end ${end}`,
        );
});

const svc = (service, first, last, observations, extra = {}) => ({
    service,
    first_ms: first,
    last_ms: last,
    lines: 3,
    observations,
    ...extra,
});
const info = (ms) => ({ ms, level: "info" });
const bad = (ms) => ({ ms, level: "error" });
const three = {
    type: "trace",
    duration_ms: 300,
    truncated: false,
    services: [
        svc("gateway", 0, 300, [info(0), info(300)]),
        svc("checkout", 12, 290, [info(12), bad(290)], { errors: 1 }),
        svc("payment", 35, 270, [info(35), bad(270)], { errors: 1 }),
    ],
    first_error: {
        service: "payment",
        ms: 270,
        message: "Connection pool exhausted",
    },
};

test("a request is one row per service in order of first line, two dots each, the first error ringed with a bubble", () => {
    const v = m.traceView(three, 440);
    assert.deepEqual(
        v.rows.map((r) => r.service),
        ["gateway", "checkout", "payment"],
    );
    assert.deepEqual(
        v.rows.map((r) => r.dots.length),
        [2, 2, 2],
    );
    assert.deepEqual(
        v.rows[2].dots.map((d) => d.error),
        [false, true],
    );
    assert.equal(v.rows[2].bubble, 0.9); // 270 / 300
    assert.equal(v.rows[1].bubble, null); // an error, but not the first of the request
    assert.deepEqual(
        v.rows[2].dots.map((d) => d.label?.text),
        ["35 ms", "270 ms"],
    );
    assert.equal(v.rows[2].height, m.ROW_H + m.BUBBLE_H);
    assert.equal(v.rows[2].top, m.ROW_H * 2);
    assert.equal(
        v.label,
        "3 services over 300 ms; first error in payment at 270 ms",
    );
    assert.deepEqual(v.table[0], {
        service: "gateway",
        offset: "0 ms",
        level: "info",
    });
    assert.equal(v.table.length, 6);
    assert.equal(v.single, false);
    assert.equal(v.hidden, 0);
});

test("one service is one row and one dot; one offset everywhere shows no offsets", () => {
    const one = m.traceView({
        type: "trace",
        duration_ms: 0,
        truncated: false,
        services: [svc("solo", 5, 5, [info(5)])],
    });
    assert.equal(one.single, true);
    assert.equal(one.rows[0].dots.length, 1);
    assert.equal(one.rows[0].dots[0].label, null);
    assert.equal(one.label, "1 service over 5 ms");
    const same = m.traceView({
        type: "trace",
        duration_ms: 0,
        truncated: false,
        services: [svc("a", 0, 0, [info(0)]), svc("b", 0, 0, [info(0)])],
    });
    assert.equal(same.axis.max, 1);
    assert.ok(same.rows.every((r) => r.dots.every((d) => d.label === null)));
});

test("labels that would touch go above their dot, and when that touches too only the ends keep theirs", () => {
    const dot = (ms, x) => ({ ms, x, error: false, count: 1, label: null });
    const close = [dot(10, 0.2), dot(12, 0.21)];
    m.placeLabels(close, 440, false);
    assert.deepEqual(
        close.map((d) => d.label.side),
        ["below", "above"],
    );
    const crowd = [dot(10, 0.2), dot(11, 0.205), dot(12, 0.21), dot(13, 0.215)];
    m.placeLabels(crowd, 440, false);
    assert.deepEqual(
        crowd.map((d) => d.label?.text ?? null),
        ["10 ms", null, null, "13 ms"],
    );
    const apart = [dot(10, 0.1), dot(200, 0.9)];
    m.placeLabels(apart, 440, false);
    assert.deepEqual(
        apart.map((d) => d.label.side),
        ["below", "below"],
    );
});

test("more than three errors in a service are one dot with a count; two lines at one offset are one dot", () => {
    const many = m.traceView({
        type: "trace",
        duration_ms: 50,
        truncated: false,
        services: [
            svc("busy", 0, 50, [info(0), bad(10), bad(20), bad(30), info(50)], {
                errors: 6,
            }),
        ],
    });
    const errors = many.rows[0].dots.filter((d) => d.error);
    assert.equal(errors.length, 1);
    assert.equal(errors[0].count, 6);
    assert.equal(errors[0].label.text, "10 ms ×6");
    const twin = m.traceView({
        type: "trace",
        duration_ms: 9,
        truncated: false,
        services: [svc("t", 9, 9, [info(9), bad(9)], { errors: 1 })],
    });
    assert.deepEqual(
        twin.rows[0].dots.map((d) => d.error),
        [true],
    );
});

test("beyond eight services the ones with errors are drawn first, in order of first line, and the rest are counted", () => {
    const services = Array.from({ length: 11 }, (_, i) =>
        svc(`svc-${String(i).padStart(2, "0")}-with-a-long-name`, i, i + 1, [
            info(i),
            info(i + 1),
        ]),
    );
    services[10] = svc("late-failure", 10, 11, [info(10), bad(11)], {
        errors: 1,
    });
    const v = m.traceView({
        type: "trace",
        duration_ms: 11,
        truncated: true,
        services,
        first_error: { service: "late-failure", ms: 11, message: "x" },
    });
    assert.equal(v.rows.length, 8);
    assert.equal(v.hidden, 3);
    assert.ok(
        v.rows.some((r) => r.service === "late-failure" && r.bubble !== null),
    );
    assert.deepEqual(
        v.rows.map((r) => r.service).slice(0, 2),
        ["svc-00-with-a-long-name", "svc-01-with-a-long-name"].map((x) => x),
    );
    assert.equal(v.rows[0].name, "svc-00-with-a…");
    assert.equal(v.rows[0].name.length, m.NAME_CHARS);
    assert.equal(v.table.length, 22); // the table keeps every service, drawn or not
});

test("the bubble stays inside the plot", () => {
    assert.equal(m.bubbleAt(0.5, 400, 100), 150);
    assert.equal(m.bubbleAt(0.98, 400, 100), 300);
    assert.equal(m.bubbleAt(0.01, 400, 100), 0);
});

test("a record saved without the per-service series has no request chart; a histogram is read back from its excerpts", () => {
    const old = {
        charts: [
            { type: "trace", duration_ms: 300, steps: [{ service: "a" }] },
        ],
    };
    assert.equal(m.chartsOf(old, "trace_request", []).trace, null);
    assert.equal(
        m.chartsOf({ charts: [three] }, "trace_request", []).trace,
        three,
    );
    assert.deepEqual(
        m.chartsOf({}, "search_logs", [
            ["a", 1],
            ["b", 2],
        ]),
        { trace: null, histogram: null },
    );
    const strip = m.chartsOf(
        { peak: { at: "02:30", count: 8 } },
        "log_histogram",
        [
            ["02:25", 4],
            ["02:30", 8],
        ],
    );
    assert.equal(strip.histogram.points.length, 2);
    assert.equal(strip.histogram.peak.at, "02:30");
});

test("saved event charts are ignored outside their evidence kind, including records saved before the fix", () => {
    const histogram = { type: "histogram", points: [["02:25", 4]] };
    const summary = { charts: [three, histogram] };
    assert.deepEqual(m.chartsOf(summary, "root_cause"), {
        trace: null,
        histogram: null,
    });
    assert.deepEqual(m.chartsOf(summary, "trace_request"), {
        trace: three,
        histogram: null,
    });
    assert.deepEqual(m.chartsOf(summary, "log_histogram"), {
        trace: null,
        histogram,
    });
    assert.equal(
        m.chartsOf(
            { charts: [{ type: "histogram", points: [] }] },
            "log_histogram",
        ).histogram,
        null,
    );
});

test("trace excerpts are the error lines, earliest first; with none, the first and last line of a whole request", () => {
    const line = (t, service, level, message) => ({
        t,
        service,
        level,
        message,
        ts: "2026-11-18 09:30:00.000+07:00",
    });
    const all = [
        line("+0ms", "gateway", 6, "in"),
        line("+270ms", "payment", 3, "Connection pool exhausted"),
        line("+290ms", "checkout", "ERROR", "Payment request failed"),
        line("+300ms", "gateway", 6, "out"),
    ];
    const rows = m.traceExcerpts({ excerpts: all, summary: { lines: 4 } });
    assert.deepEqual(
        rows.map((r) => [r.offset, r.service, r.level, r.first]),
        [
            ["+270ms", "payment", "ERROR", true],
            ["+290ms", "checkout", "ERROR", false],
        ],
    );
    const quiet = [
        line("+0ms", "a", 6, "first"),
        line("+5ms", "b", 6, "mid"),
        line("+9ms", "a", 6, "last"),
    ];
    assert.deepEqual(
        m
            .traceExcerpts({ excerpts: quiet, summary: { lines: 3 } })
            .map((r) => r.message),
        ["first", "last"],
    );
    // the record holds only the first lines of a longer request: no pretending the last stored one is the last
    assert.deepEqual(
        m
            .traceExcerpts({ excerpts: quiet, summary: { lines: 90 } })
            .map((r) => r.message),
        ["first", "mid"],
    );
    assert.equal(
        m.traceExcerpts({ excerpts: all, summary: { lines: 4 } }, 1).length,
        1,
    );
    assert.equal(m.levelWord(4), "WARN");
    assert.equal(m.isErrorLevel("fatal"), true);
    assert.equal(m.isErrorLevel("6"), false);
});

test("a request cut by the server's limit says how many lines it shows, and the coverage line is said once", () => {
    assert.equal(
        m.cutText({ kind: "trace_request", exact_count: 640, returned: 200 }),
        "Showing the earliest 200 of 640 lines.",
    );
    assert.equal(
        m.cutText({ kind: "search_logs", exact_count: 2332, returned: 2 }),
        "Showing 2 of 2,332 lines.",
    ); // search pages may be newest first, sorted by another field, or offset into the results
    assert.equal(
        m.cutText({ kind: "search_logs", exact_count: 7, returned: 7 }),
        "",
    );
    assert.equal(
        m.cutText({ kind: "search_logs", exact_count: null, returned: 7 }),
        "",
    );
    assert.equal(
        m.cutText({ kind: "log_histogram", exact_count: 126, returned: 15 }),
        "",
    ); // buckets, not lines
    assert.equal(m.coverageLine("trace_request", true), "");
    assert.match(
        m.coverageLine("trace_request", false),
        /incomplete service coverage possible/,
    );
});

test("the task flow is one column in time order with each node's start, unless branches or overlap say otherwise", () => {
    const items = [
        item("check", "c1", { purpose: "Measure failures" }),
        item("check", "c2", { purpose: "Compare baseline" }),
        item("check", "c3", { purpose: "Not run yet" }),
        item("check_result", "r1", { check: "c1", result: "126 of 4,200" }),
        item("evidence_request", "q1", {
            status: "requested",
            question: "Was there a rollout?",
        }),
    ];
    const steps = [
        timed("s2", "c2", "2026-11-18T02:40:00Z", "2026-11-18T02:40:05Z"),
        timed(
            "s1",
            "c1",
            "2026-11-18T02:31:00Z",
            "2026-11-18T02:31:09Z",
            "succeeded",
            ["ev1"],
        ),
    ];
    const flow = m.flowOf(
        flowSnap(items, steps, [
            {
                id: "ev1",
                kind: "search_logs",
                exact_count: 4200,
                returned: 50,
                sampled: true,
            },
        ]),
    );
    assert.deepEqual(
        flow.rows.map((r) => r.nodes.map((n) => n.check)),
        [["c1"], ["c2"], ["c3"]],
    );
    assert.equal(flow.rows[0].nodes[0].result, "126 of 4,200"); // the agent's sentence wins
    assert.equal(flow.rows[2].nodes[0].state, "not_run");
    assert.equal(flow.asks.length, 1);
    const counted = m.flowOf(
        flowSnap(
            [item("check", "c1", { purpose: "p" })],
            [
                timed(
                    "s1",
                    "c1",
                    "2026-11-18T02:31:00Z",
                    "2026-11-18T02:31:09Z",
                    "succeeded",
                    ["ev1"],
                ),
            ],
            [
                {
                    id: "ev1",
                    kind: "search_logs",
                    exact_count: 4200,
                    returned: 50,
                    sampled: true,
                },
            ],
        ),
    );
    assert.equal(counted.rows[0].nodes[0].result, "4,200 matched · 50 shown");
});

test("with millisecond times, calls that follow each other inside one second stay in a column and overlapping ones sit side by side", () => {
    const steps = [
        timed(
            "s1",
            null,
            "2026-11-18T02:31:00.120Z",
            "2026-11-18T02:31:00.310Z",
        ),
        timed(
            "s2",
            null,
            "2026-11-18T02:31:00.402Z",
            "2026-11-18T02:31:00.690Z",
        ),
        // two calls made together: the second begins before the first has ended
        timed(
            "s3",
            null,
            "2026-11-18T02:31:00.800Z",
            "2026-11-18T02:31:00.950Z",
        ),
        timed(
            "s4",
            null,
            "2026-11-18T02:31:00.812Z",
            "2026-11-18T02:31:00.930Z",
        ),
    ];
    assert.ok(m.flowOf(flowSnap([], steps)).rows.length === 3);
    assert.deepEqual(
        m.flowOf(flowSnap([], steps)).rows.map((r) => r.nodes.length),
        [1, 1, 2],
    );
    assert.equal(
        m.ranAt("2026-11-18T02:31:00.512Z").full,
        "18 Nov 09:31:00 GMT+7",
    );
});

test("checks that began in the same second keep the order their steps were recorded in", () => {
    const items = [
        item("branch", "b1", { question: "Is it the bank?" }),
        item("check", "c1", { purpose: "Count" }),
        item("check", "c2", { purpose: "Histogram", branches: ["b1"] }),
        item("check", "c3", { purpose: "Trace", branches: ["b1"] }),
    ];
    const at = "2026-11-18T02:31:00Z";
    const steps = [
        timed("s1", "c1", at, at),
        timed("s2", "c2", at, at),
        timed("s3", "c3", at, at),
        timed("s4", null, at, at),
    ];
    const rows = m.flowOf(flowSnap(items, steps)).rows;
    assert.deepEqual(
        rows.map((r) => r.nodes.map((n) => n.check ?? n.key)),
        [["c1"], ["c2", "c3"], ["s:s4"]],
    );
});

test("checks of one branch sit side by side, with the branch's question above them", () => {
    const items = [
        item("branch", "b1", { question: "Is it the database?" }),
        item("check", "c1", { purpose: "A", branches: ["b1"] }),
        item("check", "c2", { purpose: "B", branches: ["b1"] }),
        item("check", "c3", { purpose: "C" }),
    ];
    const steps = [
        timed("s1", "c1", "2026-11-18T02:31:00Z", "2026-11-18T02:31:09Z"),
        timed("s2", "c2", "2026-11-18T02:32:00Z", "2026-11-18T02:32:09Z"),
        timed("s3", "c3", "2026-11-18T02:35:00Z", "2026-11-18T02:35:09Z"),
    ];
    const rows = m.flowOf(flowSnap(items, steps)).rows;
    assert.deepEqual(
        rows.map((r) => r.nodes.map((n) => n.check)),
        [["c1", "c2"], ["c3"]],
    );
    assert.equal(rows[0].caption, "Is it the database?");
});

test("without branches, steps that overlapped in time are drawn side by side, at most three, and a step with no check is named by its query", () => {
    const steps = [
        timed("s1", null, "2026-11-18T02:31:00Z", "2026-11-18T02:31:30Z"),
        timed("s2", null, "2026-11-18T02:31:10Z", "2026-11-18T02:31:20Z"),
        timed("s3", null, "2026-11-18T02:31:15Z", "2026-11-18T02:31:25Z"),
        timed("s4", null, "2026-11-18T02:31:16Z", "2026-11-18T02:31:26Z"),
        timed("s5", null, "2026-11-18T02:40:00Z", "2026-11-18T02:40:01Z"),
    ];
    steps[0].requested = { query: "level:3" };
    const rows = m.flowOf(flowSnap([], steps)).rows;
    assert.deepEqual(
        rows.map((r) => r.nodes.length),
        [3, 1, 1],
    );
    assert.equal(rows[0].nodes[0].title, "level:3");
    assert.equal(rows[0].nodes[0].note, "no check recorded");
    assert.equal(rows[0].nodes[0].check, null);
});

test("a running step has no end, and finished checks fold into 'n earlier' only past twelve nodes", () => {
    const run = m.flowOf(
        flowSnap(
            [item("check", "c1", { purpose: "p" })],
            [timed("s1", "c1", "2026-11-18T02:31:00Z", null, "running")],
        ),
    );
    assert.equal(run.rows[0].nodes[0].ended, null);
    assert.equal(run.rows[0].nodes[0].state, "running");
    assert.equal(
        m.elapsedText(
            "2026-11-18T02:31:00Z",
            Date.parse("2026-11-18T02:31:12Z"),
        ),
        "12 s",
    );
    assert.equal(
        m.elapsedText(
            "2026-11-18T02:31:00Z",
            Date.parse("2026-11-18T02:33:05Z"),
        ),
        "2 min 5 s",
    );
    const done = (n) => ({
        key: `k${n}`,
        nodes: [{ key: `k${n}`, state: "done" }],
    });
    const rows = Array.from({ length: 15 }, (_, i) => done(i));
    const fold = m.foldEarlier(rows);
    assert.equal(fold.earlier.length, 3);
    assert.equal(fold.rest.length, 12);
    assert.equal(m.foldEarlier(rows.slice(0, 12)).earlier.length, 0);
    rows[1].nodes[0].state = "failed"; // a failed one stays in view, and nothing after it folds
    assert.equal(m.foldEarlier(rows).earlier.length, 1);
});

test("working explanations carry a state in words and their evidence; an assessed hypothesis is shown once", () => {
    const items = [
        item("assessment", "a1", {
            claim: "Auth failure",
            assessment: "contradicted_in_scope",
            contradicting: ["ev1"],
        }),
        item("assessment", "a2", {
            claim: "Database pressure",
            assessment: "supported",
            evidence: ["ev2", "ev3"],
        }),
        item("hypothesis", "h1", { statement: "database pressure" }),
        item("hypothesis", "h2", { statement: "Deploy regression" }),
    ];
    const got = m.explainedOf({ items });
    assert.deepEqual(
        got.map((x) => [x.text, x.state, x.word]),
        [
            ["Auth failure", "ruled_out", "ruled out in scope"],
            ["Database pressure", "supported", "supported"],
            ["Deploy regression", "open", "checking"],
        ],
    );
    assert.deepEqual(got[1].evidence, ["ev2", "ev3"]);
    assert.deepEqual(m.explainedOf({ items: [] }), []);
});

test("the inspector names the evidence, the window it ran over and the way back to the whole request", () => {
    const e = {
        kind: "trace_request",
        scope_rev: 1,
        step: {
            instance: "checkout/prod",
            requested: { trace_id: "req-7f3a" },
            executed: {
                range: {
                    from: "2026-11-18 09:30:00.000+07:00",
                    to: "2026-11-18 09:45:00.000+07:00",
                },
            },
        },
    };
    const s = {
        scope: SCOPE,
        scope_revisions: [{ rev: 1, from: SCOPE.from, to: SCOPE.to }],
    };
    assert.equal(m.aboutOf(e), "req-7f3a");
    assert.equal(m.aboutOf({ kind: "search_logs", step: null }), "search_logs");
    assert.equal(m.windowOfEvidence(e, s), "18 Nov 09:30 → 09:45 GMT+7");
    assert.equal(
        m.windowOfEvidence({ ...e, step: null }, s),
        "18 Nov 09:30 → 09:45 GMT+7",
    ); // from its scope revision
    const full = m.fullRequestOf(e);
    assert.equal(full.trace, "req-7f3a");
    assert.equal(full.instance, "checkout/prod");
    assert.equal(full.from, "2026-11-18T09:30:00.000+07:00");
    assert.equal(m.fullRequestOf({ ...e, kind: "search_logs" }), null);
});

test("the handoff chips appear only when the record says so; an interruption says why", () => {
    assert.equal(m.analysisComplete({ state: "completed" }), true);
    assert.equal(m.analysisComplete({ state: "running" }), false);
    assert.equal(
        m.fixNotVerified({ state: "completed", verification: "not_requested" }),
        true,
    );
    assert.equal(
        m.fixNotVerified({
            state: "completed",
            verification: "recovery_verified",
        }),
        false,
    );
    assert.equal(
        m.fixNotVerified({ state: "running", verification: "not_requested" }),
        false,
    );
    const s = {
        state: "interrupted",
        attempts: [{ end_reason: null }, { end_reason: "lease_expired" }],
    };
    assert.equal(m.interruptedWhy(s), "lease expired");
    assert.equal(m.interruptedWhy({ ...s, state: "running" }), "");
});

test("a request is asked for by its id, shown as the query that finds it", () => {
    const pair = m.queryPair({
        step: { requested: { trace_id: "req-7f3a" }, executed: null },
    });
    assert.deepEqual(
        [pair.asked, pair.differs],
        ['trace_id:"req-7f3a"', false],
    );
    assert.equal(
        m.queryPair({
            step: {
                requested: { query: "level:3" },
                executed: { effective_query: "level:3 AND x" },
            },
        }).differs,
        true,
    );
    assert.equal(m.queryPair({ step: null }).asked, null);
});
