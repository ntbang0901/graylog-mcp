import { useLayoutEffect, useRef, useState } from "preact/hooks";
import { createPortal } from "preact";
import { currentPage } from "../store";
import { drawChart, growBars } from "../scripts/motion";
import { num, short } from "./UI";
import type { LogStatsEnv } from "../scripts/types";
import type { ComponentChildren } from "preact";
export function spikeIndex(values: number[]) {
    const v = [...values].sort((a, b) => a - b),
        mid =
            v.length % 2
                ? v[(v.length - 1) / 2]
                : (v[v.length / 2 - 1] + v[v.length / 2]) / 2,
        top = Math.max(...v, 0);
    return top > Math.max(mid * 3, mid + 5) ? values.indexOf(top) : -1;
}
function niceMax(v: number) {
    if (v <= 0) return 1;
    const p = 10 ** Math.floor(Math.log10(v)),
        m = v / p;
    return (
        (m <= 1
            ? 1
            : m <= 1.5
              ? 1.5
              : m <= 2
                ? 2
                : m <= 3
                  ? 3
                  : m <= 5
                    ? 5
                    : 10) * p
    );
}
export function overall(envs: LogStatsEnv[], key: "buckets" | "error_buckets") {
    const ok = envs.filter((e) => !e.error && e[key]?.length);
    if (!ok.length) return { labels: [], values: [] };
    const n = Math.max(...ok.map((e) => e[key].length)),
        ref = ok.find((e) => e[key].length === n)![key],
        values: number[] = Array(n).fill(0);
    for (const e of ok)
        e[key].forEach(([, v], j) => {
            values[n - e[key].length + j] += v;
        });
    return {
        labels: ref.map(([l]) => (l.length > 11 ? l.slice(5) : l)),
        values,
    };
}
function Tooltip({
    id,
    tip,
}: {
    id: string;
    tip: {
        x: number;
        y: number;
        title: string;
        rows: [string, string, string?][];
    } | null;
}) {
    const ref = useRef<HTMLDivElement>(null),
        [pos, setPos] = useState({ left: 0, top: 0 });
    useLayoutEffect(() => {
        if (!tip || !ref.current) return;
        const r = ref.current.getBoundingClientRect();
        let x = tip.x + 14,
            y = tip.y - r.height - 12;
        if (x + r.width > innerWidth - 8) x = tip.x - r.width - 14;
        if (y < 8) y = tip.y + 18;
        setPos({ left: x, top: y });
    }, [tip]);
    // Fixed coordinates must stay relative to the viewport during Motion transforms.
    return createPortal(
        <div
            id={id}
            ref={ref}
            class={`tip${tip ? "" : " hidden"}`}
            style={{ left: `${pos.left}px`, top: `${pos.top}px` }}
            role="tooltip"
        >
            <div class="t">{tip?.title}</div>
            {tip?.rows.map(([k, v, color]) => (
                <div class="r">
                    <span>
                        {color && <i style={{ background: color }} />}
                        {k}
                    </span>
                    <b>{v}</b>
                </div>
            ))}
        </div>,
        document.body,
    );
}
export interface Series {
    name: string;
    values: number[];
    color: string;
}
export function Chart({
    id,
    labels,
    values,
    name,
    series,
    height = 210,
    spike = false,
    pick,
    selected = null,
    how = "",
    line = false,
}: {
    id: string;
    labels: string[];
    values?: number[];
    name: string;
    series?: Series[];
    height?: number;
    spike?: boolean;
    pick?: (i: number) => void;
    selected?: number | null;
    how?: string;
    line?: boolean;
}) {
    const ref = useRef<HTMLDivElement>(null),
        svg = useRef<SVGSVGElement>(null),
        [width, setWidth] = useState(720),
        [hover, setHover] = useState(-1),
        [focus, setFocus] = useState(-1),
        [tip, setTip] = useState<Parameters<typeof Tooltip>[0]["tip"]>(null);
    useLayoutEffect(() => {
        const el = ref.current;
        if (!el) return;
        const measure = () => {
            if (el.clientWidth)
                setWidth(Math.max(260, Math.round(el.clientWidth)));
        };
        measure();
        const ro = new ResizeObserver(measure);
        ro.observe(el);
        return () => ro.disconnect();
    }, []);
    const ss = series || [
            { name, values: values || [], color: "var(--chart-ink)" },
        ],
        n = labels.length,
        CW = width,
        H = height,
        padL = 40,
        padB = 20;
    const totals = labels.map((_, i) =>
            ss.reduce((a, s) => a + (s.values[i] || 0), 0),
        ),
        max = niceMax(Math.max(...totals, 1)),
        slot = (CW - padL) / Math.max(1, n),
        step = (CW - padL - 8) / Math.max(1, n - 1),
        bw = Math.max(2, Math.min(24, slot - 3));
    const x = (i: number) =>
            line ? padL + 4 + i * step : padL + i * slot + slot / 2,
        y = (v: number) => H - padB - (v / max) * (H - padB - 12),
        hot = spike ? spikeIndex(totals) : -1,
        peak = totals.indexOf(Math.max(...totals));
    const sig = `${line}|${CW}|${H}|${totals.join()}|${ss.map((s) => s.values.join()).join("|")}`;
    const page = currentPage.value;
    useLayoutEffect(() => {
        setHover(-1);
        setFocus(-1);
        setTip(null);
    }, [page, sig]);
    useLayoutEffect(() => {
        if (ref.current && n) drawChart(ref.current);
    }, [sig]);
    const every = Math.max(
        1,
        Math.ceil(labels.length / Math.max(2, Math.floor(CW / 110))),
    );
    const mark = (i: number, clientX: number, clientY: number) => {
        setHover(i);
        setTip({
            x: clientX,
            y: clientY,
            title: labels[i],
            rows: ss.map((s) => [
                s.name,
                num(s.values[i]),
                ss.length > 1 ? s.color : undefined,
            ]),
        });
    };
    const keyboard = (e: KeyboardEvent) => {
        if (!pick) return;
        let i = focus < 0 ? (selected ?? n - 1) : focus;
        switch (e.key) {
            case "ArrowRight":
                i = Math.min(n - 1, i + 1);
                break;
            case "ArrowLeft":
                i = Math.max(0, i - 1);
                break;
            case "Home":
                i = 0;
                break;
            case "End":
                i = n - 1;
                break;
            case "Enter":
            case " ":
                e.preventDefault();
                pick(i);
                return;
            case "Escape":
                setTip(null);
                setHover(-1);
                if (selected !== null) {
                    e.preventDefault();
                    pick(selected);
                }
                return;
            default:
                return;
        }
        e.preventDefault();
        setFocus(i);
        const r = svg.current!.getBoundingClientRect();
        mark(
            i,
            r.left + (x(i) / CW) * r.width,
            r.top + (line ? (y(totals[i]) / H) * r.height : 24),
        );
    };
    const d = totals
            .map(
                (v, i) =>
                    `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`,
            )
            .join(" "),
        gid = `fill-${id}`;
    const bars: ComponentChildren[] = [];
    if (!line)
        labels.forEach((_, i) => {
            let base = 0;
            ss.forEach((s) => {
                const v = s.values[i] || 0;
                if (!v) return;
                const y0 = y(base),
                    y1 = y(base + v);
                base += v;
                bars.push(
                    <rect
                        class="bar"
                        x={(x(i) - bw / 2).toFixed(1)}
                        y={y1.toFixed(1)}
                        width={bw.toFixed(1)}
                        height={Math.max(1, y0 - y1).toFixed(1)}
                        rx="2"
                        fill={i === hot ? "var(--color-error)" : s.color}
                        stroke="var(--color-paper)"
                        stroke-width="1"
                        opacity={
                            selected !== null && i !== selected
                                ? ".35"
                                : undefined
                        }
                    />,
                );
            });
        });
    // Pointer targets and keyboard bands are JSX nodes; no chart markup or click delegation.
    return (
        <div id={id} ref={ref}>
            {!n ? (
                <div class="inline-msg">No data for this range.</div>
            ) : (
                <>
                    <svg
                        ref={svg}
                        class={`chart${pick ? " pickable" : ""}`}
                        viewBox={`0 0 ${CW} ${H}`}
                        tabIndex={pick ? 0 : undefined}
                        role={pick ? "group" : "img"}
                        aria-describedby={tip ? `${id}Tip` : undefined}
                        aria-label={
                            line
                                ? `${name}: ${num(totals.reduce((a, b) => a + b, 0))} in total, peak ${num(totals[peak])} at ${labels[peak]}${pick ? ". " + how : ""}`
                                : `${name}${pick ? ". " + (how || "Left and right arrows move, Enter filters the list to that bar, Escape clears.") : ""}`
                        }
                        onKeyDown={(e) => keyboard(e)}
                        onBlur={() => {
                            setFocus(-1);
                            setHover(-1);
                            setTip(null);
                        }}
                        onMouseLeave={() => {
                            setHover(-1);
                            setTip(null);
                        }}
                    >
                        {line && (
                            <defs>
                                <linearGradient
                                    id={gid}
                                    x1="0"
                                    x2="0"
                                    y1="0"
                                    y2="1"
                                >
                                    <stop
                                        offset="0"
                                        stop-color="var(--chart-ink)"
                                        stop-opacity=".14"
                                    />
                                    <stop
                                        offset="1"
                                        stop-color="var(--chart-ink)"
                                        stop-opacity="0"
                                    />
                                </linearGradient>
                            </defs>
                        )}
                        {[0, 0.5, 1].map((f) => (
                            <>
                                <line
                                    class="grid"
                                    x1={padL}
                                    x2={CW}
                                    y1={y(max * f)}
                                    y2={y(max * f)}
                                />
                                {!!f && (
                                    <text
                                        class="axis"
                                        x={padL - 6}
                                        y={y(max * f) + 3}
                                        text-anchor="end"
                                    >
                                        {short(max * f)}
                                    </text>
                                )}
                            </>
                        ))}
                        {labels.map((l, i) =>
                            i % every ? null : (
                                <text
                                    class="axis"
                                    x={x(i)}
                                    y={H - 4}
                                    text-anchor="middle"
                                >
                                    {l}
                                </text>
                            ),
                        )}
                        {line ? (
                            <>
                                <path
                                    class="area"
                                    d={`${d} L${x(n - 1)},${y(0)} L${x(0)},${y(0)} Z`}
                                    fill={`url(#${gid})`}
                                />
                                <path class="line" d={d} />
                                {!!totals[peak] && (
                                    <text
                                        class="peak"
                                        x={Math.min(x(peak) + 8, CW - 70)}
                                        y={Math.max(12, y(totals[peak]) - 6)}
                                    >
                                        peak {short(totals[peak])}
                                    </text>
                                )}
                                <line
                                    class="cross"
                                    x1={x(hover)}
                                    x2={x(hover)}
                                    y1="0"
                                    y2={H - padB}
                                    visibility={
                                        hover < 0 ? "hidden" : "visible"
                                    }
                                />
                                <circle
                                    class="dotm"
                                    r="4"
                                    cx={x(hover)}
                                    cy={y(totals[hover] || 0)}
                                    visibility={
                                        hover < 0 ? "hidden" : "visible"
                                    }
                                />
                                <rect
                                    class="hit"
                                    x={padL}
                                    y="0"
                                    width={CW - padL}
                                    height={H - padB}
                                    fill="transparent"
                                    onMouseMove={(e) => {
                                        const r =
                                                e.currentTarget.getBoundingClientRect(),
                                            i = Math.max(
                                                0,
                                                Math.min(
                                                    n - 1,
                                                    Math.round(
                                                        (((e.clientX - r.left) /
                                                            r.width) *
                                                            (CW - padL) -
                                                            4) /
                                                            step,
                                                    ),
                                                ),
                                            );
                                        mark(i, e.clientX, e.clientY);
                                    }}
                                    onClick={() => {
                                        if (pick && hover >= 0) {
                                            setTip(null);
                                            pick(hover);
                                        }
                                    }}
                                />
                            </>
                        ) : (
                            <>
                                {selected !== null && (
                                    <rect
                                        class="selband"
                                        x={padL + selected * slot}
                                        y="0"
                                        width={slot}
                                        height={H - padB}
                                        rx="3"
                                    />
                                )}
                                {bars}
                                {labels.map((_, i) => (
                                    <rect
                                        class={pick ? "pick" : undefined}
                                        x={(padL + i * slot).toFixed(1)}
                                        y="0"
                                        width={slot.toFixed(1)}
                                        height={H - padB}
                                        fill="transparent"
                                        onMouseMove={(e) =>
                                            mark(i, e.clientX, e.clientY)
                                        }
                                        onClick={() => {
                                            setTip(null);
                                            pick?.(i);
                                        }}
                                    />
                                ))}
                                {pick && (
                                    <rect
                                        class="kbband"
                                        x={padL + Math.max(0, focus) * slot}
                                        y="0"
                                        width={slot}
                                        height={H - padB}
                                        rx="3"
                                        visibility={
                                            focus < 0 ? "hidden" : "visible"
                                        }
                                    />
                                )}
                            </>
                        )}
                    </svg>
                    {!line && ss.length > 1 && (
                        <div class="legend">
                            {ss.map((s) => (
                                <span>
                                    <i style={{ background: s.color }} />
                                    {s.name}
                                </span>
                            ))}
                        </div>
                    )}
                </>
            )}
            <Tooltip id={`${id}Tip`} tip={tip} />
        </div>
    );
}
export function Sparkline({
    values,
    w = 96,
    h = 26,
}: {
    values: number[];
    w?: number;
    h?: number;
}) {
    if (!values.length || !values.some(Boolean)) return <span />;
    const mx = Math.max(...values),
        d = values
            .map(
                (v, i) =>
                    `${i ? "L" : "M"}${((i * w) / Math.max(1, values.length - 1)).toFixed(1)},${(h - 2 - (v / mx) * (h - 4)).toFixed(1)}`,
            )
            .join(" ");
    return (
        <svg
            class="spark"
            viewBox={`0 0 ${w} ${h}`}
            width={w}
            height={h}
            aria-hidden="true"
        >
            <path d={d} />
        </svg>
    );
}
export function HBars({
    items,
    empty,
}: {
    items: { label: string; value: number; title?: string }[];
    empty: string;
}) {
    const ref = useRef<HTMLDivElement>(null);
    useLayoutEffect(() => {
        if (ref.current) growBars(ref.current);
    }, [items.map((i) => `${i.label}:${i.value}`).join()]);
    const mx = Math.max(...items.map((i) => i.value), 1);
    return (
        <div ref={ref}>
            {!items.length ? (
                <div class="inline-msg">{empty}</div>
            ) : (
                <div class="hbars">
                    {items.map((i) => (
                        <div class="hb" title={i.title || ""}>
                            <span class="mono truncate">{i.label}</span>
                            <span class="track">
                                <span
                                    style={{
                                        width: `${Math.max(2, (i.value / mx) * 100).toFixed(0)}%`,
                                    }}
                                />
                            </span>
                            <span class="v">{num(i.value)}</span>
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}
