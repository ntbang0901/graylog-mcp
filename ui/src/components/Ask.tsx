import { useRef, useState } from "preact/hooks";
import { api, errorText } from "../api";
import {
    ERROR_WORDS,
    applies,
    costLine,
    noteText,
    vocabulary,
    whenText,
} from "../ask";
import { LOG_RANGES, drillQuery } from "../explorer";
import { openInLogs, update, view } from "../explorer-store";
import { phrase } from "../logs";
import { instances, label, toast } from "../store";
import type {
    AskAnswer,
    AskFilters,
    AskMatch,
    AskView,
    ErrorKind,
    Journey,
} from "../scripts/types";
import { JourneyView } from "./Journey";
import {
    AsyncButton,
    Close,
    Dialog,
    Icon,
    JsonText,
    Segment,
    num,
    plural,
} from "./UI";

const ERRORS: ErrorKind[] = ["any", "technical", "business", "both"];
const AS_READ = "as-read";
type Shown =
    | { kind: "journey"; code: string; data: Journey }
    | { kind: "error"; text: string };

// the Logs view a reading stands for; chips edit the reading, the view follows
function follow(r: AskView, first: boolean) {
    if (first) {
        void openInLogs({
            instance: r.instance,
            query: r.query,
            range: r.range,
            from: r.from,
            to: r.to,
        });
        return;
    }
    update({
        instance: r.instance,
        q: r.query === "*" ? "" : r.query,
        range: r.range,
        from: r.from,
        to: r.to,
        offset: 0,
        trace: null,
    });
}

function Sent({
    sent,
    open,
    onClose,
}: {
    sent: AskAnswer["sent"];
    open: boolean;
    onClose: () => void;
}) {
    return (
        <Dialog
            id="askSent"
            class="wide"
            aria-labelledby="askSentTitle"
            open={open}
            onClose={onClose}
        >
            <div class="dhead">
                <h2 id="askSentTitle">What was sent to model providers</h2>
                <span class="spacer" />
                <Close onClick={onClose} />
            </div>
            <div class="dbody scroll">
                <p class="hint">
                    What this question handed to each provider it asked, as
                    redacted as the provider got it. A provider the summary says
                    failed may not have received it.
                </p>
                {sent.map((s, i) => (
                    <div key={i}>
                        <h3>
                            {s.provider} <span class="muted">· {s.task}</span>
                        </h3>
                        <pre class="json mt-xs">
                            <JsonText
                                value={{
                                    state: s.state,
                                    questions: s.questions,
                                }}
                            />
                        </pre>
                    </div>
                ))}
            </div>
        </Dialog>
    );
}

function Match({
    m,
    best,
    onJourney,
    instance,
    ran,
}: {
    m: AskMatch;
    best: boolean;
    onJourney: (() => unknown) | null;
    instance: string;
    ran: string;
}) {
    const n = `${m.estimated ? "~" : ""}${num(m.count)} time${m.count === 1 ? "" : "s"}`;
    return (
        <li class={`ask-match${best ? " best" : ""}`}>
            <div class="ask-mhead">
                {m.service && <b>{m.service}</b>}
                {m.exception && <span class="mono small">{m.exception}</span>}
                <span class="muted small">
                    {n} · {m.first || "?"} to {m.last || "?"}
                </span>
            </div>
            <div class="mono small ask-sample">{m.sample}</div>
            <div class="actions mt-xs">
                {onJourney && (
                    <AsyncButton class="btn sm primary" run={onJourney}>
                        Follow its journey
                    </AsyncButton>
                )}
                {m.first && m.last && (
                    <button
                        class="btn sm"
                        type="button"
                        title="These errors in the Logs view, over the time they were seen"
                        onClick={() => {
                            const pad = (s: string, by: number) =>
                                new Date(
                                    +new Date(s.replace(" ", "T")) + by,
                                ).toISOString();
                            void openInLogs({
                                instance,
                                query: m.exception
                                    ? drillQuery(ran, phrase(m.exception))
                                    : ran || "*",
                                from: pad(m.first!, -1000),
                                to: pad(m.last!, 1000),
                            });
                        }}
                    >
                        Its lines in Logs
                    </button>
                )}
            </div>
        </li>
    );
}

