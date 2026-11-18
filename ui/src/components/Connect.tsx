import { useEffect, useRef, useState } from "preact/hooks";
import { api, errorText } from "../api";
import { currentPage, navigate, state, toast } from "../store";
import type { ClientData, ClientSnippet } from "../scripts/types";
import { AsyncButton, Page, Tabs } from "./UI";
export function Connect() {
    const [source, setSource] = useState("git"),
        chosen = useRef(false),
        [data, setData] = useState<ClientData | null>(null),
        [error, setError] = useState(""),
        [loading, setLoading] = useState(false),
        [tab, setTab] = useState(""),
        [secrets, setSecrets] = useState(false),
        [done, setDone] = useState("");
    const tabs = (state.value?.clients || [])
            .flatMap((c) =>
                c.scopes.map((scope) => ({
                    key: c.key,
                    scope,
                    id: `${c.key}:${scope}`,
                    title:
                        c.scopes.length > 1 ? `${c.title} · ${scope}` : c.title,
                    note: c.note,
                })),
            )
            .sort(
                (a, b) =>
                    Number(b.key === "claude-code") -
                    Number(a.key === "claude-code"),
            ),
        selected = tabs.find((t) => t.id === tab) || tabs[0],
        snippet = selected
            ? (data?.[selected.key]?.[selected.scope] as
                  | ClientSnippet
                  | undefined)
            : undefined;
    useEffect(() => {
        if (currentPage.value !== "connect" || !state.value) return;
        let live = true;
        setLoading(true);
        setError("");
        void api<ClientData>(
            `/api/clients?source=${encodeURIComponent(source)}`,
        )
            .then((r) => {
                if (!live) return;
                if (
                    !chosen.current &&
                    r.shared.running &&
                    source !== "shared"
                ) {
                    setSource("shared");
                    return;
                }
                setData(r);
                setLoading(false);
            })
            .catch((e) => {
                if (live) {
                    setError(errorText(e));
                    setLoading(false);
                    toast(errorText(e), "fail");
                }
            });
        return () => {
            live = false;
        };
    }, [source, currentPage.value, !!state.value]);
    useEffect(() => {
        setSecrets(false);
        setDone("");
    }, [selected?.id, source]);
    const copy = async (text: string) => {
        await navigator.clipboard.writeText(text);
        toast("Copied");
    };
    return (
        <Page id="connect">
            <div class="card">
                <Tabs
                    id="c_tabs"
                    label="Client"
                    options={tabs.map((t) => [t.id, t.title] as const)}
                    value={selected?.id || ""}
                    onChange={setTab}
                />
                <ol class="steps">
                    <li class="step">
                        <div class="shead">
                            <span class="snum" aria-hidden="true">
                                1
                            </span>
                            <h2>How the server starts</h2>
                        </div>
                        <div class="field">
                            <label class="label" for="c_source">
                                The client starts the server with
                            </label>
                            <select
                                id="c_source"
                                value={source}
                                onChange={(e) => {
                                    chosen.current = true;
                                    setSource(e.currentTarget.value);
                                }}
                            >
                                <option value="git">
                                    uvx from GitHub (no release needed)
                                </option>
                                <option value="pypi">uvx from PyPI</option>
                                <option value="local">
                                    the installed graylog-mcp command
                                </option>
                                <option value="shared">
                                    the shared server already running
                                    (recommended)
                                </option>
                            </select>
                        </div>
                        <div class="hint" id="c_shared">
                            {error ? (
                                <>
                                    <span class="badge fail">not loaded</span>{" "}
                                    <span class="muted" role="alert">
                                        Could not load the client snippets:{" "}
                                        {error}. Leave this page and come back
                                        to try again.
                                    </span>
                                </>
                            ) : data?.shared.running ? (
                                <>
                                    <span class="badge ok">
                                        shared server running
                                    </span>{" "}
                                    <span class="mono">{data.shared.url}</span>{" "}
                                    (v{data.shared.version || "?"}): one process
                                    for every session
                                </>
                            ) : data ? (
                                <>
                                    <span class="badge">no shared server</span>{" "}
                                    <span class="muted">
                                        uvx starts one process per session.{" "}
                                        <a
                                            href="#home"
                                            onClick={(e) => {
                                                e.preventDefault();
                                                void navigate("home");
                                            }}
                                        >
                                            Finish setup
                                        </a>{" "}
                                        to share one.
                                    </span>
                                </>
                            ) : loading ? (
                                "Loading…"
                            ) : (
                                ""
                            )}
                        </div>
                        <div class="hint">
                            Tokens are never written into these files; the
                            server reads the ones saved on this machine.
                        </div>
                    </li>
                    <li class="step">
                        <div class="shead">
                            <span class="snum" aria-hidden="true">
                                2
                            </span>
                            <h2>The snippet</h2>
                            <span class="spacer" />
                            <AsyncButton
                                class="btn sm"
                                id="c_copy"
                                disabled={!snippet || loading || !!error}
                                run={() => copy(snippet?.snippet || "")}
                            >
                                Copy
                            </AsyncButton>
                        </div>
                        <div class="muted" id="c_note">
                            {selected?.note}
                        </div>
                        <div class="muted small mono anywhere" id="c_path">
                            {snippet?.path}
                        </div>
                        <pre id="c_snippet">{snippet?.snippet}</pre>
                    </li>
                    <li class="step">
                        <div class="shead">
                            <span class="snum" aria-hidden="true">
                                3
                            </span>
                            <h2>Install</h2>
                        </div>
                        <div class="muted small">
                            Writes the snippet into the file above, keeping
                            anything else that is in it.
                        </div>
                        <div id="c_actions" class="actions">
                            {selected?.key === "claude-desktop" && (
                                <label class="check small">
                                    <input
                                        type="checkbox"
                                        id="c_secrets"
                                        checked={secrets}
                                        onChange={(e) =>
                                            setSecrets(e.currentTarget.checked)
                                        }
                                    />
                                    copy token values
                                </label>
                            )}
                            <AsyncButton
                                class="btn primary"
                                id="c_install"
                                disabled={
                                    !selected || !snippet || loading || !!error
                                }
                                run={async () => {
                                    if (!selected) return;
                                    const r = await api<{
                                        replaced: boolean;
                                        path: string;
                                    }>("/api/clients/install", "POST", {
                                        client: selected.key,
                                        scope: selected.scope,
                                        source,
                                        with_secrets: secrets,
                                    });
                                    setDone(
                                        `${r.replaced ? "Updated" : "Added"} in ${r.path}`,
                                    );
                                }}
                            >
                                Install
                            </AsyncButton>
                            <span
                                class="muted small"
                                id="c_done"
                                aria-live="polite"
                            >
                                {done}
                            </span>
                        </div>
                    </li>
                </ol>
            </div>
            <div
                class="card"
                id="ccCard"
                hidden={selected?.key !== "claude-code"}
            >
                <div class="card-head">
                    <h2>Claude Code, every project</h2>
                    <span class="spacer" />
                    <AsyncButton
                        class="btn sm"
                        id="ccCopy"
                        disabled={!data || loading || !!error}
                        run={() => copy(data?.["claude-code-command"] || "")}
                    >
                        Copy
                    </AsyncButton>
                </div>
                <pre id="ccCommand">{data?.["claude-code-command"]}</pre>
            </div>
            <TraceCard hidden={selected?.key !== "claude-code"} />
        </Page>
    );
}
type TraceHooks = { enabled: boolean; path: string };
function TraceCard({ hidden }: { hidden: boolean }) {
    const [hooks, setHooks] = useState<TraceHooks | null>(null),
        [error, setError] = useState("");
    useEffect(() => {
        if (currentPage.value !== "connect" || !state.value) return;
        let live = true;
        void api<TraceHooks>("/api/setup/trace")
            .then((r) => {
                if (live) {
                    setHooks(r);
                    setError("");
                }
            })
            .catch((e) => {
                if (live) setError(errorText(e));
            });
        return () => {
            live = false;
        };
    }, [currentPage.value, !!state.value]);
    return (
        <div class="card" id="trCard" hidden={hidden}>
            <div class="card-head">
                <h2>Group calls by conversation</h2>
                <span class="spacer" />
                <span id="trState">
                    {error ? (
                        <span class="badge fail">not loaded</span>
                    ) : hooks ? (
                        <span class={hooks.enabled ? "badge ok" : "badge"}>
                            {hooks.enabled ? "on" : "off"}
                        </span>
                    ) : null}
                </span>
                <AsyncButton
                    class="btn sm"
                    id="trToggle"
                    disabled={!hooks}
                    run={async () => {
                        if (!hooks) return;
                        const r = await api<TraceHooks>(
                            "/api/setup/trace",
                            "POST",
                            { enabled: !hooks.enabled },
                        );
                        setHooks(r);
                        toast(
                            r.enabled
                                ? "Hooks added: new sessions are grouped by conversation"
                                : "Hooks taken out",
                        );
                    }}
                >
                    {hooks?.enabled ? "Turn off" : "Turn on"}
                </AsyncButton>
            </div>
            <div class="muted small">
                Claude Code does not tell the server which conversation a call
                belongs to. Two hooks in your Claude Code settings do: they
                record the session, the subagent and when you asked, as hashed
                ids (never the prompt's text), so Activity groups calls by
                conversation and turn. Applies to sessions started afterwards.
            </div>
            <div class="muted small mono anywhere mt-xs" id="trPath">
                {error || hooks?.path}
            </div>
        </div>
    );
}
