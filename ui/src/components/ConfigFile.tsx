import { useEffect, useRef, useState } from "preact/hooks";
import { api, errorText } from "../api";
import {
    currentPage,
    dirty,
    loadState,
    runDoctor,
    toast,
    state,
} from "../store";
import type { RawConfig } from "../scripts/types";
import { AsyncButton, Page, modKey, plural } from "./UI";
interface Validation {
    ok: boolean;
    error?: string;
    instances?: unknown[];
}
export function ConfigFile() {
    const [text, setText] = useState(""),
        [path, setPath] = useState(""),
        [status, setStatus] = useState(""),
        [error, setError] = useState(""),
        [errorLine, setErrorLine] = useState(0),
        [ready, setReady] = useState(false),
        loaded = useRef(""),
        ta = useRef<HTMLTextAreaElement>(null),
        gut = useRef<HTMLDivElement>(null),
        seq = useRef(0);
    const load = async () => {
        const id = ++seq.current;
        setReady(false);
        setError("");
        setStatus("");
        setErrorLine(0);
        try {
            const r = await api<RawConfig>("/api/config");
            if (id !== seq.current) return;
            setPath(r.path);
            loaded.current = r.text || "";
            setText(
                r.text ||
                    "# No config file yet. Add an environment, or paste a config here and save.\n",
            );
            setReady(true);
        } catch (e) {
            if (id !== seq.current) return;
            setText("");
            loaded.current = "";
            setError(
                `Could not read the config file: ${errorText(e)}. Leave this page and come back to try again.`,
            );
        }
    };
    useEffect(() => {
        if (currentPage.value === "config" && state.value) void load();
        return () => {
            seq.current++;
        };
    }, [currentPage.value, !!state.value]);
    const displayResult = (r: Validation, source: string, jump = true) => {
        let line = 0;
        setError("");
        if (r.ok)
            setStatus(
                `valid, ${plural(r.instances?.length || 0, "environment")}`,
            );
        else {
            const m = /\bline (\d+)/i.exec(r.error || "");
            line = m ? Math.min(+m[1], source.split("\n").length) : 0;
            setStatus("invalid");
            setError(r.error || "Invalid config");
        }
        setErrorLine(line);
        if (jump && line && ta.current) {
            const lines = source.split("\n"),
                from =
                    lines.slice(0, line - 1).join("\n").length +
                    (line > 1 ? 1 : 0);
            ta.current.focus();
            ta.current.setSelectionRange(from, from + lines[line - 1].length);
            ta.current.scrollTop = Math.max(0, (line - 4) * 20);
            if (gut.current) gut.current.scrollTop = ta.current.scrollTop;
        }
    };
    const change = (value: string) => {
        if (!ready) return;
        setText(value);
        setError("");
        setStatus("");
        setErrorLine(0);
        if (value === loaded.current) {
            dirty.value = null;
            return;
        }
        dirty.value = {
            what: "the config file",
            save: async () => {
                const v = await api<Validation>(
                    "/api/config/validate",
                    "POST",
                    { text: value },
                );
                displayResult(v, value);
                if (!v.ok)
                    throw new Error(
                        "The config file is not valid, so it was not saved",
                    );
                const r = await api<{ backup?: string }>(
                    "/api/config",
                    "POST",
                    { text: value },
                );
                toast(
                    `Saved${r.backup ? " (previous version kept as .bak)" : ""}`,
                );
                loaded.current = value;
                dirty.value = null;
                setStatus("saved");
                await loadState();
                void runDoctor();
            },
            discard: () => {
                void load();
            },
        };
    };
    return (
        <Page id="config">
            <div class="card">
                <div class="card-head">
                    <h2 class="mono small anywhere raw-path" id="rawPath">
                        {path}
                    </h2>
                    <span class="spacer" />
                    <span id="rawState">
                        {status && (
                            <span
                                class={`badge ${status === "invalid" ? "fail" : "ok"}`}
                            >
                                {status}
                            </span>
                        )}
                    </span>
                    <AsyncButton
                        class="btn sm"
                        id="validateRaw"
                        disabled={!ready}
                        run={async () => {
                            const r = await api<Validation>(
                                "/api/config/validate",
                                "POST",
                                { text },
                            );
                            displayResult(r, text);
                            if (!r.ok)
                                toast(r.error || "Invalid config", "fail");
                        }}
                    >
                        Validate
                    </AsyncButton>
                </div>
                <div class="ed tall">
                    <div class="gut" id="raw_gut" ref={gut} aria-hidden="true">
                        {Array.from(
                            { length: text.split("\n").length },
                            (_, i) => (
                                <span
                                    class={`gl${i + 1 === errorLine ? " err" : ""}`}
                                >
                                    {i + 1}
                                </span>
                            ),
                        )}
                    </div>
                    <textarea
                        ref={ta}
                        id="rawText"
                        rows={28}
                        wrap="off"
                        spellcheck={false}
                        autoCapitalize="off"
                        aria-label="Config file"
                        aria-describedby="rawErr"
                        value={text}
                        readOnly={!ready}
                        placeholder={
                            !ready && !error ? "Loading the config file…" : ""
                        }
                        onInput={(e) => change(e.currentTarget.value)}
                        onScroll={(e) => {
                            if (gut.current)
                                gut.current.scrollTop =
                                    e.currentTarget.scrollTop;
                        }}
                    />
                </div>
                <div
                    class={`ed-state${error ? " bad" : ""} mt-xs`}
                    id="rawErr"
                    role="alert"
                >
                    {error}
                </div>
                <div class="hint mt-xs">
                    <span class="kbd">{modKey()} S</span> checks the file and
                    saves it when it is valid. The previous version is kept as a{" "}
                    <span class="mono">.bak</span>.
                </div>
            </div>
        </Page>
    );
}
