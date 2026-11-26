import { useEffect } from "preact/hooks";
import { currentPage, instances, toast } from "../store";
import { openInLogs } from "../explorer-store";
import {
    STATES,
    chooseCheck,
    chooseEvidence,
    close,
    downloadExport,
    invEnabled,
    invRestart,
    invSource,
    invError,
    invEvidence,
    invFilter,
    invList,
    invLive,
    invSel,
    invSnap,
    loadInvestigations,
    loadSnapshot,
    open,
    setInvFilter,
    setInvestigations,
    startStream,
    stopStream,
} from "../investigations";
import {
    CHECK_CLASS,
    CHECK_WORD,
    OUTCOME_WORD,
    STATE_CLASS,
    STEP_CLASS,
    STEP_WORD,
    aboutOf,
    analysisComplete,
    bodyText,
    chartsOf,
    citedBy,
    countText,
    coverageLine,
    cutText,
    elapsedText,
    evidenceNo,
    fixNotVerified,
    flowOf,
    foldEarlier,
    fullRequestOf,
    interruptedWhy,
    itemsOf,
    leaseText,
    logsViewOf,
    needsAttention,
    nextCheck,
    nodesIn,
    queryPair,
    reportOf,
    stateWord,
    traceExcerpts,
    windowOfEvidence,
    windowText,
    ranAt,
    viewZone,
    zoned,
    type CheckState,
    type ExcerptRow,
    type FlowNode,
    type FlowRow,
} from "../investigations-model";
import type { Fields, InvEvidence, InvSnapshot } from "../scripts/types";
import { HistogramStrip, RequestObservations } from "./InvestigationCharts";
import { CitedList, Findings, TokenList } from "./InvestigationFindings";
import { AsyncButton, Page, Refresh, Skeleton } from "./UI";
import { formatDateTime } from "../time";

const when = (iso: string | null) => formatDateTime(iso, "");
const LIVE_WORD = {
    live: "Live",
    connecting: "Connecting…",
    off: "Reconnecting",
    idle: "",
} as const;
// arrow keys move between the buttons of one list
function listKeys(e: KeyboardEvent) {
    const forward = e.key === "ArrowDown" || e.key === "ArrowRight";
    if (!forward && e.key !== "ArrowUp" && e.key !== "ArrowLeft") return;
    const all = [
        ...(e.currentTarget as HTMLElement).querySelectorAll<HTMLElement>(
            "button.inv-pick, button.inv-node-btn",
        ),
    ];
    const at = all.indexOf(document.activeElement as HTMLElement);
    const next = all[at + (forward ? 1 : -1)];
    if (next) {
        e.preventDefault();
        next.focus();
    }
}
function Badge({ cls, children }: { cls?: string; children: string }) {
    return <span class={`badge ${cls ?? ""}`}>{children}</span>;
}

