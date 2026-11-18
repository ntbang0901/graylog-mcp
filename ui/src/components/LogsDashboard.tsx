import { useLayoutEffect, useRef } from "preact/hooks";
import type { ComponentChildren } from "preact";
import { navigate } from "../store";
import { phrase } from "../logs";
import {
    fieldOf,
    fmtLimit,
    fmtMs,
    fmtSpan,
    type DashboardAnswer,
    type EndpointRow,
    type ExplorerFields,
} from "../explorer";
import { drill, narrowTo, slow, sort } from "../explorer-store";
import { growBars } from "../scripts/motion";
import { Chart } from "./Charts";
import { num, plural, short, timeAgo } from "./UI";

const FIELD_SETTING: Record<keyof ExplorerFields, string> = {
    service: "service_fields",
    endpoint: "endpoint_fields",
    status: "status_fields",
    latency: "latency_fields",
    trace: "trace_fields",
};
export function FieldsNote({
    fields,
    needs,
}: {
    fields: ExplorerFields;
    needs: (keyof ExplorerFields)[];
}) {
    const missing = needs.filter((f) => !fields[f]);
    if (!missing.length) return null;
    return (
        <div class="inline-msg">
            No {missing.map((f) => `${f} field`).join(", ")} found in these
            logs. Set{" "}
            {missing.map((f, i) => (
                <>
                    {i ? ", " : ""}
                    <span class="mono">{FIELD_SETTING[f]}</span>
                </>
            ))}{" "}
            on{" "}
            <button
                type="button"
                class="lg-link"
                onClick={() => navigate("fields")}
            >
                Log fields
            </button>
            .
        </div>
    );
}

// [class, colour, the range a click keeps]: "other" and "none" are not a range of codes
const STATUS_PARTS: [string, string, string | null][] = [
    ["2xx", "var(--chart-ink)", "[200 TO 299]"],
    ["3xx", "var(--chart-mid)", "[300 TO 399]"],
    ["4xx", "var(--chart-warn)", "[400 TO 499]"],
    ["5xx", "var(--color-error)", "[500 TO 599]"],
    ["1xx", "var(--chart-mid)", "[100 TO 199]"],
    ["other", "var(--chart-error-soft)", null],
    ["none", "var(--color-paper-3)", null],
];
const pct = (v: number) => `${v < 10 ? v.toFixed(1) : Math.round(v)}%`;

