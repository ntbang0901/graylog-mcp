// Activity's Dashboard tab: calls per bucket (a bar lists its calls), their outcome, the environments and the tools.
import { useRef, useState } from "preact/hooks";
import { currentEnv, filters, loadErrors, usage } from "../store";
import { setActivityTab } from "../activity";
import type { ToolStat, Usage } from "../scripts/types";
import { Chart, HBars } from "./Charts";
import { fmtMs } from "./ActivityParts";
import { Skeleton, num, short, timeAgo } from "./UI";

const bucketLabel = (t: number, step: number) =>
    step >= 6
        ? new Date(t * 1000).toLocaleString([], {
              weekday: "short",
              hour: "2-digit",
              minute: "2-digit",
          })
        : new Date(t * 1000).toLocaleTimeString([], {
              hour: "2-digit",
              minute: "2-digit",
          });
function listCallsOf(tool: string) {
    filters.value = { ...filters.value, tool };
    setActivityTab("calls");
}

interface Part {
    name: string;
    value: number;
    color: string;
}
function Donut({ parts, total }: { parts: Part[]; total: number }) {
    const R = 42,
        C = 2 * Math.PI * R;
    let at = 0;
    const arcs = parts
        .filter((p) => p.value > 0)
        .map((p) => {
            const len = (C * p.value) / total,
                offset = -at;
            at += len;
            return (
                <circle
                    r={R}
                    cx="60"
                    cy="60"
                    fill="none"
                    stroke={p.color}
                    stroke-width="16"
                    stroke-dasharray={`${len.toFixed(2)} ${(C - len).toFixed(2)}`}
                    stroke-dashoffset={offset.toFixed(2)}
                    transform="rotate(-90 60 60)"
                />
            );
        });
    return (
        <div class="odonut">
            <svg
                viewBox="0 0 120 120"
                width="132"
                height="132"
                role="img"
                aria-label={`${total} calls: ${parts.map((p) => `${p.value} ${p.name.toLowerCase()}`).join(", ")}`}
            >
                <circle
                    r={R}
                    cx="60"
                    cy="60"
                    fill="none"
                    stroke="var(--color-paper-3)"
                    stroke-width="16"
                />
                {arcs}
                <text x="60" y="58" text-anchor="middle" class="dn">
                    {short(total)}
                </text>
                <text x="60" y="74" text-anchor="middle" class="dl">
                    calls
                </text>
            </svg>
            <ul class="dlegend">
                {parts.map((p) => (
                    <li>
                        <i style={{ background: p.color }} />
                        <span>{p.name}</span>
                        <b>{num(p.value)}</b>
                        <span class="muted">
                            {total ? Math.round((100 * p.value) / total) : 0}%
                        </span>
                    </li>
                ))}
            </ul>
        </div>
    );
}