// ------------------------------------------------------------------ list
function InvList() {
    const list = invList.value,
        f = invFilter.value;
    const names = [...new Set(instances.value.map((e) => e.name))];
    return (
        <>
            <div class="inv-filters">
                <label>
                    State
                    <select
                        value={f.state}
                        onChange={(e) =>
                            setInvFilter({ state: e.currentTarget.value })
                        }
                    >
                        {STATES.map((s) => (
                            <option value={s}>
                                {s === "all" ? "Any state" : stateWord(s)}
                            </option>
                        ))}
                    </select>
                </label>
                <label>
                    Environment
                    <select
                        value={f.instance}
                        onChange={(e) =>
                            setInvFilter({ instance: e.currentTarget.value })
                        }
                    >
                        <option value="">Any environment</option>
                        {names.map((n) => (
                            <option value={n}>{n}</option>
                        ))}
                    </select>
                </label>
            </div>
            {!list ? (
                <Skeleton />
            ) : !list.length ? (
                <div class="empty">
                    <h3>
                        {f.state !== "all" || f.instance
                            ? "No investigation matches"
                            : "No investigation yet"}
                    </h3>
                    <div>
                        An investigation starts from a connected agent: it calls{" "}
                        <code>investigation</code> with{" "}
                        <code>action="start"</code>, and what it checks and
                        concludes appears here. Nothing on this page starts one.
                    </div>
                </div>
            ) : (
                <ul class="inv-list" onKeyDown={listKeys}>
                    {list.map((i) => (
                        <li>
                            <button
                                class="inv-pick inv-row"
                                onClick={() => void open(i.id)}
                            >
                                <span class="inv-row-main">
                                    <b>{i.title}</b>
                                    <span class="inv-q">{i.question}</span>
                                </span>
                                <span class="inv-row-meta">
                                    <Badge cls={STATE_CLASS[i.state]}>
                                        {stateWord(i.state)}
                                    </Badge>
                                    {needsAttention(i) && (
                                        <Badge cls="warn">
                                            Needs attention
                                        </Badge>
                                    )}
                                    <span class="mono">{i.label}</span>
                                    <span>{i.instance}</span>
                                    <span>{windowText(i.scope)}</span>
                                    <span>Updated {when(i.updated)}</span>
                                </span>
                            </button>
                        </li>
                    ))}
                </ul>
            )}
        </>
    );
}