function Histogram({ d }: { d: DashboardAnswer }) {
    const step = d.histogram.interval_s * 1000,
        have = new Map(d.histogram.buckets.map((b) => [b.t, b])),
        parse = (s: string) => +new Date(s.replace(" ", "T")),
        from = parse(d.range.from),
        to = parse(d.range.to);
    let buckets: DashboardAnswer["histogram"]["buckets"] = [];
    // empty buckets are filled in so the axis is time; only for a sane number of bars
    if (step > 0 && !isNaN(from) && !isNaN(to) && (to - from) / step <= 400)
        for (let t = Math.floor(from / step) * step; t < to; t += step)
            buckets.push(have.get(t) || { t, count: 0, errors: 0 });
    else buckets = d.histogram.buckets;
    const days = to - from > 86400000,
        label = (t: number) =>
            new Date(t).toLocaleString(
                [],
                days
                    ? { weekday: "short", hour: "2-digit", minute: "2-digit" }
                    : { hour: "2-digit", minute: "2-digit" },
            );
    return (
        <Chart
            id="lgHist"
            name="Messages over time"
            how="Left and right arrows move, Enter narrows the range to that bar."
            height={200}
            labels={buckets.map((b) => label(b.t))}
            series={[
                {
                    name: "Messages",
                    values: buckets.map((b) => Math.max(0, b.count - b.errors)),
                    color: "var(--chart-ink)",
                },
                {
                    name: "Technical errors",
                    values: buckets.map((b) => b.errors),
                    color: "var(--color-error)",
                },
            ]}
            pick={(i) => {
                const b = buckets[i];
                if (b) narrowTo(new Date(b.t), new Date(b.t + step));
            }}
        />
    );
}
function Apps({ d }: { d: DashboardAnswer }) {
    const ref = useRef<HTMLDivElement>(null),
        apps = d.apps || [],
        mx = Math.max(1, ...apps.map((a) => a.count));
    useLayoutEffect(() => {
        if (ref.current) growBars(ref.current);
    }, [apps.map((a) => `${a.app}:${a.count}`).join()]);
    if (!d.apps) return <FieldsNote fields={d.fields} needs={["service"]} />;
    if (!apps.length)
        return <div class="inline-msg">No message in this range.</div>;
    return (
        <div class="hbars lg-apps" ref={ref}>
            {apps.map((a) => {
                const errs = a.business_errors + a.technical_errors;
                return (
                    <div
                        class="hb"
                        title={`${num(a.count)} messages, ${num(a.business_errors)} business errors, ${num(a.technical_errors)} technical errors`}
                    >
                        <button
                            type="button"
                            class="lg-link mono truncate"
                            onClick={() =>
                                d.fields.service &&
                                drill(
                                    `${fieldOf(d.fields.service)}:${phrase(a.app)}`,
                                    "all",
                                )
                            }
                        >
                            {a.app}
                        </button>
                        <span class="track">
                            <span
                                style={{
                                    width: `${Math.max(2, (a.count / mx) * 100).toFixed(0)}%`,
                                }}
                            />
                            {!!errs && (
                                <i
                                    class="lg-err"
                                    style={{
                                        width: `${Math.max(1, (errs / mx) * 100).toFixed(1)}%`,
                                    }}
                                />
                            )}
                        </span>
                        <span class="v">
                            {num(a.count)}
                            {!!errs && (
                                <>
                                    {" "}
                                    <b class="fail-text">{num(errs)}</b>
                                </>
                            )}
                        </span>
                    </div>
                );
            })}
        </div>
    );
}
function Status({ d }: { d: DashboardAnswer }) {
    const st = d.status;
    if (!st) return <FieldsNote fields={d.fields} needs={["status"]} />;
    const parts = STATUS_PARTS.filter(([k]) => st[k]),
        total = parts.reduce((a, [k]) => a + st[k], 0);
    if (!total) return <div class="inline-msg">No message in this range.</div>;
    const R = 52,
        C = 2 * Math.PI * R,
        keep = (range: string | null) => {
            if (range && d.fields.status)
                drill(`${fieldOf(d.fields.status)}:${range}`);
        };
    let at = 0;
    return (
        <div class="lvl">
            <svg
                class="donut"
                viewBox="0 0 128 128"
                role="img"
                aria-label={parts.map(([k]) => `${k} ${num(st[k])}`).join(", ")}
            >
                <circle
                    cx="64"
                    cy="64"
                    r={R}
                    fill="none"
                    stroke="var(--color-paper-3)"
                    stroke-width="14"
                />
                {parts.map(([k, color, range]) => {
                    const len = (st[k] / total) * C,
                        seg = Math.max(0.8, len - (parts.length > 1 ? 2 : 0)),
                        offset = -at;
                    at += len;
                    return (
                        <circle
                            class={range ? "arc" : undefined}
                            cx="64"
                            cy="64"
                            r={R}
                            fill="none"
                            stroke={color}
                            stroke-width="14"
                            stroke-dasharray={`${seg.toFixed(2)} ${(C - seg).toFixed(2)}`}
                            stroke-dashoffset={offset.toFixed(2)}
                            transform="rotate(-90 64 64)"
                            onClick={() => keep(range)}
                        >
                            <title>
                                {k}: {num(st[k])}
                            </title>
                        </circle>
                    );
                })}
                <text class="dn-v" x="64" y="62" text-anchor="middle">
                    {short(total)}
                </text>
                <text class="dn-l" x="64" y="80" text-anchor="middle">
                    messages
                </text>
            </svg>
            <div
                class="lvl-rows"
                role="group"
                aria-label="Show one status class"
            >
                {parts.map(([k, color, range]) => {
                    const body: ComponentChildren = (
                        <>
                            <i style={{ background: color }} />
                            <span class="lvl-w">{k}</span>
                            <span />
                            <b>{num(st[k])}</b>
                            <small class="muted">
                                {pct((100 * st[k]) / total)}
                            </small>
                        </>
                    );
                    return range ? (
                        <button
                            type="button"
                            class="lvl-row"
                            title={`Keep only ${k}`}
                            onClick={() => keep(range)}
                        >
                            {body}
                        </button>
                    ) : (
                        <div class="lvl-row lg-static">{body}</div>
                    );
                })}
            </div>
        </div>
    );
}

type Column = {
    key: string;
    head: string;
    value: (r: EndpointRow) => number | string;
    show?: (v: number) => string;
};
const latency = (r: EndpointRow, p: "p50" | "p95" | "p99") =>
    r.latency_ms?.[p] != null ? Math.round(r.latency_ms[p]!) : -1;