// [key, heading, value of a row, how to show it]
type Col = [
    string,
    string,
    (t: ToolStat) => number | string,
    (v: number | string) => string,
];
const count = (v: number | string) => num(v);
const msOrDash = (v: number | string) => (v ? fmtMs(Number(v)) : "-");
function toolCols(slowMs: string): Col[] {
    return [
        ["tool", "Tool", (t) => t.tool, String],
        ["calls", "Calls", (t) => t.calls, count],
        ["misses", "Found nothing", (t) => t.misses, count],
        ["errors", "Errors", (t) => t.errors, count],
        [
            "err_pct",
            "% errors",
            (t) => (t.calls ? t.errors / t.calls : 0),
            (v) => `${Math.round(100 * Number(v))}%`,
        ],
        ["truncated", "Cut", (t) => t.truncated || 0, count],
        ["p50_ms", "Median", (t) => t.p50_ms, msOrDash],
        ["p95_ms", "p95", (t) => t.p95_ms, msOrDash],
        ["slow", "Slow", (t) => t.slow?.[slowMs] || 0, count],
        [
            "last_ts",
            "Last",
            (t) => t.last_ts || 0,
            (v) => (v ? timeAgo(Number(v)) : "-"),
        ],
    ];
}
const SLOW = [
    ["1000", "1 s"],
    ["3000", "3 s"],
    ["10000", "10 s"],
] as const;
function ToolTable({ u }: { u: Usage | null }) {
    // the failing tools first
    const [sort, setSort] = useState({ key: "errors", dir: -1 }),
        [slowMs, setSlowMs] = useState("3000");
    const cols = toolCols(slowMs),
        col = cols.find((c) => c[0] === sort.key) || cols[3],
        rows = [...(u?.tools || [])].sort((a, b) => {
            const x = col[2](a),
                y = col[2](b);
            return (
                (typeof x === "string"
                    ? x.localeCompare(String(y))
                    : x - Number(y)) * sort.dir || b.calls - a.calls
            );
        }),
        slowText = SLOW.find(([v]) => v === slowMs)?.[1] || "";
    return (
        <section class="card" aria-labelledby="toolsTitle">
            <div class="card-head">
                <h2 id="toolsTitle">Tools</h2>
                <span class="muted small" id="toolsSub">
                    click a column to sort, a tool to list its calls
                </span>
                <span class="spacer" />
                <label class="check small" for="slowMs">
                    Slow from
                </label>
                <select
                    class="sel auto-width"
                    id="slowMs"
                    value={slowMs}
                    onChange={(e) => setSlowMs(e.currentTarget.value)}
                >
                    {SLOW.map(([v, text]) => (
                        <option value={v}>{text}</option>
                    ))}
                </select>
            </div>
            <div id="toolTable">
                {!u ? (
                    <Skeleton short />
                ) : !rows.length ? (
                    <div class="allclear">
                        // no tool called in this range yet
                    </div>
                ) : (
                    <div class="table-wrap">
                        <table class="ttable">
                            <thead>
                                <tr>
                                    {cols.map(([k, heading], i) => (
                                        <th
                                            key={k}
                                            class={i ? "num" : undefined}
                                            aria-sort={
                                                k === sort.key
                                                    ? sort.dir < 0
                                                        ? "descending"
                                                        : "ascending"
                                                    : "none"
                                            }
                                        >
                                            <button
                                                type="button"
                                                class="sorth"
                                                onClick={() =>
                                                    setSort(
                                                        sort.key === k
                                                            ? {
                                                                  key: k,
                                                                  dir: -sort.dir,
                                                              }
                                                            : {
                                                                  key: k,
                                                                  dir:
                                                                      k ===
                                                                      "tool"
                                                                          ? 1
                                                                          : -1,
                                                              },
                                                    )
                                                }
                                            >
                                                {heading}
                                                {k === "slow"
                                                    ? ` ≥ ${slowText}`
                                                    : ""}
                                                <span aria-hidden="true">
                                                    {k === sort.key
                                                        ? sort.dir < 0
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
                                {rows.map((t) => (
                                    <tr key={t.tool}>
                                        {cols.map(([k, , value, show], i) => {
                                            const v = value(t);
                                            if (!i)
                                                return (
                                                    <td>
                                                        <button
                                                            type="button"
                                                            class="linkbtn tn"
                                                            title={`List the calls of ${t.tool}`}
                                                            onClick={() =>
                                                                listCallsOf(
                                                                    t.tool,
                                                                )
                                                            }
                                                        >
                                                            {t.tool}
                                                        </button>
                                                    </td>
                                                );
                                            const on = Number(v) > 0,
                                                bad =
                                                    on &&
                                                    (k === "errors" ||
                                                        k === "err_pct"),
                                                warn =
                                                    on &&
                                                    (k === "misses" ||
                                                        k === "slow" ||
                                                        k === "truncated");
                                            return (
                                                <td
                                                    class={`num${bad ? " bad" : warn ? " warn" : ""}`}
                                                    title={
                                                        k === "last_ts" && on
                                                            ? new Date(
                                                                  Number(v) *
                                                                      1000,
                                                              ).toLocaleString()
                                                            : undefined
                                                    }
                                                >
                                                    {show(v)}
                                                </td>
                                            );
                                        })}
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                )}
            </div>
        </section>
    );
}

export function ActivityDashboard() {
    const u = usage.value,
        f = filters.value,
        // the chart's keyboard picks a bar to look at it; a click on one goes to its calls
        byKey = useRef(false);
    const pick = (i: number) => {
        const bucket = filters.value.bucket === i ? null : i;
        filters.value = { ...filters.value, bucket };
        if (bucket !== null && !byKey.current) setActivityTab("calls");
        byKey.current = false;
    };
    const t = u?.totals,
        found = t ? Math.max(0, t.calls - t.misses - t.errors) : 0;
    return (
        <>
            <section class="card" aria-labelledby="actChartTitle">
                <div class="card-head">
                    <h2 id="actChartTitle">Calls</h2>
                    <span class="muted small mono" id="callsSub">
                        {u &&
                            `${num(u.totals.calls)} calls · ${num(u.totals.misses)} found nothing · ${num(u.totals.errors)} errors · per ${u.bucket_hours === 1 ? "hour" : `${u.bucket_hours} hours`}${currentEnv.value ? " · every environment" : ""}`}
                    </span>
                    <span class="spacer" />
                    <span class="muted small">
                        click a bar to list its calls
                    </span>
                </div>
                {!u ? (
                    <div id="callsChart">
                        {loadErrors.value.usage ? (
                            <div class="inline-msg">
                                Could not load the calls:{" "}
                                {loadErrors.value.usage}. Press Refresh to try
                                again.
                            </div>
                        ) : (
                            <Skeleton />
                        )}
                    </div>
                ) : !u.totals.calls ? (
                    <div id="callsChart">
                        <div class="inline-msg">
                            {u.enabled
                                ? "No tool calls in this range yet. They show up here as soon as Claude uses graylog."
                                : "Nothing is recorded."}
                        </div>
                    </div>
                ) : (
                    <div
                        onKeyDownCapture={(e) => {
                            byKey.current = e.key === "Enter" || e.key === " ";
                        }}
                    >
                        <Chart
                            id="callsChart"
                            labels={u.buckets.map((b) =>
                                bucketLabel(b.t, u.bucket_hours),
                            )}
                            name="Tool calls"
                            height={220}
                            series={[
                                {
                                    name: "Found something",
                                    values: u.buckets.map(
                                        (b) => b.calls - b.misses - b.errors,
                                    ),
                                    color: "var(--chart-ink)",
                                },
                                {
                                    name: "Found nothing",
                                    values: u.buckets.map((b) => b.misses),
                                    color: "var(--chart-warn)",
                                },
                                {
                                    name: "Error",
                                    values: u.buckets.map((b) => b.errors),
                                    color: "var(--color-error)",
                                },
                            ]}
                            selected={f.bucket}
                            pick={pick}
                        />
                    </div>
                )}
            </section>
            <div class="dgrid even">
                <section class="card" aria-labelledby="outcomeTitle">
                    <div class="card-head">
                        <h2 id="outcomeTitle">Outcome</h2>
                        <span class="muted small">every call in the range</span>
                    </div>
                    <div id="actDonut">
                        {!t ? (
                            <Skeleton short />
                        ) : t.calls ? (
                            <Donut
                                total={t.calls}
                                parts={[
                                    {
                                        name: "Found something",
                                        value: found,
                                        color: "var(--chart-ink)",
                                    },
                                    {
                                        name: "Found nothing",
                                        value: t.misses,
                                        color: "var(--chart-warn)",
                                    },
                                    {
                                        name: "Error",
                                        value: t.errors,
                                        color: "var(--color-error)",
                                    },
                                ]}
                            />
                        ) : (
                            <div class="inline-msg">
                                No call in this range yet.
                            </div>
                        )}
                    </div>
                </section>
                <section class="card" aria-labelledby="envCallsTitle">
                    <div class="card-head">
                        <h2 id="envCallsTitle">Environments</h2>
                        <span class="muted small">calls per environment</span>
                    </div>
                    <div id="actEnvs">
                        {!u ? (
                            <Skeleton short />
                        ) : (
                            <HBars
                                items={(u.instances || []).map((i) => ({
                                    label: i.instance || "(default)",
                                    value: i.calls,
                                    title: `${i.calls} calls, ${i.misses} found nothing, ${i.errors} errors`,
                                }))}
                                empty="No call in this range yet."
                            />
                        )}
                    </div>
                </section>
            </div>
            <ToolTable u={u} />
        </>
    );
}