// ------------------------------------------------------------------ workspace
const OUTCOME_CLASS: Record<string, string> = {
    explanation_supported: "ok",
    cause_undetermined: "warn",
    no_matching_evidence: "warn",
    no_anomaly_detected: "",
};
function Head({ snap }: { snap: InvSnapshot }) {
    const attempt = snap.attempts.find((a) => a.current),
        lease = leaseText(snap.lease_expires),
        why = interruptedWhy(snap),
        zone = zoned(snap.scope.from),
        started = snap.scope_revisions[0]?.author;
    return (
        <header class="inv-head">
            <button class="btn sm ghost" onClick={() => void close()}>
                ← All investigations
            </button>
            <h2>
                {snap.title} <span class="mono">{snap.label}</span>
            </h2>
            <div class="inv-tags">
                <Badge cls={STATE_CLASS[snap.state]}>
                    {why
                        ? `${stateWord(snap.state)} · ${why}`
                        : stateWord(snap.state)}
                </Badge>
                {snap.outcome && (
                    <Badge cls={OUTCOME_CLASS[snap.outcome]}>
                        {OUTCOME_WORD[snap.outcome] ?? snap.outcome}
                    </Badge>
                )}
                <span class="inv-tag">{snap.instance}</span>
                <span
                    class="inv-tag"
                    title={`Times are shown in your browser's time zone, ${viewZone()}${zone ? ` (${zone.zone})` : ""}`}
                >
                    {windowText(snap.scope)}
                </span>
            </div>
            <p class="inv-question">{snap.question}</p>
            <details class="inv-more">
                <summary>Details</summary>
                <dl class="inv-facts">
                    <div>
                        <dt>Query</dt>
                        <dd class="mono">{snap.scope.query || "*"}</dd>
                    </div>
                    {snap.repo && (
                        <div>
                            <dt>Repository</dt>
                            <dd class="mono">{snap.repo}</dd>
                        </div>
                    )}
                    {started && (
                        <div>
                            <dt>Started by</dt>
                            <dd>{started}</dd>
                        </div>
                    )}
                    <div>
                        <dt>Attempts</dt>
                        <dd>
                            {snap.attempts.length}
                            {attempt && ` · current ${attempt.n}`}
                            {lease && snap.state === "running" && (
                                <span> · agent lease {lease}</span>
                            )}
                        </dd>
                    </div>
                    <div>
                        <dt>Scope revisions</dt>
                        <dd>
                            {snap.scope_revisions.length || 1}
                            {snap.scope.rev > 1 &&
                                ` · now revision ${snap.scope.rev}`}
                        </dd>
                    </div>
                    <div>
                        <dt>Time zone</dt>
                        <dd>
                            {viewZone()}
                            <span class="inv-muted">
                                {" "}
                                · your browser; the instance uses{" "}
                                {snap.scope.timezone}
                            </span>
                        </dd>
                    </div>
                    <div>
                        <dt>Stream</dt>
                        <dd>{LIVE_WORD[invLive.value] || "Not connected"}</dd>
                    </div>
                </dl>
                <div class="inv-actions">
                    {snap.follows && (
                        <button
                            class="btn sm ghost"
                            onClick={() => void open(snap.follows!)}
                        >
                            Follows up an earlier investigation
                        </button>
                    )}
                    {snap.followed_by?.map((l) => (
                        <button
                            class="btn sm ghost"
                            onClick={() => void open(l)}
                        >
                            Followed up by {l}
                        </button>
                    ))}
                </div>
            </details>
        </header>
    );
}
function ReportView({ r }: { r: Fields }) {
    const got = reportOf(r);
    return (
        <div class="inv-report">
            {got.outcome && (
                <Badge cls={OUTCOME_CLASS[got.outcome]}>
                    {OUTCOME_WORD[got.outcome] ?? got.outcome}
                </Badge>
            )}
            {!!got.findings.length && (
                <ul class="inv-bullets">
                    {got.findings.map((f) => (
                        <li>{f}</li>
                    ))}
                </ul>
            )}
            {got.text && <p>{got.text}</p>}
            {!!got.limits.length && (
                <>
                    <h4>Limits</h4>
                    <ul class="inv-bullets">
                        {got.limits.map((f) => (
                            <li>{f}</li>
                        ))}
                    </ul>
                </>
            )}
            {!!got.next.length && (
                <>
                    <h4>Next checks</h4>
                    <ul class="inv-bullets">
                        {got.next.map((f) => (
                            <li>{f}</li>
                        ))}
                    </ul>
                </>
            )}
        </div>
    );
}
// choosing a node also opens its first evidence, so the right pane is never empty for a check that ran
function pickNode(evidence: string | undefined, check: string | null) {
    void chooseCheck(check);
    if (evidence) void chooseEvidence(evidence);
}
const GLYPH: Record<CheckState, string> = {
    done: "✓",
    running: "",
    failed: "!",
    not_run: "",
};
// one check: what it was for, the tool, what it showed, when it ran, and the evidence behind it
function Node({ n, snap }: { n: FlowNode; snap: InvSnapshot }) {
    const sel = invSel.value,
        on =
            (n.check !== null && sel.check === n.check) ||
            (n.check === null && n.evidence.includes(sel.evidence ?? "")),
        failed = n.steps.find((s) => s.error),
        at = ranAt(n.started);
    return (
        <li class={`inv-node ${CHECK_CLASS[n.state]}${on ? " on" : ""}`}>
            <button
                class="inv-node-btn"
                aria-current={on}
                onClick={() => pickNode(n.evidence[0], n.check)}
            >
                <span class={`inv-mark ${n.state}`} aria-hidden="true">
                    {GLYPH[n.state]}
                </span>
                <b>{n.title}</b>
                {!!n.tools.length && (
                    <small class="mono">tool: {n.tools.join(", ")}</small>
                )}
                {n.result && <span class="inv-node-result">{n.result}</span>}
                {failed && <span class="inv-err">{failed.error}</span>}
                <span
                    class={`inv-state ${CHECK_CLASS[n.state]}`}
                    title={at?.full}
                >
                    {[
                        at?.text,
                        n.state === "running"
                            ? `Running ${elapsedText(n.started)}`.trim()
                            : CHECK_WORD[n.state],
                        on ? "Selected" : "",
                        n.note ?? "",
                    ]
                        .filter(Boolean)
                        .join(" · ")}
                </span>
            </button>
            {!!n.evidence.length && (
                <TokenList snap={snap} ids={n.evidence} on={sel.evidence} />
            )}
        </li>
    );
}
// a row of the flow: one node, or side by side the checks of a branch or the ones that ran together
function Row({ row, snap }: { row: FlowRow; snap: InvSnapshot }) {
    const n = row.nodes.length;
    return (
        <li class="inv-row">
            {row.caption && <p class="inv-group">{row.caption}</p>}
            <ul
                class={n > 1 ? "inv-par" : "inv-solo"}
                style={
                    n > 1 ? ({ "--n": n } as Record<string, number>) : undefined
                }
            >
                {row.nodes.map((node) => (
                    <Node n={node} snap={snap} />
                ))}
            </ul>
        </li>
    );
}
function TaskFlow({ snap }: { snap: InvSnapshot }) {
    const flow = flowOf(snap),
        { earlier, rest } = foldEarlier(flow.rows),
        sel = invSel.value,
        hidden = earlier.some((r) =>
            r.nodes.some(
                (n) =>
                    (n.check !== null && n.check === sel.check) ||
                    (!!sel.evidence && n.evidence.includes(sel.evidence)),
            ),
        ),
        zone = zoned(snap.scope.from)?.zone;
    return (
        <section class="inv-pane inv-flow" aria-label="Task flow">
            <h3>
                Task flow{" "}
                <span class="inv-muted">
                    evidence gathering{zone ? ` · times in ${zone}` : ""}
                </span>
            </h3>
            <ol class="inv-rows" onKeyDown={listKeys}>
                {!!earlier.length && (
                    <li class="inv-row">
                        <details class="inv-earlier" open={hidden || undefined}>
                            <summary>{nodesIn(earlier)} earlier checks</summary>
                            <ol class="inv-rows">
                                {earlier.map((r) => (
                                    <Row row={r} snap={snap} />
                                ))}
                            </ol>
                        </details>
                    </li>
                )}
                {rest.map((r) => (
                    <Row row={r} snap={snap} />
                ))}
                {flow.asks.map((r) => (
                    <li class="inv-row">
                        <ul class="inv-solo">
                            <li class="inv-node ask">
                                <span class="inv-mark ask" aria-hidden="true">
                                    ?
                                </span>
                                <b>{bodyText(r, "question")}</b>
                                <span class="inv-state warn">
                                    Waiting for your answer
                                </span>
                            </li>
                        </ul>
                    </li>
                ))}
            </ol>
            {!snap.steps.length && !flow.asks.length && (
                <p class="inv-muted">No step has run yet.</p>
            )}
        </section>
    );
}
function Explanations({ snap }: { snap: InvSnapshot }) {
    return (
        <aside class="inv-pane inv-evidence" aria-label="Working explanations">
            <h3>Working explanations</h3>
            <Findings snap={snap} explanations />
        </aside>
    );
}
const excerptText = (x: unknown): string =>
    Array.isArray(x)
        ? x.join(" · ")
        : x && typeof x === "object"
          ? Object.entries(x)
                .map(([k, v]) => `${k} ${String(v)}`)
                .join("  ")
          : String(x);