const orDash = (f: (v: number) => string) => (v: number) =>
    v < 0 ? "-" : f(v);
function columns(threshold: number | null, withLatency: boolean): Column[] {
    const all: Column[] = [
        { key: "endpoint", head: "API", value: (r) => r.endpoint },
        { key: "count", head: "Calls", value: (r) => r.count, show: num },
        {
            key: "business_errors",
            head: "Business errors",
            value: (r) => r.business_errors ?? -1,
            show: orDash(num),
        },
        {
            key: "technical_errors",
            head: "Technical errors",
            value: (r) => r.technical_errors,
            show: num,
        },
        {
            key: "error_pct",
            head: "% errors",
            value: (r) => r.error_pct ?? 0,
            show: (v) => `${v}%`,
        },
        ...(["p50", "p95", "p99"] as const).map((p) => ({
            key: p,
            head: p,
            value: (r: EndpointRow) => latency(r, p),
            show: orDash(fmtMs),
        })),
        {
            key: "slow",
            head: `Slow ≥ ${threshold == null ? "-" : fmtLimit(threshold)}`,
            value: (r) => r.slow?.[String(threshold)] ?? -1,
            show: orDash(num),
        },
        {
            key: "last_ts",
            head: "Last",
            value: (r) => r.last_ts || 0,
            show: (v) => (v ? timeAgo(v / 1000) : "-"),
        },
    ];
    return withLatency
        ? all
        : all.filter((c) => !["p50", "p95", "p99", "slow"].includes(c.key));
}
function Endpoints({
    d,
    threshold,
}: {
    d: DashboardAnswer;
    threshold: number | null;
}) {
    const e = d.endpoints!,
        s = sort.value;
    if (!e.rows.length)
        return (
            <div class="allclear">// no message with an API in this range</div>
        );
    const cols = columns(threshold, !!d.fields.latency),
        col = cols.find((c) => c.key === s.key) || cols[1],
        rows = [...e.rows].sort((a, b) => {
            const x = col.value(a),
                y = col.value(b);
            return (
                (typeof x === "string"
                    ? x.localeCompare(String(y))
                    : x - Number(y)) * s.dir || b.count - a.count
            );
        });
    // a count opens the API's messages, narrowed to the kind of error it counts
    const errorOf: Record<string, string | null | undefined> = {
        count: undefined,
        business_errors: d.error_queries.business,
        technical_errors: d.error_queries.technical,
    };
    const open = (api: string, extra?: string | null) => {
        if (d.fields.endpoint)
            drill(
                `${fieldOf(d.fields.endpoint)}:${phrase(api)}${extra ? ` AND (${extra})` : ""}`,
                "all",
            );
    };
    return (
        <>
            <div class="table-wrap">
                <table class="lg-api">
                    <thead>
                        <tr>
                            {cols.map((c, i) => (
                                <th
                                    class={i ? "num" : undefined}
                                    aria-sort={
                                        c.key === col.key
                                            ? s.dir < 0
                                                ? "descending"
                                                : "ascending"
                                            : "none"
                                    }
                                >
                                    <button
                                        type="button"
                                        class="lg-sort"
                                        onClick={() => {
                                            sort.value =
                                                s.key === c.key
                                                    ? {
                                                          key: c.key,
                                                          dir:
                                                              s.dir < 0
                                                                  ? 1
                                                                  : -1,
                                                      }
                                                    : {
                                                          key: c.key,
                                                          dir:
                                                              c.key ===
                                                              "endpoint"
                                                                  ? 1
                                                                  : -1,
                                                      };
                                        }}
                                    >
                                        {c.head}
                                        <span aria-hidden="true">
                                            {c.key === col.key
                                                ? s.dir < 0
                                                    ? " ↓"
                                                    : " ↑"
                                                : ""}
                                        </span>
                                    </button>
                                </th>
                            ))}
                        </tr>
                    </thead>
                    <tbody>
                        {rows.map((r) => (
                            <tr key={r.endpoint}>
                                {cols.map((c, i) => {
                                    if (!i)
                                        return (
                                            <td>
                                                <button
                                                    type="button"
                                                    class="lg-link mono"
                                                    title={`Read the logs of ${r.endpoint}`}
                                                    onClick={() =>
                                                        open(r.endpoint)
                                                    }
                                                >
                                                    {r.endpoint}
                                                </button>
                                            </td>
                                        );
                                    const v = Number(c.value(r)),
                                        shown = c.show ? c.show(v) : String(v),
                                        cls =
                                            (c.key === "technical_errors" ||
                                                c.key === "error_pct") &&
                                            v > 0
                                                ? " bad"
                                                : (c.key ===
                                                        "business_errors" ||
                                                        c.key === "slow") &&
                                                    v > 0
                                                  ? " warn"
                                                  : "",
                                        link =
                                            c.key in errorOf &&
                                            v > 0 &&
                                            (c.key === "count" ||
                                                !!errorOf[c.key]);
                                    return (
                                        <td
                                            class={`num${cls}`}
                                            title={
                                                c.key === "last_ts" && v
                                                    ? new Date(
                                                          v,
                                                      ).toLocaleString()
                                                    : undefined
                                            }
                                        >
                                            {link ? (
                                                <button
                                                    type="button"
                                                    class="lg-link"
                                                    onClick={() =>
                                                        open(
                                                            r.endpoint,
                                                            errorOf[c.key],
                                                        )
                                                    }
                                                >
                                                    {shown}
                                                </button>
                                            ) : (
                                                shown
                                            )}
                                        </td>
                                    );
                                })}
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>
            <div class="cmeta">
                <span>
                    Top {num(e.rows.length)} APIs by calls ·{" "}
                    {num(e.covered ?? 0)} of {plural(d.total, "message")}
                    {e.more ? " · more APIs are not shown" : ""}
                    {e.without_field
                        ? ` · ${num(e.without_field)} without an API`
                        : ""}
                </span>
            </div>
        </>
    );
}
export function Dashboard({ d }: { d: DashboardAnswer }) {
    const f = d.fields,
        threshold = d.slow_ms.includes(slow.value ?? NaN)
            ? slow.value
            : (d.slow_ms[1] ?? d.slow_ms[0] ?? null);
    return (
        <>
            {f.service && (
                <FieldsNote fields={f} needs={["endpoint", "latency"]} />
            )}
            <section class="card">
                <div class="card-head">
                    <h2>Messages</h2>
                    <span class="muted small mono">
                        {plural(d.total, "message")} · per{" "}
                        {fmtSpan(d.histogram.interval_s)} · errors are technical
                        errors
                    </span>
                    <span class="spacer" />
                    <span class="muted small">
                        click a bar to narrow the range to it
                    </span>
                </div>
                <Histogram d={d} />
            </section>
            <div class="dgrid even">
                <section class="card">
                    <div class="card-head">
                        <h2>Applications</h2>
                        <span class="muted small">
                            messages, <b class="fail-text">errors</b> · click
                            one to read its logs
                        </span>
                    </div>
                    <Apps d={d} />
                </section>
                <section class="card">
                    <div class="card-head">
                        <h2>Status</h2>
                        {f.status && (
                            <span class="muted small">
                                <span class="mono">{f.status}</span> · click a
                                class to keep only it
                            </span>
                        )}
                    </div>
                    <Status d={d} />
                </section>
            </div>
            <section class="card">
                <div class="card-head">
                    <h2>By API</h2>
                    {f.endpoint && (
                        <span class="muted small">
                            <span class="mono">{f.endpoint}</span> · click a
                            column to sort, an API or a count to read its logs
                        </span>
                    )}
                    <span class="spacer" />
                    {d.endpoints && f.latency && d.slow_ms.length > 0 && (
                        <>
                            <label class="check small" for="lgSlow">
                                Slow from
                            </label>
                            <select
                                class="sel auto-width"
                                id="lgSlow"
                                value={String(threshold)}
                                onChange={(e) => {
                                    slow.value = Number(e.currentTarget.value);
                                }}
                            >
                                {d.slow_ms.map((t) => (
                                    <option value={String(t)}>
                                        {fmtLimit(t)}
                                    </option>
                                ))}
                            </select>
                        </>
                    )}
                </div>
                {d.endpoints ? (
                    <Endpoints d={d} threshold={threshold} />
                ) : (
                    <FieldsNote fields={f} needs={["endpoint"]} />
                )}
                <p class="muted small">
                    Business errors:{" "}
                    <span class="mono">
                        {d.error_queries.business || "not set"}
                    </span>{" "}
                    · technical errors:{" "}
                    <span class="mono">
                        {d.error_queries.technical || "not set"}
                    </span>
                </p>
            </section>
        </>
    );
}
