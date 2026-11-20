import { useState } from "preact/hooks";
import { api, errorText } from "../api";
import { loadState, runDoctor, toast } from "../store";
import type { BundleExport, BundlePreview } from "../scripts/types";
import { AsyncButton, plural } from "./UI";

function download(name: string, text: string) {
    const href = URL.createObjectURL(
            new Blob([text], { type: "application/toml" }),
        ),
        a = Object.assign(document.createElement("a"), {
            href,
            download: name,
        });
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(href), 1000);
}

function Paths({
    title,
    sign,
    paths,
}: {
    title: string;
    sign: string;
    paths: string[];
}) {
    if (!paths.length) return null;
    return (
        <details class="mt-xs" open={paths.length <= 12}>
            <summary>
                {title} ({paths.length})
            </summary>
            <ul class="mono small">
                {paths.map((p) => (
                    <li key={p}>
                        {sign} {p}
                    </li>
                ))}
            </ul>
        </details>
    );
}

// export the shared part of this config for the team, or import the bundle a teammate exported
export function TeamBundle() {
    const [name, setName] = useState(""),
        [description, setDescription] = useState(""),
        [exported, setExported] = useState<BundleExport | null>(null),
        [text, setText] = useState(""),
        [view, setView] = useState<BundlePreview | null>(null),
        [error, setError] = useState("");
    const forget = () => {
        setView(null);
        setError("");
    };
    return (
        <div class="card mt-sm">
            <div class="card-head">
                <h2>Team bundle</h2>
            </div>
            <p class="hint">
                Share environments, fields, error queries, redaction, presets,
                scan rules and the glossary with your team. A bundle never holds
                tokens, passwords, local paths or model providers.
            </p>
            <h3 class="mt-sm">Export</h3>
            <div class="field">
                <label class="label req" for="bundle_name">
                    Team name
                </label>
                <input
                    id="bundle_name"
                    placeholder="acme"
                    autoComplete="off"
                    spellcheck={false}
                    value={name}
                    onInput={(e) => setName(e.currentTarget.value)}
                />
            </div>
            <div class="field">
                <label class="label" for="bundle_description">
                    Description
                </label>
                <input
                    id="bundle_description"
                    placeholder="what the bundle is for"
                    value={description}
                    onInput={(e) => setDescription(e.currentTarget.value)}
                />
            </div>
            <AsyncButton
                class="btn sm"
                id="bundleExport"
                disabled={!name.trim()}
                run={async () => {
                    const q = new URLSearchParams({
                        name: name.trim(),
                        description,
                    });
                    try {
                        const r = await api<BundleExport>(
                            `/api/bundle/export?${q}`,
                        );
                        setExported(r);
                        download(r.filename, r.text);
                    } catch (e) {
                        toast(errorText(e), "fail");
                    }
                }}
            >
                Export and download
            </AsyncButton>
            {exported && exported.dropped.length > 0 && (
                <div class="hint mt-xs">
                    Left out as personal, secret or local:{" "}
                    <span class="mono">{exported.dropped.join(", ")}</span>
                </div>
            )}

            <h3 class="mt-sm">Import</h3>
            <div class="field">
                <label class="label" for="bundle_file">
                    Bundle file
                </label>
                <input
                    id="bundle_file"
                    type="file"
                    accept=".toml,text/plain"
                    onChange={async (e) => {
                        const file = e.currentTarget.files?.[0];
                        if (!file) return;
                        setText(await file.text());
                        forget();
                    }}
                />
            </div>
            <textarea
                rows={6}
                spellcheck={false}
                aria-label="Bundle text"
                placeholder="or paste the bundle here"
                value={text}
                onInput={(e) => {
                    setText(e.currentTarget.value);
                    forget();
                }}
            />
            <AsyncButton
                class="btn sm mt-xs"
                id="bundlePreview"
                disabled={!text.trim()}
                run={async () => {
                    try {
                        setView(
                            await api<BundlePreview>(
                                "/api/bundle/preview",
                                "POST",
                                { text },
                            ),
                        );
                        setError("");
                    } catch (e) {
                        setView(null);
                        setError(errorText(e));
                    }
                }}
            >
                Preview
            </AsyncButton>
            {error && (
                <div class="ed-state bad mt-xs" role="alert">
                    {error}
                </div>
            )}
            {view && (
                <div class="mt-xs" id="bundleView">
                    <div>
                        <b>{view.name}</b> goes to{" "}
                        <span class="mono small">{view.team_file}</span>:{" "}
                        {plural(view.added.length, "new setting")},{" "}
                        {view.changed.length} changed, {view.removed.length}{" "}
                        removed.
                    </div>
                    {view.security.length > 0 && (
                        <div class="ed-state bad mt-xs" role="alert">
                            Check these: they decide where a token or log text
                            goes.
                            <ul>
                                {view.security.map((s) => (
                                    <li key={s}>{s}</li>
                                ))}
                            </ul>
                        </div>
                    )}
                    <Paths title="New" sign="+" paths={view.added} />
                    <Paths title="Changed" sign="~" paths={view.changed} />
                    <Paths title="Removed" sign="-" paths={view.removed} />
                    <Paths
                        title="Your own config sets these differently, and yours stay in force"
                        sign="="
                        paths={view.shadowed}
                    />
                    {view.include_added && (
                        <div class="hint mt-xs">
                            <span class="mono">
                                include = "teams/{view.name}.toml"
                            </span>{" "}
                            will be added to{" "}
                            <span class="mono">{view.config_path}</span>.
                        </div>
                    )}
                    <AsyncButton
                        class="btn sm primary mt-xs"
                        id="bundleImport"
                        run={async () => {
                            try {
                                const r = await api<BundlePreview>(
                                    "/api/bundle/import",
                                    "POST",
                                    { text },
                                );
                                toast(
                                    r.logins.length
                                        ? `Imported. Save the tokens of: ${r.logins.join(", ")}`
                                        : "Imported",
                                );
                                setView(null);
                                setText("");
                                await loadState();
                                void runDoctor();
                            } catch (e) {
                                setError(errorText(e));
                            }
                        }}
                    >
                        Import
                    </AsyncButton>
                </div>
            )}
        </div>
    );
}