// A question in plain words, read into the Logs view: the reading as chips the user can change, the errors that
// fit, and the journey of a pasted code.
export function Ask() {
    const [mode, setMode] = useState("ask"),
        [question, setQuestion] = useState(""),
        [code, setCode] = useState(""),
        [answer, setAnswer] = useState<{
            asked: string;
            data: AskAnswer;
        } | null>(null),
        [reading, setReading] = useState<AskView | null>(null),
        [error, setError] = useState(""),
        [busy, setBusy] = useState(false),
        [shown, setShown] = useState<Shown | null>(null),
        [sentOpen, setSentOpen] = useState(false),
        seq = useRef(0);
    const ask = async (q: string, instance?: string) => {
        const mine = ++seq.current;
        setBusy(true);
        try {
            const data = await api<AskAnswer>("/api/ask", "POST", {
                question: q,
                ...(instance ? { instance } : {}),
            });
            if (mine !== seq.current) return;
            setAnswer({ asked: q, data });
            setReading(data.view);
            setError("");
            setShown(null);
            if (data.view) follow(data.view, true);
        } catch (e) {
            if (mine === seq.current) setError(errorText(e));
        } finally {
            if (mine === seq.current) setBusy(false);
        }
    };
    const refilter = async (next: AskView) => {
        const mine = ++seq.current;
        setReading(next);
        try {
            const f = await api<AskFilters>("/api/ask/filters", "POST", {
                instance: next.instance,
                errors: next.errors,
                terms: next.terms,
            });
            if (mine !== seq.current) return;
            const done = { ...next, query: f.query, notes: f.notes };
            setReading(done);
            follow(done, false);
        } catch (e) {
            if (mine === seq.current) toast(errorText(e), "fail");
        }
    };
    const journey = async (c: string, instance?: string) => {
        const v = view.value;
        try {
            const r = await api<{ result: Journey }>(
                "/api/ask/journey",
                "POST",
                {
                    code: c,
                    instance: instance || v.instance || undefined,
                    range: v.from && v.to ? "24h" : v.range || "24h",
                },
            );
            setShown({ kind: "journey", code: c, data: r.result });
        } catch (e) {
            setShown({ kind: "error", text: errorText(e) });
        }
    };
    const setTime = (choice: string) => {
        if (!reading || !answer?.data.view) return;
        const orig = answer.data.view,
            next =
                choice === AS_READ
                    ? {
                          ...reading,
                          range: orig.range,
                          from: orig.from,
                          to: orig.to,
                      }
                    : { ...reading, range: choice, from: "", to: "" };
        setReading(next);
        follow(next, false);
    };
    const r = answer?.data.result,
        sent = answer?.data.sent || [],
        u = r?.understood,
        where = reading?.instance || u?.instance || "",
        env = instances.value.find((e) => e.name === where),
        terms = (vocabulary.value?.terms || []).filter(
            (t) => applies(t, env) && !reading?.terms.includes(t.name),
        ),
        orig = answer?.data.view,
        timeChoice =
            reading && orig
                ? reading.from === orig.from &&
                  reading.to === orig.to &&
                  reading.range === orig.range
                    ? AS_READ
                    : reading.range
                : "",
        best = r?.matches?.[0],
        flowTrace = r?.flow?.trace_id;
    return (
        <div class="well ask" id="askBox">
            <div class="ask-top">
                <Segment
                    id="askMode"
                    label="Ask or paste a code"
                    options={[
                        ["ask", "Ask in plain words"],
                        ["code", "Paste a code"],
                    ]}
                    value={mode}
                    onChange={setMode}
                />
                <span class="spacer" />
                <span class="muted small" aria-live="polite">
                    {busy ? "Reading the question…" : ""}
                </span>
            </div>
            {mode === "ask" ? (
                <form
                    class="ask-form"
                    role="search"
                    aria-label="Ask in plain words"
                    onSubmit={(e) => e.preventDefault()}
                >
                    <input
                        type="search"
                        id="askQ"
                        maxLength={2000}
                        placeholder="Ask in plain words, e.g. thanh toán lỗi trong 15 phút qua ở prod"
                        aria-label="Question"
                        autoComplete="off"
                        value={question}
                        onInput={(e) => setQuestion(e.currentTarget.value)}
                    />
                    <AsyncButton
                        class="btn primary"
                        type="submit"
                        id="askGo"
                        disabled={!question.trim()}
                        run={() => ask(question.trim())}
                    >
                        Ask
                    </AsyncButton>
                </form>
            ) : (
                <form
                    class="ask-form"
                    role="search"
                    aria-label="Follow a code"
                    onSubmit={(e) => e.preventDefault()}
                >
                    <input
                        type="search"
                        id="askCode"
                        placeholder="An order code, transaction id or trace id, e.g. ORD-1001"
                        aria-label="Code"
                        autoComplete="off"
                        spellcheck={false}
                        value={code}
                        onInput={(e) => setCode(e.currentTarget.value)}
                    />
                    <AsyncButton
                        class="btn primary"
                        type="submit"
                        id="askFollow"
                        disabled={!code.trim()}
                        run={() => journey(code.trim())}
                    >
                        Follow
                    </AsyncButton>
                </form>
            )}
            {error && (
                <div class="ed-state bad mt-xs" role="alert">
                    {error}
                </div>
            )}
            {r && u && mode === "ask" && (
                <div class="ask-answer mt-sm">
                    {r.clarify && (
                        <div class="banner warn flush" role="status">
                            <Icon name="warn" />
                            <div class="text">
                                <b>{r.clarify}</b>
                                {!!r.options?.length && (
                                    <div class="actions mt-xs">
                                        {r.options.map((o) => (
                                            <AsyncButton
                                                key={o}
                                                class="btn sm"
                                                run={() =>
                                                    ask(answer!.asked, o)
                                                }
                                            >
                                                {o}
                                            </AsyncButton>
                                        ))}
                                    </div>
                                )}
                                {!!r.readings?.length && (
                                    <ul class="mt-xs">
                                        {r.readings.map((x) => (
                                            <li key={x.intent}>
                                                {x.what}:{" "}
                                                <span class="muted">
                                                    ask “{x.ask}”
                                                </span>
                                            </li>
                                        ))}
                                    </ul>
                                )}
                            </div>
                        </div>
                    )}
                    <div
                        class="chips ask-chips mt-xs"
                        aria-label="How the question was read"
                    >
                        <label class="chip">
                            <span class="muted">where</span>
                            <select
                                aria-label="Environment"
                                value={where}
                                onChange={(e) =>
                                    void ask(
                                        answer!.asked,
                                        e.currentTarget.value,
                                    )
                                }
                            >
                                {!where && (
                                    <option value="">
                                        choose an environment
                                    </option>
                                )}
                                {instances.value.map((e) => (
                                    <option value={e.name}>{label(e)}</option>
                                ))}
                            </select>
                        </label>
                        {reading && (
                            <label
                                class="chip"
                                title={
                                    "words" in u.time && u.time.words
                                        ? `read from “${u.time.words}”`
                                        : undefined
                                }
                            >
                                <span class="muted">when</span>
                                <select
                                    aria-label="Time range"
                                    value={timeChoice}
                                    onChange={(e) =>
                                        setTime(e.currentTarget.value)
                                    }
                                >
                                    {orig && (
                                        <option value={AS_READ}>
                                            {whenText(orig)}
                                        </option>
                                    )}
                                    {LOG_RANGES.map((x) => (
                                        <option value={x}>last {x}</option>
                                    ))}
                                </select>
                            </label>
                        )}
                        {reading && (
                            <label class="chip">
                                <span class="muted">what</span>
                                <select
                                    aria-label="Kind of error"
                                    value={reading.errors}
                                    onChange={(e) =>
                                        void refilter({
                                            ...reading,
                                            errors: e.currentTarget
                                                .value as ErrorKind,
                                        })
                                    }
                                >
                                    {ERRORS.map((k) => (
                                        <option value={k}>
                                            {ERROR_WORDS[k]}
                                        </option>
                                    ))}
                                </select>
                            </label>
                        )}
                        {reading?.terms.map((t) => {
                            const said = u.topics?.find((x) => x.term === t);
                            return (
                                <span
                                    class="chip ask-term"
                                    key={t}
                                    title={
                                        said?.matched
                                            ? `read from “${said.matched}”`
                                            : undefined
                                    }
                                >
                                    {t}
                                    <button
                                        type="button"
                                        aria-label={`Remove ${t}`}
                                        onClick={() =>
                                            void refilter({
                                                ...reading,
                                                terms: reading.terms.filter(
                                                    (x) => x !== t,
                                                ),
                                            })
                                        }
                                    >
                                        ×
                                    </button>
                                </span>
                            );
                        })}
                        {reading && terms.length > 0 && (
                            <select
                                class="sel auto-width"
                                aria-label="Add a term of the glossary"
                                value=""
                                onChange={(e) => {
                                    const t = e.currentTarget.value;
                                    e.currentTarget.value = "";
                                    if (t)
                                        void refilter({
                                            ...reading,
                                            terms: [...reading.terms, t],
                                        });
                                }}
                            >
                                <option value="">+ term</option>
                                {terms.map((t) => (
                                    <option value={t.name}>{t.name}</option>
                                ))}
                            </select>
                        )}
                    </div>
                    {reading?.notes.map((n) => (
                        <div class="hint mt-xs" key={n}>
                            {noteText(n)}
                        </div>
                    ))}
                    <p class="ask-summary mt-sm">{r.summary}</p>
                    {r.hint && <div class="hint">{r.hint}</div>}
                    {!!r.matches?.length && reading && (
                        <ul class="ask-matches mt-xs">
                            {r.matches.slice(0, 3).map((m) => (
                                <Match
                                    m={m}
                                    best={m === best}
                                    instance={reading.instance}
                                    ran={r.ran || reading.query}
                                    onJourney={
                                        m === best && flowTrace
                                            ? () =>
                                                  journey(
                                                      flowTrace,
                                                      reading.instance,
                                                  )
                                            : null
                                    }
                                />
                            ))}
                            {r.matches.length > 3 && (
                                <li class="muted small">
                                    and{" "}
                                    {plural(
                                        r.matches.length -
                                            3 +
                                            (r.matches_omitted || 0),
                                        "more group",
                                    )}
                                </li>
                            )}
                        </ul>
                    )}
                    {r.traces?.map((j) => (
                        <JourneyView key={j.trace_id} j={j} />
                    ))}
                    <div class="ask-cost mt-xs">
                        <span class="muted small">{costLine(r.cost)}</span>
                        <span class="spacer" />
                        {sent.length ? (
                            <button
                                class="btn sm ghost"
                                type="button"
                                id="askSentBtn"
                                onClick={() => setSentOpen(true)}
                            >
                                What was sent
                            </button>
                        ) : (
                            <span class="muted small">
                                Nothing was sent to a model provider
                            </span>
                        )}
                    </div>
                    <Sent
                        sent={sent}
                        open={sentOpen}
                        onClose={() => setSentOpen(false)}
                    />
                </div>
            )}
            {shown?.kind === "error" && (
                <div class="ed-state bad mt-xs" role="alert">
                    {shown.text}
                </div>
            )}
            {shown?.kind === "journey" && (
                <div class="mt-sm">
                    <JourneyView key={shown.code} j={shown.data} />
                </div>
            )}
        </div>
    );
}
