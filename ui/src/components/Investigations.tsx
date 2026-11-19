import { useEffect } from "preact/hooks";
import { currentPage, instances, toast } from "../store";
import { openInLogs } from "../explorer-store";
import { pathOf } from "../router";
import {
    STATES,
    chooseBranch,
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
    blockedBranches,
    bodyText,
    checkState,
    citedBy,
    countText,
    evidenceOfSteps,
    itemsOf,
    leaseText,
    logsViewOf,
    needsAttention,
    nextCheck,
    queryPair,
    reportOf,
    resultsOf,
    stateWord,
    windowText,
    workspaceOf,
    type CheckState,
    type CheckView,
} from "../investigations-model";
import type {
    Fields,
    InvEvidence,
    InvSnapshot,
    InvStep,
} from "../scripts/types";
import { CitedList, Findings } from "./InvestigationFindings";
import { AsyncButton, Page, Refresh, Skeleton } from "./UI";

const when = (iso: string | null) =>
    iso ? new Date(iso).toLocaleString([], { hour12: false }) : "";
const LIVE_WORD = {
    live: "Live",
    connecting: "Connecting…",
    off: "Reconnecting",
    idle: "",
} as const;
// arrow keys move between the buttons of one list
function listKeys(e: KeyboardEvent) {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    const all = [
        ...(e.currentTarget as HTMLElement).querySelectorAll<HTMLElement>(
            "button.inv-pick",
        ),
    ];
    const at = all.indexOf(document.activeElement as HTMLElement);
    const next = all[at + (e.key === "ArrowDown" ? 1 : -1)];
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
        lease = leaseText(snap.lease_expires);
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
                    {stateWord(snap.state)}
                </Badge>
                {snap.outcome && (
                    <Badge cls={OUTCOME_CLASS[snap.outcome]}>
                        {OUTCOME_WORD[snap.outcome] ?? snap.outcome}
                    </Badge>
                )}
                <span class="inv-tag">{snap.instance}</span>
                <span class="inv-tag">{windowText(snap.scope)}</span>
            </div>
            <p class="inv-question">{snap.question}</p>
            <details class="inv-more">
                <summary>Details</summary>
                <dl class="inv-facts">
                    <div>
                        <dt>Query</dt>
                        <dd class="mono">{snap.scope.query || "*"}</dd>
                    </div>
                    {snap.scope_revisions.length > 1 && (
                        <div>
                            <dt>Window</dt>
                            <dd>revision {snap.scope.rev}</dd>
                        </div>
                    )}
                    {attempt && (
                        <div>
                            <dt>Attempt</dt>
                            <dd>
                                {attempt.n}
                                {lease && snap.state === "running" && (
                                    <span> · agent lease {lease}</span>
                                )}
                            </dd>
                        </div>
                    )}
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
const STATE_MARK: Record<CheckState, string> = {
    done: "✓",
    running: "◌",
    failed: "!",
    not_run: "○",
};
// one step of the task flow: what was checked, with which tool, what it showed, and the evidence behind it
function FlowNode({
    title,
    steps,
    result,
    check,
    note,
    snap,
}: {
    title: string;
    steps: InvStep[];
    result?: string;
    check: string | null;
    note?: string;
    snap: InvSnapshot;
}) {
    const sel = invSel.value,
        state = checkState(steps),
        evidence = evidenceOfSteps(steps),
        on =
            (check !== null && sel.check === check) ||
            (check === null && evidence.includes(sel.evidence ?? "")),
        tools = [...new Set(steps.map((s) => s.operation))],
        failed = steps.find((s) => s.error);
    return (
        <li class={`inv-node ${CHECK_CLASS[state]}${on ? " on" : ""}`}>
            <button
                class="inv-node-btn"
                aria-current={on}
                onClick={() => pickNode(evidence[0], check)}
            >
                <span class="inv-mark" aria-hidden="true">
                    {STATE_MARK[state]}
                </span>
                <b>{title}</b>
                {!!tools.length && (
                    <small class="mono">tool: {tools.join(", ")}</small>
                )}
                {result && <span class="inv-node-result">{result}</span>}
                {failed && <span class="inv-err">{failed.error}</span>}
                <span class={`inv-state ${CHECK_CLASS[state]}`}>
                    {CHECK_WORD[state]}
                    {on && " · Selected"}
                    {note && ` · ${note}`}
                </span>
            </button>
            {!!evidence.length && (
                <span class="inv-cites">
                    {evidence.map((id) => (
                        <button
                            class={`btn sm ${sel.evidence === id ? "primary" : "ghost"}`}
                            aria-pressed={sel.evidence === id}
                            onClick={() => void chooseEvidence(id)}
                        >
                            {snap.evidence.find((e) => e.id === id)?.kind ??
                                "evidence"}
                        </button>
                    ))}
                </span>
            )}
        </li>
    );
}
function TaskFlow({ snap }: { snap: InvSnapshot }) {
    const ws = workspaceOf(snap),
        asks = itemsOf(snap, "evidence_request").filter(
            (r) => r.data.status === "requested",
        ),
        node = (c: CheckView) => (
            <FlowNode
                snap={snap}
                check={c.item.id}
                title={bodyText(c.item, "purpose")}
                steps={c.steps}
                result={resultsOf(snap, c.item.id)[0]?.data.result as string}
            />
        );
    return (
        <section class="inv-pane inv-flow" aria-label="Task flow">
            <h3>
                Task flow <span class="inv-muted">evidence gathering</span>
            </h3>
            <ol onKeyDown={listKeys}>
                {ws.branches.map((b) => (
                    <>
                        <li class="inv-group">
                            {bodyText(b.item, "question")}
                        </li>
                        {b.checks.map(node)}
                    </>
                ))}
                {ws.branches.length > 0 && ws.loose.length > 0 && (
                    <li class="inv-group">Other checks</li>
                )}
                {ws.loose.map(node)}
                {ws.adHoc.map((s) => (
                    <FlowNode
                        snap={snap}
                        check={null}
                        title={String(s.requested.query ?? s.operation)}
                        steps={[s]}
                        note="no check recorded"
                    />
                ))}
                {asks.map((r) => (
                    <li class="inv-node ask">
                        <span class="inv-mark" aria-hidden="true">
                            ?
                        </span>
                        <b>{bodyText(r, "question")}</b>
                        <span class="inv-state warn">
                            Waiting for your answer
                        </span>
                    </li>
                ))}
            </ol>
            {!snap.steps.length && !asks.length && (
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
function EvidencePane({ snap }: { snap: InvSnapshot }) {
    const e: InvEvidence | null = invEvidence.value,
        sel = invSel.value;
    if (!e || e.id !== sel.evidence)
        return (
            <aside class="inv-pane inv-evidence" aria-label="Evidence">
                <Skeleton />
            </aside>
        );
    const pair = queryPair(e),
        logs = logsViewOf(e),
        step = e.step,
        cited = citedBy(snap, e.id),
        range = (step?.executed as { range?: { from: string; to: string } })
            ?.range,
        shown = e.excerpts.slice(0, 5),
        more = e.excerpts.slice(5),
        line = (x: unknown) => <li class="mono">{excerptText(x)}</li>;
    return (
        <aside class="inv-pane inv-evidence" aria-label="Evidence">
            <button
                class="btn sm ghost"
                onClick={() => void chooseEvidence(null)}
            >
                ← Explanations
            </button>
            <h3>
                <span class="mono">{e.kind}</span>
            </h3>
            <div class="inv-tags">
                <span class="inv-tag">{countText(e)}</span>
                {e.sampled && <Badge cls="warn">Sampled</Badge>}
                {e.truncated && <Badge cls="warn">Cut to fit</Badge>}
                {step?.fenced_out && (
                    <Badge cls="fail">Arrived after takeover</Badge>
                )}
                {step && step.state !== "succeeded" && (
                    <Badge cls={STEP_CLASS[step.state]}>
                        {STEP_WORD[step.state] ?? step.state}
                    </Badge>
                )}
            </div>
            <h4>Query</h4>
            <code class="inv-code">{pair.asked ?? "—"}</code>
            {range && (
                <p class="inv-muted">
                    Saved query · {range.from} → {range.to}
                </p>
            )}
            {pair.differs && (
                <p class="inv-gap">
                    Ran a different query: <code>{pair.ran ?? "—"}</code>
                </p>
            )}
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
            {!!cited.noted.length && (
                <CitedList title="Looked at" cls="" items={cited.noted} />
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
            {e.excerpts_expired ? (
                <p class="inv-muted">
                    Excerpts expired: the summary and counts are kept.
                </p>
            ) : (
                !!e.excerpts.length && (
                    <>
                        <h4>Excerpts</h4>
                        <ol class="inv-excerpts">{shown.map(line)}</ol>
                        {!!more.length && (
                            <details class="inv-more">
                                <summary>{more.length} more</summary>
                                <ol class="inv-excerpts">{more.map(line)}</ol>
                            </details>
                        )}
                    </>
                )
            )}
            <div class="inv-actions">
                {logs && (
                    <button
                        class="btn sm"
                        onClick={() => void openInLogs(logs)}
                    >
                        Open in Logs
                    </button>
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
        <div class="inv-workspace">
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