// +270 ms  payment  ERROR  Connection pool exhausted
function Line({ x }: { x: ExcerptRow }) {
    return (
        <li class={`inv-line${x.first ? " first" : ""}`}>
            <span class="mono inv-line-t">{x.offset}</span>
            <span class="inv-line-svc" title={x.service}>
                {x.service}
            </span>
            <span class={`badge ${x.error ? "fail" : ""}`}>{x.level}</span>
            <span class="mono inv-line-msg" title={x.message}>
                {x.message}
            </span>
        </li>
    );
}
function SearchIcon() {
    return (
        <svg class="inv-icon" viewBox="0 0 16 16" aria-hidden="true">
            <circle cx="7" cy="7" r="4.5" />
            <path d="m10.5 10.5 3.5 3.5" />
        </svg>
    );
}
// The inspector: the evidence as asked and over which window, a picture where one helps, a few lines, and what it
// means for the explanations behind a disclosure
function EvidencePane({ snap }: { snap: InvSnapshot }) {
    const e: InvEvidence | null = invEvidence.value,
        sel = invSel.value;
    if (!e || e.id !== sel.evidence)
        return (
            <aside
                class="inv-pane inv-evidence inv-inspector"
                aria-label="Evidence"
            >
                <Skeleton />
            </aside>
        );
    const pair = queryPair(e),
        logs = logsViewOf(e),
        request = fullRequestOf(e),
        step = e.step,
        cited = citedBy(snap, e.id),
        window = windowOfEvidence(e, snap),
        series = chartsOf(e.summary, e.kind, e.excerpts),
        cut = cutText(e),
        isTrace = e.kind === "trace_request",
        hasChart = !!series.trace || !!series.histogram,
        shown: ExcerptRow[] = isTrace ? traceExcerpts(e, 3) : [],
        generic = isTrace || series.histogram ? [] : e.excerpts.slice(0, 3),
        rest = isTrace
            ? traceExcerpts(e, Infinity).slice(shown.length)
            : series.histogram
              ? []
              : e.excerpts.slice(3),
        hasError = !!series.trace?.first_error || shown.some((x) => x.error),
        note = [
            countText(e),
            coverageLine(e.kind, !!series.trace),
            series.trace ? "" : cut,
        ]
            .filter(Boolean)
            .join(" · ");
    return (
        <aside
            class="inv-pane inv-evidence inv-inspector"
            aria-label="Evidence"
        >
            <div class="inv-ins-head">
                <button
                    class="btn sm ghost"
                    onClick={() => void chooseEvidence(null)}
                >
                    ← Explanations
                </button>
                <span class="inv-muted">Saved query + absolute window</span>
            </div>
            <h3 class="inv-ins-title">
                <span class="mono">
                    {evidenceNo(snap, e.id)} / {aboutOf(e)}
                </span>
                {step?.fenced_out && (
                    <Badge cls="fail">Arrived after takeover</Badge>
                )}
                {step && step.state !== "succeeded" && (
                    <Badge cls={STEP_CLASS[step.state]}>
                        {STEP_WORD[step.state] ?? step.state}
                    </Badge>
                )}
            </h3>
            <div class="inv-query">
                <SearchIcon />
                <code>{pair.asked ?? "—"}</code>
            </div>
            {pair.differs && (
                <p class="inv-gap">
                    Ran a different query: <code>{pair.ran ?? "—"}</code>
                </p>
            )}
            {window && <p class="inv-muted">Saved query · {window}</p>}
            {series.trace && (
                <RequestObservations chart={series.trace} cut={cut} />
            )}
            {series.histogram && (
                <HistogramStrip
                    chart={series.histogram}
                    sourceZone={
                        typeof e.summary.timezone === "string"
                            ? e.summary.timezone
                            : snap.scope.timezone
                    }
                />
            )}
            {e.excerpts_expired ? (
                <p class="inv-muted">
                    Excerpts expired: the summary and counts are kept.
                </p>
            ) : (
                (!!shown.length || !!generic.length) && (
                    <ol class="inv-excerpts">
                        {shown.map((x) => (
                            <Line x={x} />
                        ))}
                        {generic.map((x) => (
                            <li class="mono">{excerptText(x)}</li>
                        ))}
                    </ol>
                )
            )}
            <p class="inv-coverage">
                {note || (hasChart ? "" : "No excerpts kept.")}
                {e.sampled && <Badge cls="warn">Sampled</Badge>}
                {e.truncated && <Badge cls="warn">Cut to fit</Badge>}
            </p>
            <details class="inv-more inv-why">
                <summary>Why it matters</summary>
                {!!cited.supports.length && (
                    <CitedList
                        title="Supports"
                        cls="supports"
                        items={cited.supports}
                    />
                )}
                {!!cited.contradicts.length && (
                    <CitedList
                        title="Contradicts"
                        cls="contradicts"
                        items={cited.contradicts}
                    />
                )}
                {!!e.coverage.length && (
                    <div class="inv-gaps">
                        <h4>Gaps</h4>
                        <ul class="inv-bullets">
                            {e.coverage.map((c) => (
                                <li>{c}</li>
                            ))}
                        </ul>
                    </div>
                )}
                {!!cited.noted.length && (
                    <CitedList title="Looked at" cls="" items={cited.noted} />
                )}
                {!cited.supports.length &&
                    !cited.contradicts.length &&
                    !cited.noted.length &&
                    !e.coverage.length && (
                        <p class="inv-muted">
                            No explanation rests on this yet.
                        </p>
                    )}
                {!!rest.length && !e.excerpts_expired && (
                    <>
                        <h4>More lines</h4>
                        <ol class="inv-excerpts">
                            {rest.map((x) =>
                                typeof x === "object" && "offset" in x ? (
                                    <Line x={x as ExcerptRow} />
                                ) : (
                                    <li class="mono">{excerptText(x)}</li>
                                ),
                            )}
                        </ol>
                    </>
                )}
            </details>
            <div class="inv-actions">
                {logs && (
                    <button
                        class="btn sm"
                        onClick={() => void openInLogs(logs)}
                    >
                        Open in Logs
                    </button>
                )}
                {request && (
                    <button
                        class="btn sm"
                        onClick={() => void openInLogs(request)}
                    >
                        Open full request
                    </button>
                )}
                {hasError && series.trace && (
                    <span class="inv-muted">
                        First observed error does not establish root cause.
                    </span>
                )}
            </div>
        </aside>
    );
}
// the end of the journey: what was observed, the probable explanation, what is still unverified
function Handoff({ snap }: { snap: InvSnapshot }) {
    const report = reportOf(snap.reports[snap.reports.length - 1] ?? {}),
        assess = itemsOf(snap, "assessment"),
        supported = assess.find((a) => a.data.assessment === "supported"),
        open = assess.find((a) => a.data.assessment === "undetermined"),
        ask = nextCheck(snap),
        results = itemsOf(snap, "check_result"),
        observed =
            report.findings[0] ??
            (results[0] ? bodyText(results[results.length - 1], "result") : ""),
        probable = supported
            ? bodyText(supported, "claim")
            : snap.outcome
              ? (OUTCOME_WORD[snap.outcome] ?? snap.outcome)
              : "",
        unverified =
            (ask ? ask.text : "") ||
            (open ? bodyText(open, "claim") : "") ||
            report.limits[0] ||
            "";
    const prompt = `Continue ${snap.label}. Read it with get_investigation (investigation_id='${snap.label}', view='briefing'), then call investigation (action='resume', investigation_id='${snap.label}').`;
    return (
        <footer class="inv-handoff">
            <div>
                <h4>Observed</h4>
                <p>{observed || "Nothing recorded yet."}</p>
            </div>
            <div>
                <h4>Probable explanation</h4>
                <p>{probable || "Not concluded yet."}</p>
            </div>
            <div>
                <h4>Still unverified</h4>
                <p>{unverified || "Nothing open."}</p>
            </div>
            <div class="inv-handoff-acts">
                {(analysisComplete(snap) || fixNotVerified(snap)) && (
                    <div class="inv-tags">
                        {analysisComplete(snap) && (
                            <Badge cls="ok">Analysis complete</Badge>
                        )}
                        {fixNotVerified(snap) && (
                            <Badge>Fix not verified</Badge>
                        )}
                    </div>
                )}
                <AsyncButton
                    class="btn sm"
                    run={async () => {
                        await navigator.clipboard.writeText(prompt);
                        toast("Copied: paste it to the agent");
                    }}
                >
                    Copy prompt to resume
                </AsyncButton>
                <button
                    class="btn sm ghost"
                    onClick={() => void downloadExport(snap)}
                >
                    Export Markdown
                </button>
                <code class="inv-cli">
                    graylog-mcp investigate show {snap.label} --open
                </code>
            </div>
            {(!!snap.reports.length ||
                itemsOf(snap, "identity").length > 0 ||
                itemsOf(snap, "relationship").length > 0 ||
                itemsOf(snap, "comparison").length > 0) && (
                <details class="inv-more inv-full">
                    <summary>Full report and findings</summary>
                    {!!snap.reports.length && (
                        <ReportView r={snap.reports[snap.reports.length - 1]} />
                    )}
                    <Findings snap={snap} extras />
                </details>
            )}
        </footer>
    );
}
function Workspace({ snap }: { snap: InvSnapshot }) {
    return (
        <div
            class="inv-workspace"
            onKeyDown={(e) => {
                if (e.key === "Escape" && invSel.value.evidence)
                    void chooseCheck(null);
            }}
        >
            <Head snap={snap} />
            {snap.truncated && (
                <p class="inv-muted">
                    This view is cut to fit: the oldest steps are left out.
                </p>
            )}
            <div class="inv-panes">
                <TaskFlow snap={snap} />
                {invSel.value.evidence ? (
                    <EvidencePane snap={snap} />
                ) : (
                    <Explanations snap={snap} />
                )}
            </div>
            <Handoff snap={snap} />
        </div>
    );
}
export function Investigations() {
    const active = currentPage.value === "investigations",
        sel = invSel.value,
        snap = invSnap.value;
    useEffect(() => {
        if (!active) {
            stopStream();
            return;
        }
        void loadInvestigations();
        const id = invSel.value.id;
        if (id && invLive.value === "idle") {
            void loadSnapshot(id);
            startStream(id);
        }
        return () => stopStream();
    }, [active]);
    return (
        <Page
            id="investigations"
            tools={
                <>
                    {sel.id && LIVE_WORD[invLive.value] && (
                        <span class="badge">{LIVE_WORD[invLive.value]}</span>
                    )}
                    {invEnabled.value &&
                        invSource.value !== "env" &&
                        !sel.id && (
                            <button
                                class="btn sm ghost"
                                onClick={() => void setInvestigations(false)}
                            >
                                Turn off
                            </button>
                        )}
                    <Refresh
                        run={() =>
                            sel.id ? loadSnapshot(sel.id) : loadInvestigations()
                        }
                    />
                </>
            }
        >
            {invError.value && (
                <div class="inline-msg" role="alert">
                    {invError.value}
                </div>
            )}
            {invRestart.value && (
                <div class="inline-msg" role="status">
                    Saved. A running server reads this when it starts: restart
                    it (<code>graylog-mcp start</code>) for new agent sessions
                    to {invEnabled.value ? "get" : "lose"} the investigation
                    tools.
                </div>
            )}
            {!invEnabled.value ? (
                <div class="empty">
                    <h3>Investigations are off</h3>
                    <div>
                        When on, a connected agent can keep a record of what it
                        asked, checked and concluded, and it appears here. It
                        adds the <code>investigation</code> and{" "}
                        <code>get_investigation</code> tools, and about 9 KB to
                        each session's tool list.
                    </div>
                    <div class="actions">
                        {invSource.value === "env" ? (
                            <p class="inv-muted">
                                <code>GRAYLOG_MCP_INVESTIGATIONS</code> is set
                                for this server and decides; unset it to use
                                this switch.
                            </p>
                        ) : (
                            <button
                                class="btn primary"
                                onClick={() => void setInvestigations(true)}
                            >
                                Turn investigations on
                            </button>
                        )}
                    </div>
                </div>
            ) : !sel.id ? (
                <InvList />
            ) : snap ? (
                <Workspace snap={snap} />
            ) : (
                <Skeleton />
            )}
        </Page>
    );
}
