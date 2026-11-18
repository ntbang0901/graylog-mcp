import { useEffect, useLayoutEffect, useRef } from "preact/hooks";
import { api, errorText } from "../api";
import {
    currentEnv,
    instances,
    label,
    range,
    breakdown,
    breakdownPick,
    breakdownRefresh,
    breakdownTarget,
} from "../store";
import type { Breakdown as BreakdownData } from "../scripts/types";
import { levelOf, openLogs, phrase } from "../logs";
import { growBars } from "../scripts/motion";
import { Skeleton, num, short } from "./UI";
const pct = (v: number | null | undefined) =>
    v == null ? "" : `${v < 10 ? v.toFixed(1) : Math.round(v)}%`;
const lastPart = (v: string) =>
    /^[\w$]+(\.[\w$]+){2,}$/.test(v) ? v.slice(v.lastIndexOf(".") + 1) : v;
const color = (n: number | null) =>
    n === null
        ? "var(--chart-mid)"
        : n <= 3
          ? "var(--color-error)"
          : n === 4
            ? "var(--chart-warn)"
            : n === 5
              ? "var(--chart-mid)"
              : n === 6
                ? "var(--chart-ink)"
                : "var(--color-muted)";
export function Breakdown() {
    const name = breakdownTarget.value,
        r = range.value,
        refresh = breakdownRefresh.value,
        ref = useRef<HTMLElement>(null),
        b = breakdown.value;
    useEffect(() => {
        let live = true;
        breakdown.value = null;
        if (name)
            void api<BreakdownData>(
                `/api/breakdown?instance=${encodeURIComponent(name)}&range=${r}`,
            )
                .then((v) => {
                    if (live) breakdown.value = v;
                })
                .catch((e) => {
                    if (live)
                        breakdown.value = {
                            instance: name,
                            range: r,
                            error: errorText(e),
                        };
                });
        return () => {
            live = false;
        };
    }, [name, r, refresh]);
    useLayoutEffect(() => {
        if (b && ref.current) growBars(ref.current);
    }, [b]);
    const e = instances.value.find((e) => e.name === (b?.instance || name)),
        total = b?.total || 0,
        errs = b?.errors_total || 0,
        svcs = b?.services || [],
        mx = Math.max(1, ...svcs.map((s) => s.count));
    const lv = (b?.levels || [])
            .map((v) => ({ ...v, lv: levelOf(v.value) }))
            .sort((a, b) => (a.lv.n ?? 9) - (b.lv.n ?? 9)),
        sum = lv.reduce((a, v) => a + v.count, 0),
        errLevels = lv
            .filter((v) => v.lv.n !== null && v.lv.n <= 3)
            .reduce((a, v) => a + v.count, 0),
        R = 52,
        C = 2 * Math.PI * R;
    let at = 0;
    const arcs = lv.map((v) => {
        const len = (v.count / sum) * C,
            seg = Math.max(0.8, len - (lv.length > 1 ? 2 : 0)),
            offset = -at;
        at += len;
        return (
            <circle
                class="arc"
                cx="64"
                cy="64"
                r={R}
                fill="none"
                stroke={color(v.lv.n)}
                stroke-width="14"
                stroke-dasharray={`${seg.toFixed(2)} ${(C - seg).toFixed(2)}`}
                stroke-dashoffset={offset.toFixed(2)}
                transform="rotate(-90 64 64)"
                onClick={() => openLevel(v)}
            >
                <title>
                    {v.lv.word}: {num(v.count)}
                </title>
            </circle>
        );
    });
    const base = () => ({
        instance: b?.instance || name!,
        range: r,
        errorQuery: b?.error_query,
        column: b?.service_field,
    });
    function openLevel(v: (typeof lv)[number]) {
        openLogs({
            ...base(),
            title: `Level: ${v.lv.word}`,
            query: `level:${typeof v.value === "number" || /^\d$/.test(String(v.value)) ? v.value : phrase(v.value)}`,
        });
    }
    const groups = b?.error_groups || [],
        gm = Math.max(1, ...groups.map((x) => x.count)),
        rest = errs - groups.reduce((a, x) => a + x.count, 0);
    const allErrors = () =>
        openLogs({
            ...base(),
            title: "Every error line",
            query: "*",
            errorsOnly: true,
        });
    return (
        <section
            class="bd"
            id="bd"
            aria-labelledby="bdTitle"
            ref={ref}
            hidden={!name}
        >
            <div class="bd-head">
                <h2 id="bdTitle">
                    Inside <span id="bdName">{e ? label(e) : ""}</span>
                </h2>
                <span class="muted small" id="bdSub">
                    {!b ? (
                        "reading Graylog…"
                    ) : b.error ? (
                        ""
                    ) : (
                        <>
                            {short(total)} messages ·{" "}
                            <span class={errs ? "bad-text" : ""}>
                                {short(errs)} errors
                            </span>{" "}
                            · last {r === "7d" ? "7 days" : "24 hours"}
                        </>
                    )}
                </span>
                <span class="spacer" />
                <select
                    id="bdEnv"
                    class="bd-env"
                    aria-label="Environment to break down"
                    hidden={!!currentEnv.value || instances.value.length < 2}
                    value={name || ""}
                    onChange={(e) => {
                        breakdownPick.value = e.currentTarget.value;
                    }}
                >
                    {instances.value.map((e) => (
                        <option value={e.name}>{label(e)}</option>
                    ))}
                </select>
                <button
                    class="btn sm"
                    id="bdAll"
                    onClick={() =>
                        openLogs({
                            ...base(),
                            query: "*",
                            title: "Every log line",
                        })
                    }
                >
                    <svg class="i" viewBox="0 0 24 24" aria-hidden="true">
                        <path d="M4 6h16M4 12h16M4 18h10" />
                    </svg>
                    Browse logs
                </button>
            </div>
            <div class="card">
                <div class="card-head">
                    <h3>By service</h3>
                    <span class="muted small" id="svcSub">
                        {b && !b.error ? (
                            <>
                                grouped by{" "}
                                <span class="mono">{b.service_field}</span> ·
                                pick one to read its logs
                            </>
                        ) : !b ? (
                            "pick a service to read its logs"
                        ) : (
                            ""
                        )}
                    </span>
                    <span class="spacer" />
                    <span class="legend bd-legend" aria-hidden="true">
                        <span>
                            <i class="ok" />
                            fine
                        </span>
                        <span>
                            <i class="bad" />
                            errors
                        </span>
                    </span>
                </div>
                <div id="svcList">
                    {!b ? (
                        <Skeleton short />
                    ) : b.error ? (
                        <div class="inline-msg">
                            Could not read {e ? label(e) : b.instance}:{" "}
                            {b.error}
                        </div>
                    ) : !svcs.length ? (
                        <div class="inline-msg">
                            No value of{" "}
                            <span class="mono">{b.service_field}</span> in these
                            logs. Set <span class="mono">service_fields</span>{" "}
                            for this environment on the Log fields screen.
                        </div>
                    ) : (
                        <div class="svcs">
                            {svcs.map((s) => (
                                <div class="svc">
                                    <button
                                        class="svc-name truncate"
                                        title={`Logs of ${s.value}`}
                                        onClick={() =>
                                            openLogs({
                                                ...base(),
                                                title: String(s.value),
                                                query: `${b.service_field}:${phrase(s.value)}`,
                                            })
                                        }
                                    >
                                        {s.value}
                                    </button>
                                    <span
                                        class="track svc-track"
                                        aria-hidden="true"
                                        onClick={() =>
                                            openLogs({
                                                ...base(),
                                                title: String(s.value),
                                                query: `${b.service_field}:${phrase(s.value)}`,
                                            })
                                        }
                                    >
                                        <span
                                            style={{
                                                width: `${Math.max(1.5, (s.count / mx) * 100).toFixed(1)}%`,
                                            }}
                                        >
                                            <i
                                                class="ok"
                                                style={{
                                                    flex: s.count - s.errors,
                                                }}
                                            />
                                            {!!s.errors && (
                                                <i
                                                    class="bad"
                                                    style={{ flex: s.errors }}
                                                />
                                            )}
                                        </span>
                                    </span>
                                    <span class="svc-n">
                                        <b>{num(s.count)}</b>
                                        <small>{pct(s.pct)}</small>
                                    </span>
                                    {s.errors ? (
                                        <button
                                            class="svc-err"
                                            title={`Error lines of ${s.value}`}
                                            onClick={() =>
                                                openLogs({
                                                    ...base(),
                                                    title: String(s.value),
                                                    query: `${b.service_field}:${phrase(s.value)}`,
                                                    errorsOnly: true,
                                                })
                                            }
                                        >
                                            <b>{num(s.errors)}</b> errors{" "}
                                            <small>
                                                {pct(
                                                    (100 * s.errors) / s.count,
                                                )}
                                            </small>
                                        </button>
                                    ) : (
                                        <span class="svc-err none">
                                            no errors
                                        </span>
                                    )}
                                </div>
                            ))}
                        </div>
                    )}
                </div>
            </div>
            <div class="dgrid even">
                <div class="card">
                    <div class="card-head">
                        <h3>By level</h3>
                        <span class="muted small" id="lvlSub">
                            {sum && !b?.error
                                ? "pick a level to read its lines"
                                : ""}
                        </span>
                    </div>
                    <div id="lvlBox">
                        {!b ? (
                            <Skeleton short />
                        ) : b.error ? (
                            <div class="inline-msg">-</div>
                        ) : !sum ? (
                            <div class="inline-msg">
                                These logs have no{" "}
                                <span class="mono">level</span> field.
                            </div>
                        ) : (
                            <div class="lvl">
                                <svg
                                    class="donut"
                                    viewBox="0 0 128 128"
                                    role="img"
                                    aria-label={lv
                                        .map(
                                            (v) =>
                                                `${v.lv.word} ${num(v.count)}`,
                                        )
                                        .join(", ")}
                                >
                                    <circle
                                        cx="64"
                                        cy="64"
                                        r={R}
                                        fill="none"
                                        stroke="var(--color-paper-3)"
                                        stroke-width="14"
                                    />
                                    {arcs}
                                    <text
                                        class="dn-v"
                                        x="64"
                                        y="62"
                                        text-anchor="middle"
                                    >
                                        {short(sum)}
                                    </text>
                                    <text
                                        class="dn-l"
                                        x="64"
                                        y="80"
                                        text-anchor="middle"
                                    >
                                        {errLevels
                                            ? `${pct((100 * errLevels) / sum)} errors`
                                            : "messages"}
                                    </text>
                                </svg>
                                <div class="lvl-rows">
                                    {lv.map((v) => (
                                        <button
                                            class="lvl-row"
                                            onClick={() => openLevel(v)}
                                        >
                                            <i
                                                style={{
                                                    background: color(v.lv.n),
                                                }}
                                            />
                                            <span
                                                class={`lvl-w ${v.lv.cls ? `${v.lv.cls}-text` : ""}`}
                                            >
                                                {v.lv.word}
                                            </span>
                                            <span class="mono muted small">
                                                {v.value}
                                            </span>
                                            <b>{num(v.count)}</b>
                                            <small>{pct(v.pct)}</small>
                                        </button>
                                    ))}
                                </div>
                            </div>
                        )}
                    </div>
                </div>
                <div class="card">
                    <div class="card-head">
                        <h3>What fails</h3>
                        <span class="muted small" id="failSub">
                            {groups.length ? (
                                <>
                                    by{" "}
                                    <span class="mono">{b?.error_field}</span> ·
                                    pick one to read its lines
                                </>
                            ) : (
                                ""
                            )}
                        </span>
                    </div>
                    <div id="failList">
                        {!b ? (
                            <Skeleton short />
                        ) : b.error ? (
                            <div class="inline-msg">-</div>
                        ) : !errs ? (
                            <div class="allclear">
                                // no error in this range
                            </div>
                        ) : !groups.length ? (
                            <div class="inline-msg">
                                {num(errs)} errors, but none has an exception or
                                logger field to group them by.{" "}
                                <button class="btn sm" onClick={allErrors}>
                                    Read the error lines
                                </button>
                            </div>
                        ) : (
                            <>
                                <div class="fails">
                                    {groups.map((x) => (
                                        <button
                                            class="fail-row"
                                            title={x.value}
                                            onClick={() =>
                                                openLogs({
                                                    ...base(),
                                                    title: lastPart(x.value),
                                                    query: `${b.error_field}:${phrase(x.value)}`,
                                                    errorsOnly: true,
                                                })
                                            }
                                        >
                                            <span class="fail-name min0">
                                                <b class="truncate">
                                                    {lastPart(x.value)}
                                                </b>
                                                <small class="truncate">
                                                    {x.sample?.message ||
                                                        x.value}
                                                </small>
                                            </span>
                                            <span
                                                class="track"
                                                aria-hidden="true"
                                            >
                                                <span
                                                    style={{
                                                        width: `${Math.max(2, (x.count / gm) * 100).toFixed(1)}%`,
                                                    }}
                                                />
                                            </span>
                                            <span class="fail-n">
                                                <b>{num(x.count)}</b>
                                                <small>
                                                    {pct(
                                                        (100 * x.count) / errs,
                                                    )}
                                                </small>
                                            </span>
                                        </button>
                                    ))}
                                </div>
                                {rest > 0 && (
                                    <button
                                        class="btn sm ghost fail-rest"
                                        onClick={allErrors}
                                    >
                                        {num(rest)} more errors in other groups
                                        · read every error line
                                    </button>
                                )}
                            </>
                        )}
                    </div>
                </div>
            </div>
        </section>
    );
}
