import { useEffect, useLayoutEffect, useRef, useState } from "preact/hooks";
import type { ComponentChildren } from "preact";
import {
    checks,
    context,
    currentEnv,
    envHealth,
    instances,
    label,
    loadDashboard,
    loadErrors,
    loadSetup,
    openEnv,
    openEnvByName,
    openCalls,
    openProblems,
    range,
    rerun,
    runDoctor,
    setup,
    state,
    stats,
    statsAt,
    usage,
    byGroup,
    navigate,
    breakdownRefresh,
    breakdownTarget,
} from "../store";
import { finishSetup, setupStep, setupSteps, setupUpdate } from "../setup";
import { Breakdown } from "./Breakdown";
import { openLogs } from "../logs";
import { countTo, REDUCED } from "../scripts/motion";
import {
    AsyncButton,
    ContextChip,
    Icon,
    Page,
    Range,
    Refresh,
    Skeleton,
    StateTag,
    num,
    plural,
    short,
    timeAgo,
} from "./UI";
import { Chart, HBars, Sparkline, overall, spikeIndex } from "./Charts";
import type { Call } from "../scripts/types";
export const callWhat = (c: Call) => {
    const a = c.args || {};
    return String(
        a.query ||
            a.trace_id ||
            a.field ||
            a.ref ||
            a.contains ||
            a.group_by ||
            "-",
    );
};
interface Attn {
    id: string;
    kind: string;
    subject: string;
    why: ComponentChildren;
    small?: ComponentChildren;
    action: string;
    run: () => unknown;
}
function attentionItems(): Attn[] {
    const items: Attn[] = [];
    for (const e of instances.value.filter(
        (e) => !currentEnv.value || e.name === currentEnv.value,
    )) {
        const h = envHealth(e),
            p = h.problems[0],
            kind = e.auth === "basic" ? "password" : "token",
            run = () => (e.local ? openEnv(e) : navigate("config"));
        if (!e.secret_set && e.local)
            items.push({
                id: `env:${e.name}`,
                kind: "fail",
                subject: label(e),
                why: `${kind} missing`,
                small: "Saved on this machine only, never in a repository.",
                action: `Add ${kind}`,
                run,
            });
        else if (p)
            items.push({
                id: `env:${e.name}`,
                kind: h.cls,
                subject: label(e),
                why: `${h.cls === "fail" ? "failing" : "warning"} · ${p.detail}`,
                small: p.fix,
                action: e.local ? "Edit" : "Open config",
                run,
            });
    }
    for (const c of checks.value || [])
        if (!c.instance && (c.status === "fail" || c.status === "warn"))
            items.push({
                id: `check:${c.name}`,
                kind: c.status,
                subject: c.name,
                why: c.detail,
                small: c.fix,
                action: "Check again",
                run: runDoctor,
            });
    if (loadErrors.value.setup)
        items.push({
            id: "setup:error",
            kind: "warn",
            subject: "setup",
            why: "Could not read the setup state",
            small: loadErrors.value.setup,
            action: "Retry",
            run: () => loadSetup(),
        });
    for (const step of setupSteps()) {
        if (step.ok || (step.key === "env" && instances.value.length)) continue;
        items.push({
            id: `setup:${step.key}`,
            kind: "warn",
            subject: "setup",
            why: step.what,
            small: step.why,
            action:
                step.key === "env"
                    ? "Add environment"
                    : step.auto
                      ? {
                            server: "Start",
                            autostart: "Turn on",
                            claude: "Connect",
                        }[step.key] || "Connect"
                      : "Copy command",
            run:
                step.key === "env"
                    ? () => openEnv(null)
                    : step.auto
                      ? () => setupStep(step.key)
                      : () =>
                            navigator.clipboard.writeText(
                                setup.value!.claude.command,
                            ),
        });
    }
    if (setup.value?.update?.available)
        items.push({
            id: "update",
            kind: "warn",
            subject: "update",
            why: "A newer version is available",
            small: `running ${setup.value.install.label || "v" + setup.value.install.version}`,
            action: "Update",
            run: setupUpdate,
        });
    const u = usage.value;
    if (u?.enabled) {
        const misses = currentEnv.value
                ? u.misses.filter((m) => m.instance === currentEnv.value)
                : u.misses,
            n = currentEnv.value ? misses.length : u.totals.misses,
            counts: Record<string, number> = {};
        for (const c of misses) {
            const w = callWhat(c);
            if (w !== "-") counts[w] = (counts[w] || 0) + 1;
        }
        const top = Object.entries(counts).sort((a, b) => b[1] - a[1])[0]?.[0];
        if (n)
            items.push({
                id: "misses",
                kind: "warn",
                subject: plural(n, "search", "searches"),
                why: "found nothing",
                small: top ? (
                    <>
                        most often <span class="mono">{top}</span>
                    </>
                ) : undefined,
                action: "See in Activity",
                run: openProblems,
            });
    }
    return items.sort(
        (a, b) => Number(b.kind === "fail") - Number(a.kind === "fail"),
    );
}
function AttentionRows({ items }: { items: Attn[] }) {
    const [rows, setRows] = useState(
        items.map((item) => ({ item, gone: false })),
    );
    useLayoutEffect(() => {
        const live = new Set(items.map((item) => item.id));
        setRows((previous) => [
            ...items.map((item) => ({ item, gone: false })),
            ...previous
                .filter((row) => !live.has(row.item.id))
                .map((row) => ({ ...row, gone: true })),
        ]);
        const timer = setTimeout(
            () => setRows((previous) => previous.filter((row) => !row.gone)),
            REDUCED.matches ? 0 : 340,
        );
        return () => clearTimeout(timer);
    }, [items]);
    return (
        <>
            {rows.map(({ item: a, gone }) => (
                <div
                    class={`arow${gone ? " gone" : ""}`}
                    key={a.id}
                    inert={gone}
                >
                    <div class="clip">
                        <div class="aitem">
                            <span class={`glyph ${a.kind}`} aria-hidden="true">
                                {a.kind === "fail" ? "✕" : "!"}
                            </span>
                            <span class="subj">{a.subject}</span>
                            <span class="why">
                                {a.why}
                                {a.small && <small>{a.small}</small>}
                            </span>
                            <AsyncButton
                                class={`btn${a.kind === "fail" ? " primary" : ""}`}
                                run={a.run}
                            >
                                {a.action}
                            </AsyncButton>
                        </div>
                    </div>
                </div>
            ))}
        </>
    );
}
function Figure({
    to,
    format,
    placeholder,
    index,
}: {
    to: number | null;
    format: (n: number) => string;
    placeholder: string;
    index: number;
}) {
    const [value, setValue] = useState(placeholder),
        counted = useRef<string | null>(null),
        r = range.value;
    useLayoutEffect(() => {
        if (to === null) {
            setValue(placeholder);
            return;
        }
        const final = format(to);
        if (counted.current === r || REDUCED.matches) {
            setValue(final);
            return;
        }
        counted.current = r;
        const anim = countTo(
            to,
            (n) => setValue(format(Math.round(n))),
            () => setValue(final),
        );
        return () => anim.stop();
    }, [to, r, placeholder]);
    return <div class="kv">{to === null ? placeholder : value}</div>;
}
export function Overview() {
    const u = usage.value,
        all = instances.value,
        ctx = context.value,
        r = range.value,
        st = stats.value,
        envs = st
            ? ctx
                ? st.environments.filter((e) => e.name === ctx.name)
                : st.environments
            : null;
    useEffect(() => {
        if (state.value) void loadDashboard();
    }, [r, all.map((e) => e.name).join("|")]);
    const vol = overall(envs || [], "buckets"),
        errs = overall(envs || [], "error_buckets"),
        hot = spikeIndex(errs.values),
        tot = envs ? envs.reduce((a, e) => a + (e.total || 0), 0) : null,
        errors = envs ? envs.reduce((a, e) => a + (e.errors || 0), 0) : null,
        down = envs?.filter((e) => e.error).length || 0,
        step = st?.bucket_hours || (r === "7d" ? 6 : 1),
        noEnv = !all.length;
    const before = `vs the ${r === "7d" ? "7 days" : "24 hours"} before`,
        scope = ctx ? " · all envs" : "",
        calls = u?.totals.calls || 0,
        diff = u ? calls - u.previous.calls : 0,
        hit = u?.totals.hit_rate ?? null,
        hitDiff =
            hit !== null && u?.previous.hit_rate != null
                ? hit - u.previous.hit_rate
                : null;
    const target = () => currentEnv.value || breakdownTarget.value;
    const showLogs = (onlyErrors: boolean) => {
        const instance = target();
        if (instance)
            openLogs({
                instance,
                range: r,
                query: "*",
                title: onlyErrors ? "Every error line" : "Every log line",
                errorsOnly: onlyErrors,
            });
    };
    const openBucket = (
        i: number,
        n: number,
        at: string,
        errorsOnly: boolean,
    ) => {
        const instance = target();
        if (!instance) return;
        const interval = step * 3600000,
            end =
                Math.floor((statsAt.value || Date.now()) / interval) * interval,
            start = end - (n - 1 - i) * interval;
        openLogs({
            instance,
            query: "*",
            title: `${errorsOnly ? "Errors" : "Messages"} at ${at}`,
            from: new Date(start).toISOString(),
            to: new Date(start + interval).toISOString(),
            when: `${at} to ${String((Number(at.slice(-5).split(":")[0]) + step) % 24).padStart(2, "0")}:${at.slice(-2)}`,
            errorsOnly,
        });
    };
    const tiles = [
        {
            label: `Log messages · ${r}`,
            to: noEnv ? null : tot,
            format: short,
            placeholder: noEnv ? "-" : "…",
            note: noEnv
                ? "no environment yet"
                : down
                  ? `${down} environment${down > 1 ? "s" : ""} not reached`
                  : envs
                    ? ctx
                        ? label(ctx)
                        : `across ${plural(envs.length, "environment")}`
                    : "reading Graylog…",
            cls: down ? "warn" : "",
            title: noEnv ? "" : "Read these log lines",
            run: noEnv ? undefined : () => showLogs(false),
        },
        {
            label: `Errors · ${r}`,
            to: noEnv ? null : errors,
            format: short,
            placeholder: noEnv ? "-" : "…",
            note:
                errors === null
                    ? "\u00a0"
                    : hot >= 0
                      ? `spike at ${errs.labels[hot]}`
                      : tot
                        ? `${((100 * (errors || 0)) / tot).toFixed(1)}% of messages`
                        : "\u00a0",
            cls: hot >= 0 ? "bad" : "",
            title: noEnv ? "" : "Read the error lines",
            run: noEnv ? undefined : () => showLogs(true),
        },
        {
            label: `Tool calls · ${r}${scope}`,
            to: u ? calls : null,
            format: num,
            placeholder: "…",
            note: !u
                ? "\u00a0"
                : !u.enabled
                  ? "recording is off"
                  : u.previous.calls
                    ? `${diff >= 0 ? "↑" : "↓"} ${Math.abs(Math.round((100 * diff) / u.previous.calls))}% ${before}`
                    : `${diff >= 0 ? "+" : ""}${diff} ${before}`,
            cls: "",
            title: "See the calls in Activity",
            run: () => openCalls(),
        },
        {
            label: `Found something${scope}`,
            to: hit,
            format: (n: number) => `${n}%`,
            placeholder: !u ? "…" : "-",
            note: !u
                ? "\u00a0"
                : hitDiff !== null
                  ? `${hitDiff >= 0 ? "+" : ""}${hitDiff} pt ${before}`
                  : u.totals.searches
                    ? `${u.totals.misses} of ${u.totals.searches} found nothing`
                    : "no search yet",
            cls:
                hitDiff === null
                    ? u?.totals.misses
                        ? "warn"
                        : ""
                    : hitDiff < 0
                      ? "warn"
                      : hitDiff > 0
                        ? "good"
                        : "",
            title: u?.totals.searches
                ? `${u.totals.misses} of ${u.totals.searches} searches found nothing`
                : "See the searches in Activity",
            run: openProblems,
        },
    ];
    const previousMiss = useRef<Attn | null>(null);
    const items = attentionItems();
    if (!u && previousMiss.current) items.push(previousMiss.current);
    else
        previousMiss.current =
            items.find((item) => item.id === "misses") || null;
    const bad = all.filter((e) => envHealth(e).cls === "fail").length,
        ok = checks.value
            ? all.filter((e) => envHealth(e).cls === "ok").length
            : null,
        groups = new Set(all.map((e) => e.group).filter(Boolean)).size;
    const status: ComponentChildren[] = [];
    if (!all.length) status.push("no environment yet");
    else {
        if (groups)
            status.push(
                <>
                    <b>{groups}</b> {groups === 1 ? "group" : "groups"}
                </>,
            );
        status.push(
            <>
                <b>{all.length}</b>{" "}
                {all.length === 1 ? "environment" : "environments"}
            </>,
        );
        status.push(
            ok === null ? (
                loadErrors.value.doctor ? (
                    "check failed"
                ) : (
                    "checking…"
                )
            ) : (
                <>
                    <b>{ok}</b> healthy
                </>
            ),
        );
        if (bad)
            status.push(
                <>
                    <b class="bad">{bad}</b> failing
                </>,
            );
    }
    if (u?.enabled) {
        status.push(
            <>
                <b>{num(calls)}</b> calls / {r}
            </>,
        );
        if (hit !== null)
            status.push(
                <>
                    <b>{hit}%</b> found something
                </>,
            );
    }
    const emptyCalls = !u
        ? "Loading…"
        : !u.enabled
          ? "Recording is off (GRAYLOG_MCP_USAGE=off)."
          : "No tool calls yet. They show up here as soon as Claude uses graylog.";
    const misses = (
        u
            ? ctx
                ? u.misses.filter((m) => m.instance === ctx.name)
                : u.misses
            : []
    ).slice(0, 6);
    const chartError = noEnv
        ? "Add an environment to see its logs here."
        : loadErrors.value.stats ||
          (!vol.values.length && st
              ? `No environment answered${envs?.length ? `: ${envs[0].error || ""}` : ""}.`
              : "");
    return (
        <Page
            id="home"
            tools={
                <>
                    <ContextChip />
                    <Range />
                    <Refresh
                        id="recheck"
                        run={() => {
                            breakdownRefresh.value++;
                            return Promise.all([runDoctor(), loadDashboard()]);
                        }}
                    />
                </>
            }
        >
            <div class="statusline" id="statusline" aria-live="polite">
                <span class="p">▸</span>
                {status.map((p, i) => (
                    <>
                        {i > 0 && (
                            <span class="sep" aria-hidden="true">
                                ·
                            </span>
                        )}
                        <span>{p}</span>
                    </>
                ))}
                <span class="cursor" aria-hidden="true" />
            </div>
            <section class="attn" aria-labelledby="attnTitle">
                <div class="card-head">
                    <h2 id="attnTitle">Needs attention</h2>
                    <span class="count-pill" id="attnCount">
                        {items.length ? `${items.length} open` : ""}
                    </span>
                    <span class="spacer" />
                    <span id="attnHead">
                        {setupSteps().filter((s) => !s.ok && s.auto).length >
                            1 && (
                            <AsyncButton class="btn primary" run={finishSetup}>
                                <Icon name="bolt" />
                                Finish setup
                            </AsyncButton>
                        )}
                    </span>
                </div>
                <div id="attnList">
                    <AttentionRows items={items} />
                    {!items.length &&
                        (checks.value &&
                        u &&
                        (setup.value || loadErrors.value.setup) ? (
                            <div class="allclear">// all clear</div>
                        ) : (
                            <div class="skeleton askel" />
                        ))}
                </div>
            </section>
            <div class="dash-top">
                <div class="card">
                    <div class="card-head">
                        <h2>Environments</h2>
                        <span class="muted small" id="healthSummary">
                            {!all.length
                                ? ""
                                : checks.value
                                  ? `${ok} of ${all.length} healthy`
                                  : loadErrors.value.doctor
                                    ? "check failed"
                                    : "checking…"}
                        </span>
                        <span class="spacer" />
                        <button
                            class="btn sm ghost icon"
                            aria-label="Add environment"
                            title="Add environment"
                            onClick={() => openEnv(null)}
                        >
                            <Icon name="plus" />
                        </button>
                    </div>
                    <div id="healthList" class="health">
                        {!all.length ? (
                            <div class="empty">
                                <h3>No environment yet</h3>
                                <div>Add the Graylog of each environment.</div>
                                <div class="actions">
                                    <button
                                        class="btn primary"
                                        onClick={() => openEnv(null)}
                                    >
                                        <Icon name="plus" />
                                        Add your first environment
                                    </button>
                                </div>
                            </div>
                        ) : (
                            byGroup(all).map(([g, envs]) => (
                                <>
                                    {g && (
                                        <div class="hgroup">
                                            {g}
                                            {state.value?.default_group ===
                                                g && (
                                                <span class="badge">
                                                    default
                                                </span>
                                            )}
                                        </div>
                                    )}
                                    {envs.map((e) => {
                                        const h = envHealth(e),
                                            need = !e.secret_set && e.local,
                                            kind =
                                                e.auth === "basic"
                                                    ? "password"
                                                    : "token",
                                            p = h.problems[0],
                                            s = st?.environments.find(
                                                (x) =>
                                                    x.name === e.name &&
                                                    !x.error,
                                            ),
                                            action =
                                                need ||
                                                (h.cls === "fail" && e.local);
                                        return (
                                            <div
                                                class={`hrow${e.name === currentEnv.value ? " ctx" : ""}`}
                                                onClick={() =>
                                                    openEnvByName(e.name)
                                                }
                                            >
                                                <div class="name">
                                                    <button
                                                        class="namebtn"
                                                        aria-label={`Edit ${label(e)}`}
                                                    >
                                                        {e.environment ||
                                                            e.name}
                                                    </button>
                                                    {e.default && (
                                                        <span class="badge">
                                                            default
                                                        </span>
                                                    )}
                                                    {!e.local && (
                                                        <span class="badge">
                                                            shared file
                                                        </span>
                                                    )}
                                                </div>
                                                <div class="detail">
                                                    {need ? (
                                                        `${kind} missing`
                                                    ) : p ? (
                                                        <span
                                                            class="problem"
                                                            title={
                                                                p.detail +
                                                                (p.fix
                                                                    ? `. Fix: ${p.fix}`
                                                                    : "")
                                                            }
                                                        >
                                                            {p.detail}
                                                        </span>
                                                    ) : s ? (
                                                        <>
                                                            <button
                                                                class="linkish"
                                                                title={`Read the log lines of ${label(e)}`}
                                                                onClick={(
                                                                    ev,
                                                                ) => {
                                                                    ev.stopPropagation();
                                                                    openLogs({
                                                                        instance:
                                                                            e.name,
                                                                        range: r,
                                                                        query: "*",
                                                                        title: "Every log line",
                                                                    });
                                                                }}
                                                            >
                                                                {num(s.total)}{" "}
                                                                messages
                                                            </button>
                                                            ,{" "}
                                                            <button
                                                                class={`linkish${s.errors ? " bad" : ""}`}
                                                                title={`Read the error lines of ${label(e)}`}
                                                                onClick={(
                                                                    ev,
                                                                ) => {
                                                                    ev.stopPropagation();
                                                                    openLogs({
                                                                        instance:
                                                                            e.name,
                                                                        range: r,
                                                                        query: "*",
                                                                        title: "Every error line",
                                                                        errorsOnly: true,
                                                                    });
                                                                }}
                                                            >
                                                                {num(s.errors)}{" "}
                                                                errors
                                                            </button>
                                                        </>
                                                    ) : (
                                                        h.text || e.url
                                                    )}
                                                </div>
                                                {s && !action ? (
                                                    <Sparkline
                                                        values={s.buckets.map(
                                                            (b) => b[1],
                                                        )}
                                                    />
                                                ) : (
                                                    <span class="spark-slot" />
                                                )}
                                                <div class="actions">
                                                    {need ? (
                                                        <button class="btn sm">
                                                            <Icon name="key" />
                                                            Add {kind}
                                                        </button>
                                                    ) : h.cls === "fail" &&
                                                      e.local ? (
                                                        <button class="btn sm">
                                                            <Icon name="edit" />
                                                            Fix
                                                        </button>
                                                    ) : null}
                                                    <StateTag cls={h.cls} />
                                                </div>
                                            </div>
                                        );
                                    })}
                                </>
                            ))
                        )}
                    </div>
                </div>
                <div class="kpis" id="kpis">
                    {tiles.map((t, i) => {
                        const children = (
                            <>
                                <div class="kl">{t.label}</div>
                                <Figure
                                    index={i}
                                    to={t.to}
                                    format={t.format}
                                    placeholder={t.placeholder}
                                />
                                <div class={`kd ${t.cls}`}>{t.note}</div>
                            </>
                        );
                        return t.run ? (
                            <button
                                type="button"
                                class="kpi link"
                                title={t.title}
                                onClick={t.run}
                            >
                                {children}
                            </button>
                        ) : (
                            <div class="kpi">{children}</div>
                        );
                    })}
                </div>
            </div>
            <div class="dgrid">
                <div class="card">
                    <div class="card-head">
                        <h2>Log volume</h2>
                        <span class="muted small" id="volSub">
                            messages per {step === 1 ? "hour" : `${step} hours`}
                            , {ctx ? label(ctx) : "every environment"}
                        </span>
                    </div>
                    {chartError ? (
                        <div id="volChart">
                            <div class="inline-msg">{chartError}</div>
                        </div>
                    ) : !st ? (
                        <div id="volChart">
                            <Skeleton />
                        </div>
                    ) : (
                        <Chart
                            id="volChart"
                            line
                            labels={vol.labels}
                            values={vol.values}
                            name="Messages"
                            pick={(i) =>
                                openBucket(
                                    i,
                                    vol.values.length,
                                    vol.labels[i],
                                    false,
                                )
                            }
                            how="Left and right arrows move, Enter opens the log lines of that point."
                        />
                    )}
                </div>
                <div class="card">
                    <div class="card-head">
                        <h2>Errors</h2>
                        <span class="muted small" id="errSub">
                            per {step === 1 ? "hour" : `${step} hours`}
                        </span>
                    </div>
                    {chartError ? (
                        <div id="errChart">
                            <div class="inline-msg">{chartError}</div>
                        </div>
                    ) : !st ? (
                        <div id="errChart">
                            <Skeleton />
                        </div>
                    ) : (
                        <Chart
                            id="errChart"
                            labels={errs.labels}
                            series={[
                                {
                                    name: "Errors",
                                    values: errs.values,
                                    color: "var(--chart-error-soft)",
                                },
                            ]}
                            name="Errors"
                            spike
                            pick={(i) =>
                                openBucket(
                                    i,
                                    errs.values.length,
                                    errs.labels[i],
                                    true,
                                )
                            }
                            how="Left and right arrows move, Enter opens the error lines of that bar."
                        />
                    )}
                </div>
            </div>
            <Breakdown />
            <div class="dgrid even">
                <div class="card">
                    <div class="card-head">
                        <h2>Tools the model used</h2>
                        <span class="muted small" id="toolNote">
                            {ctx ? "all environments" : ""}
                        </span>
                    </div>
                    <div id="toolBars">
                        <HBars
                            items={(u?.tools || []).slice(0, 7).map((t) => ({
                                label: t.tool,
                                value: t.calls,
                                title: `${t.calls} calls, ${t.misses} found nothing, ${t.errors} errors, p95 ${t.p95_ms} ms`,
                            }))}
                            empty={emptyCalls}
                        />
                    </div>
                </div>
                <div class="card">
                    <div class="card-head">
                        <h2>Searches that found nothing</h2>
                        <span class="spacer" />
                        <a
                            href="#activity"
                            class="small"
                            onClick={(e) => {
                                e.preventDefault();
                                void navigate("activity");
                            }}
                        >
                            Activity
                        </a>
                    </div>
                    <div id="missList">
                        {!u ? (
                            <div class="inline-msg">
                                {loadErrors.value.usage
                                    ? `Could not load the calls: ${loadErrors.value.usage}.`
                                    : "Loading…"}
                            </div>
                        ) : !misses.length ? (
                            <div class="inline-msg">
                                {u.totals.searches
                                    ? `Every search found something${ctx ? ` on ${label(ctx)}` : ""}.`
                                    : "No search yet."}
                            </div>
                        ) : (
                            <div class="health">
                                {misses.map((m) => (
                                    <div
                                        class="hrow pair"
                                        title="Open in the Playground"
                                        onClick={() => rerun(m)}
                                    >
                                        <div class="min0">
                                            <div class="mono small truncate">
                                                {callWhat(m)}
                                            </div>
                                            <div class="detail small">
                                                {m.tool}, {m.instance || ""},{" "}
                                                {timeAgo(m.ts)}
                                            </div>
                                        </div>
                                        <span class="miss small">0 found</span>
                                    </div>
                                ))}
                            </div>
                        )}
                    </div>
                </div>
            </div>
        </Page>
    );
}
