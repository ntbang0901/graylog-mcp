// The two pictures of the evidence inspector, drawn as SVG from the series the server kept with the record: the
// request observations (dots on a time axis, one row per service) and a slim strip for a histogram. No chart library;
// every colour is a class, so both themes work.
import {
    BUBBLE_H,
    BUBBLE_TEXT,
    bubbleAt,
    traceView,
    type HistogramChart,
    type TraceChart,
} from "../investigations-model";

const W = 560,
    LABEL = 104,
    RIGHT = 16,
    PLOT = W - LABEL - RIGHT,
    AXIS_H = 22,
    BUBBLE_W = BUBBLE_TEXT.length * 6 + 16;
const clamp = (v: number, lo: number, hi: number) =>
    Math.min(Math.max(v, lo), Math.max(lo, hi));

// what a reader needs to know about the picture, always shown; the rest only when it is true
export const LEGEND =
    "Dots = log observations. Log-derived ordering; service coverage may be incomplete.";

export function RequestObservations({
    chart,
    cut,
}: {
    chart: TraceChart;
    cut: string;
}) {
    const v = traceView(chart, PLOT),
        H = AXIS_H + v.height + 4,
        gx = (x: number) => LABEL + x * PLOT;
    return (
        <figure class="inv-chart">
            <svg
                class="inv-svg"
                viewBox={`0 0 ${W} ${H}`}
                role="img"
                aria-label={v.label}
            >
                {v.axis.ticks.map((t, i) => (
                    <g>
                        <line
                            class="grid"
                            x1={gx(t.x)}
                            x2={gx(t.x)}
                            y1={AXIS_H - 4}
                            y2={H}
                        />
                        <text
                            class={`tick${i % 2 ? " alt" : ""}`}
                            x={gx(t.x)}
                            y="12"
                            text-anchor={
                                i === 0
                                    ? "start"
                                    : i === v.axis.ticks.length - 1
                                      ? "end"
                                      : "middle"
                            }
                        >
                            {t.label}
                        </text>
                    </g>
                ))}
                {v.rows.map((r) => {
                    const cy =
                        AXIS_H +
                        r.top +
                        (r.bubble !== null ? BUBBLE_H : 0) +
                        20;
                    return (
                        <g>
                            <polygon
                                class="hex"
                                points="5,0 10,3 10,9 5,12 0,9 0,3"
                                transform={`translate(0 ${cy - 6})`}
                            />
                            <text class="lane" x="16" y={cy + 3}>
                                {r.name}
                                <title>{r.service}</title>
                            </text>
                            <line
                                class="span"
                                x1={gx(r.first)}
                                x2={gx(r.last)}
                                y1={cy}
                                y2={cy}
                            />
                            {r.dots.map((d) => {
                                const x = gx(d.x);
                                return (
                                    <g>
                                        {d.error ? (
                                            <g class="obs err">
                                                <circle cx={x} cy={cy} r="6" />
                                                <circle
                                                    cx={x}
                                                    cy={cy}
                                                    r="2.6"
                                                />
                                            </g>
                                        ) : (
                                            <circle
                                                class="obs"
                                                cx={x}
                                                cy={cy}
                                                r="3.5"
                                            />
                                        )}
                                        {d.label && (
                                            <text
                                                class="tick"
                                                x={
                                                    LABEL +
                                                    clamp(
                                                        d.x * PLOT -
                                                            (d.label.text
                                                                .length *
                                                                6) /
                                                                2,
                                                        0,
                                                        PLOT -
                                                            d.label.text
                                                                .length *
                                                                6,
                                                    )
                                                }
                                                y={
                                                    d.label.side === "below"
                                                        ? cy + 18
                                                        : cy - 10
                                                }
                                            >
                                                {d.label.text}
                                            </text>
                                        )}
                                    </g>
                                );
                            })}
                            {r.bubble !== null && (
                                <g class="bubble">
                                    <rect
                                        x={
                                            LABEL +
                                            bubbleAt(r.bubble, PLOT, BUBBLE_W)
                                        }
                                        y={AXIS_H + r.top + 6}
                                        width={BUBBLE_W}
                                        height="16"
                                        rx="4"
                                    />
                                    <text
                                        x={
                                            LABEL +
                                            bubbleAt(r.bubble, PLOT, BUBBLE_W) +
                                            BUBBLE_W / 2
                                        }
                                        y={AXIS_H + r.top + 17.5}
                                        text-anchor="middle"
                                    >
                                        {BUBBLE_TEXT}
                                    </text>
                                    <polygon
                                        points={`${gx(r.bubble) - 4},${AXIS_H + r.top + 22} ${gx(r.bubble) + 4},${AXIS_H + r.top + 22} ${gx(r.bubble)},${AXIS_H + r.top + 28}`}
                                    />
                                </g>
                            )}
                        </g>
                    );
                })}
            </svg>
            <figcaption class="inv-legend">
                <span class="inv-legend-dot" aria-hidden="true" /> {LEGEND}
                {v.single && " Only one service logged this request."}
                {v.hidden > 0 && ` +${v.hidden} more services.`}
                {chart.truncated && " Some services are left out."}
                {cut && ` ${cut}`}
            </figcaption>
            <div
                class="inv-sr"
                role="region"
                aria-label="Observations as a table"
                tabIndex={0}
            >
                <table>
                    <caption>{v.label}</caption>
                    <thead>
                        <tr>
                            <th>Service</th>
                            <th>Offset</th>
                            <th>Level</th>
                        </tr>
                    </thead>
                    <tbody>
                        {v.table.map((o) => (
                            <tr>
                                <td>{o.service}</td>
                                <td>{o.offset}</td>
                                <td>{o.level}</td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>
        </figure>
    );
}

// bars of the buckets with the peak marked: the one thing a count cannot show
export function HistogramStrip({ chart }: { chart: HistogramChart }) {
    const n = chart.points.length,
        H = 56,
        slot = W / n,
        bar = Math.max(1, slot - Math.min(2, slot * 0.25)),
        max = Math.max(1, ...chart.points.map((p) => p[1])),
        peak = chart.peak;
    return (
        <figure class="inv-chart inv-strip">
            <svg
                class="inv-svg"
                viewBox={`0 0 ${W} ${H}`}
                preserveAspectRatio="none"
                role="img"
                aria-label={`${chart.source_buckets ?? n} buckets${chart.interval ? ` of ${chart.interval}` : ""}${chart.source_buckets ? `; ${n} bars show the maximum in each group` : ""}${peak ? `; peak ${peak.count ?? "?"} at ${peak.at}` : ""}`}
            >
                {chart.points.map(([label, v], i) => {
                    const h = Math.max(1, (v / max) * (H - 4));
                    return (
                        <rect
                            class={`bar${label === peak?.at ? " peak" : ""}`}
                            x={i * slot}
                            y={H - h}
                            width={bar}
                            height={h}
                        >
                            <title>{`${label}: ${v}`}</title>
                        </rect>
                    );
                })}
            </svg>
            <figcaption class="inv-legend">
                {chart.extent?.[0] ?? chart.points[0][0]} →{" "}
                {chart.extent?.[1] ?? chart.points[n - 1][0]}
                {chart.source_buckets &&
                    ` · ${chart.source_buckets} buckets in ${n} bars (maximum per group)`}
                {peak && (
                    <>
                        {" "}
                        · peak {peak.count ?? "?"} at {peak.at}
                    </>
                )}
            </figcaption>
        </figure>
    );
}
