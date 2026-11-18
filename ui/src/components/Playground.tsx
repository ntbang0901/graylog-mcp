import { useEffect, useLayoutEffect, useRef, useState } from "preact/hooks";
import { api, errorText } from "../api";
import { currentEnv, instances, playground, state, toast } from "../store";
import type { Fields, Json, RunResult } from "../scripts/types";
import { REDUCED } from "../scripts/motion";
import {
    AsyncButton,
    Page,
    Segment,
    modKey,
    num,
    plural,
    whenShort,
} from "./UI";
import { Marks, maskRegex } from "./Marks";
import { JsonTree } from "./JsonTree";
interface Run {
    tool: string;
    instance: string;
    args: Fields;
    at: number;
    result?: Json;
    chars?: number;
    tokens?: number;
    ms?: number;
    error?: string;
}
export function Playground() {
    const [tool, setTool] = useState("root_cause"),
        [env, setEnv] = useState(""),
        [args, setArgs] = useState(""),
        [validate, setValidate] = useState(false),
        [view, setView] = useState("pretty"),
        [history, setHistory] = useState<Run[]>([]),
        [result, setResult] = useState<Run | null>(null),
        [collapsed, setCollapsed] = useState(false),
        ta = useRef<HTMLTextAreaElement>(null),
        gut = useRef<HTMLDivElement>(null),
        tree = useRef<HTMLDivElement>(null),
        resultRef = useRef<HTMLDivElement>(null),
        tabFree = useRef(false),
        request = playground.value;
    const template = (t: string) => state.value?.tool_templates[t] || {},
        chooseTool = (t: string) => {
            setTool(t);
            setArgs(JSON.stringify(template(t), null, 2));
            setValidate(false);
        };
    useEffect(() => {
        if (state.value) {
            if (!state.value.tools.includes(tool))
                chooseTool(state.value.tools[0] || "root_cause");
            else if (!args) setArgs(JSON.stringify(template(tool), null, 2));
            if (!instances.value.some((e) => e.name === env))
                setEnv(
                    (
                        instances.value.find((e) => e.default) ||
                        instances.value[0]
                    )?.name || "",
                );
        }
    }, [state.value]);
    useEffect(() => {
        if (
            currentEnv.value &&
            instances.value.some((e) => e.name === currentEnv.value)
        )
            setEnv(currentEnv.value);
    }, [currentEnv.value]);
    useLayoutEffect(() => {
        if (!request) return;
        setTool(request.tool);
        setArgs(
            JSON.stringify(request.args || template(request.tool), null, 2),
        );
        setValidate(false);
        if (
            request.instance &&
            instances.value.some((e) => e.name === request.instance)
        )
            setEnv(request.instance);
        ta.current?.focus();
    }, [request]);
    function check():
        | { ok: true; args: Fields }
        | { ok: false; error: string } {
        if (!args.trim()) return { ok: true, args: {} };
        try {
            const v: unknown = JSON.parse(args);
            if (v === null || typeof v !== "object" || Array.isArray(v))
                return { ok: false, error: "must be a JSON object" };
            return { ok: true, args: v as Fields };
        } catch (e) {
            return {
                ok: false,
                error: errorText(e).replace(/^JSON\.parse: /, ""),
            };
        }
    }
    const valid = check(),
        bad = validate && !valid.ok;
    const run = async () => {
        setValidate(true);
        const chk = check();
        if (!chk.ok) {
            ta.current?.focus();
            throw new Error(`Arguments are not valid JSON: ${chk.error}`);
        }
        const t0 = performance.now(),
            entry: Run = {
                tool,
                instance: env,
                args: chk.args,
                at: Date.now() / 1000,
            };
        try {
            const r = await api<RunResult>("/api/run", "POST", {
                tool,
                instance: env,
                args: chk.args,
            });
            Object.assign(entry, {
                result: r.result,
                chars: r.chars,
                tokens: r.approx_tokens,
                ms: r.ms,
            });
        } catch (e) {
            Object.assign(entry, {
                error: errorText(e),
                ms: Math.round(performance.now() - t0),
            });
        }
        setHistory((h) => [entry, ...h].slice(0, 20));
        setResult(entry);
        setCollapsed(false);
        setTimeout(
            () =>
                resultRef.current?.scrollIntoView({
                    block: "nearest",
                    behavior: REDUCED.matches ? "auto" : "smooth",
                }),
            0,
        );
    };
    const text =
            result?.result !== undefined
                ? JSON.stringify(result.result, null, 2)
                : "",
        masked = (text.match(maskRegex()) || []).length;
    return (
        <Page id="playground">
            <div class="pg">
                <div class="pg-req">
                    <div class="well">
                        <div class="chips mb" id="p_quick">
                            {state.value?.quick_tools
                                .filter((t) => state.value?.tools.includes(t))
                                .map((t) => (
                                    <button
                                        class="chip"
                                        aria-pressed={tool === t}
                                        onClick={() => chooseTool(t)}
                                    >
                                        {t}
                                    </button>
                                ))}
                        </div>
                        <div class="row">
                            <div class="field">
                                <label class="label" for="p_env">
                                    Environment
                                </label>
                                <select
                                    id="p_env"
                                    value={env}
                                    onChange={(e) =>
                                        setEnv(e.currentTarget.value)
                                    }
                                >
                                    {instances.value.map((e) => (
                                        <option value={e.name}>
                                            {e.group
                                                ? `${e.group}/${e.environment}`
                                                : e.name}
                                        </option>
                                    ))}
                                </select>
                            </div>
                            <div class="field">
                                <label class="label" for="p_tool">
                                    Tool
                                </label>
                                <select
                                    id="p_tool"
                                    value={tool}
                                    onChange={(e) =>
                                        chooseTool(e.currentTarget.value)
                                    }
                                >
                                    {state.value?.tools.map((t) => (
                                        <option value={t}>{t}</option>
                                    ))}
                                </select>
                            </div>
                        </div>
                        <div class="field mt-sm">
                            <div class="ed-head">
                                <label class="label" for="p_args">
                                    Arguments (JSON)
                                </label>
                                <span class="spacer" />
                                <span
                                    class={`ed-state${bad ? " bad" : ""}`}
                                    id="p_valid"
                                    role="status"
                                >
                                    {bad && !valid.ok
                                        ? valid.error
                                        : validate && args.trim()
                                          ? "valid JSON"
                                          : ""}
                                </span>
                            </div>
                            <div class={`ed${bad ? " bad" : ""}`}>
                                <div
                                    class="gut"
                                    id="p_gut"
                                    ref={gut}
                                    aria-hidden="true"
                                >
                                    {Array.from(
                                        { length: args.split("\n").length },
                                        (_, i) => i + 1,
                                    ).join("\n")}
                                </div>
                                <textarea
                                    ref={ta}
                                    id="p_args"
                                    rows={9}
                                    wrap="off"
                                    spellcheck={false}
                                    autoCapitalize="off"
                                    autoComplete="off"
                                    value={args}
                                    aria-invalid={bad}
                                    onInput={(e) =>
                                        setArgs(e.currentTarget.value)
                                    }
                                    onScroll={(e) => {
                                        if (gut.current)
                                            gut.current.scrollTop =
                                                e.currentTarget.scrollTop;
                                    }}
                                    onBlur={() => setValidate(true)}
                                    onKeyDown={(ev) => {
                                        if (ev.key === "Escape") {
                                            tabFree.current = true;
                                            ev.stopPropagation();
                                            return;
                                        }
                                        if (
                                            ev.key !== "Tab" ||
                                            ev.ctrlKey ||
                                            ev.metaKey ||
                                            ev.altKey
                                        ) {
                                            tabFree.current = false;
                                            return;
                                        }
                                        if (tabFree.current) {
                                            tabFree.current = false;
                                            return;
                                        }
                                        ev.preventDefault();
                                        const el = ev.currentTarget,
                                            a = el.selectionStart,
                                            b = el.selectionEnd;
                                        if (a === b && !ev.shiftKey)
                                            el.setRangeText("  ", a, b, "end");
                                        else {
                                            const from =
                                                    el.value.lastIndexOf(
                                                        "\n",
                                                        a - 1,
                                                    ) + 1,
                                                out = el.value
                                                    .slice(from, b)
                                                    .split("\n")
                                                    .map((l) =>
                                                        ev.shiftKey
                                                            ? l.replace(
                                                                  /^ {1,2}/,
                                                                  "",
                                                              )
                                                            : "  " + l,
                                                    )
                                                    .join("\n");
                                            el.setRangeText(
                                                out,
                                                from,
                                                b,
                                                "select",
                                            );
                                            el.selectionStart = from;
                                        }
                                        setArgs(el.value);
                                    }}
                                />
                            </div>
                            <div class="hint">
                                Tab indents · <span class="kbd">Esc</span> then
                                Tab leaves the editor · the environment above is
                                used unless the arguments name an{" "}
                                <span class="mono">instance</span>
                            </div>
                        </div>
                        <div class="row mt-sm pg-actions">
                            <AsyncButton
                                class="btn primary"
                                id="runTool"
                                run={run}
                            >
                                Run{" "}
                                <span class="kbd" id="runKbd">
                                    {modKey()} ↵
                                </span>
                            </AsyncButton>
                            <button
                                class="btn"
                                id="p_format"
                                onClick={() => {
                                    setValidate(true);
                                    if (valid.ok)
                                        setArgs(
                                            JSON.stringify(valid.args, null, 2),
                                        );
                                    ta.current?.focus();
                                }}
                            >
                                Format
                            </button>
                            <button
                                class="btn"
                                id="p_reset"
                                onClick={() => {
                                    chooseTool(tool);
                                    ta.current?.focus();
                                }}
                            >
                                Reset to template
                            </button>
                        </div>
                    </div>
                    <div class="card" id="p_histCard">
                        <div class="card-head">
                            <h2>This session</h2>
                            <span class="spacer" />
                            <span class="muted small" id="p_histN">
                                {history.length
                                    ? `${history.length} of 20`
                                    : ""}
                            </span>
                        </div>
                        <div
                            class={`muted small${history.length ? " hidden" : ""}`}
                            id="p_histEmpty"
                        >
                            Runs from this visit are listed here. Reload and
                            they are gone; the model's own calls are in
                            Activity.
                        </div>
                        <div class="hist" id="p_hist">
                            {history.map((h) => (
                                <button
                                    class="hrow2"
                                    aria-current={
                                        h === result ? "true" : "false"
                                    }
                                    onClick={() => {
                                        setTool(h.tool);
                                        setEnv(h.instance);
                                        setArgs(
                                            JSON.stringify(h.args, null, 2),
                                        );
                                        setValidate(false);
                                        setResult(h);
                                        setCollapsed(false);
                                    }}
                                >
                                    <span
                                        class={`badge ${h.error ? "fail" : "ok"}`}
                                    >
                                        {h.error ? "failed" : "ok"}
                                    </span>
                                    <span class="ht">
                                        {h.tool} · {h.instance || "default"}
                                    </span>
                                    <span class="hw">
                                        {h.ms} ms · {whenShort(h.at)}
                                    </span>
                                </button>
                            ))}
                        </div>
                    </div>
                </div>
                <div class="pg-res">
                    <div class="card" id="p_result" ref={resultRef}>
                        <div class="card-head">
                            <h2>Result</h2>
                            <span class="spacer" />
                            {result && !result.error && (
                                <Segment
                                    id="p_view"
                                    label="Result view"
                                    options={[
                                        ["pretty", "Pretty"],
                                        ["raw", "Raw"],
                                    ]}
                                    value={view}
                                    onChange={(v) => {
                                        setView(v);
                                        setCollapsed(false);
                                    }}
                                />
                            )}
                            <button
                                class={`btn sm${!result || result.error || view !== "pretty" ? " hidden" : ""}`}
                                id="p_collapse"
                                title="Collapse or expand every level"
                                onClick={() => {
                                    const all = [
                                            ...(tree.current?.querySelectorAll(
                                                "details",
                                            ) || []),
                                        ],
                                        anyOpen = all.some(
                                            (d, i) => i > 0 && d.open,
                                        );
                                    all.forEach((d, i) => {
                                        d.open = i === 0 || !anyOpen;
                                    });
                                    setCollapsed(anyOpen);
                                }}
                            >
                                {collapsed ? "Expand all" : "Collapse all"}
                            </button>
                            <AsyncButton
                                class={`btn sm${result ? "" : " hidden"}`}
                                id="p_copy"
                                run={async () => {
                                    if (result) {
                                        await navigator.clipboard.writeText(
                                            result.error || text,
                                        );
                                        toast("Copied");
                                    }
                                }}
                            >
                                Copy
                            </AsyncButton>
                        </div>
                        <div class="pmeta mono" id="p_meta" aria-live="polite">
                            {result &&
                                (result.error ? (
                                    <>
                                        <span class="bad">failed</span> ·{" "}
                                        {result.tool} on{" "}
                                        {result.instance || "default"} ·{" "}
                                        {result.ms} ms
                                    </>
                                ) : (
                                    <>
                                        {result.tool} ·{" "}
                                        {result.instance || "default"} ·{" "}
                                        {result.ms} ms · {num(result.chars)}{" "}
                                        chars (~{num(result.tokens)} tokens) ·{" "}
                                        {masked
                                            ? plural(masked, "masked value")
                                            : "nothing masked"}
                                    </>
                                ))}
                        </div>
                        <div
                            class={`pempty${result ? " hidden" : ""}`}
                            id="p_empty"
                        >
                            Run a tool to see what the model would see: the same
                            redaction, the same size limit.
                        </div>
                        <div
                            class={`tree${!result || (!result.error && view !== "pretty") ? " hidden" : ""}`}
                            id="p_tree"
                            ref={tree}
                        >
                            {result?.error ? (
                                <div class="perr">{result.error}</div>
                            ) : (
                                result?.result !== undefined && (
                                    <JsonTree
                                        key={
                                            history.indexOf(result) + ":" + view
                                        }
                                        value={result.result}
                                    />
                                )
                            )}
                        </div>
                        <pre
                            class={
                                !result || result.error || view !== "raw"
                                    ? "hidden"
                                    : ""
                            }
                            id="p_out"
                        >
                            <Marks text={text} />
                        </pre>
                    </div>
                </div>
            </div>
        </Page>
    );
}
