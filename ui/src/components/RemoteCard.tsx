import { useEffect, useState } from "preact/hooks";
import { api, errorText } from "../api";
import { waitFor } from "../setup";
import { currentPage, state, toast } from "../store";
import { AsyncButton } from "./UI";

type Paired = {
    client_id: string;
    name: string;
    paired: number;
    used: number | null;
    active: boolean;
};
type Environment = { name: string; description: string; editable: boolean };
type Remote = {
    enabled: boolean;
    running?: boolean;
    error: string | null;
    public_url?: string;
    port: number;
    mcp_url?: string | null;
    instances: string[];
    environments: Environment[];
    restarts: boolean;
    repository_file?: boolean;
    clients: Paired[];
};
type Saved = { ok: true; restarting: boolean };

const when = (t: number | null) =>
    t ? new Date(t * 1000).toLocaleString() : "never";

function badge(r: Remote) {
    if (!r.enabled) return <span class="badge">off</span>;
    if (r.running) return <span class="badge ok">on</span>;
    return (
        <span
            class="badge warn"
            title="Turned on in the config, but nothing answers on its port: restart the server, or see its log"
        >
            on, not listening
        </span>
    );
}

// Turning the channel on or off: the tunnel's address, its port and the environments ChatGPT may read.
function RemoteForm({
    r,
    onSaved,
    onCancel,
}: {
    r: Remote;
    onSaved: (enabled: boolean, saved: Saved) => Promise<void>;
    onCancel?: () => void;
}) {
    const [url, setUrl] = useState(r.public_url || ""),
        [port, setPort] = useState(String(r.port)),
        [offered, setOffered] = useState<string[]>(r.instances);
    const save = async () => {
        const saved = await api<Saved>("/api/remote/settings", "POST", {
            enabled: true,
            public_url: url.trim(),
            port: Number(port) || undefined,
            instances: offered,
        });
        await onSaved(true, saved);
    };
    return (
        <form class="mt-xs" onSubmit={(e) => e.preventDefault()}>
            <div class="muted small">
                ChatGPT reaches only a server on the internet. Run a tunnel you
                are allowed to use (cloudflared, ngrok) that forwards to the
                port below, and paste its https address. Each ChatGPT account
                still needs a pairing code made on this machine.
            </div>
            <div class="row mt-xs">
                <div class="field">
                    <label class="label req" for="rcUrl">
                        Tunnel address
                    </label>
                    <input
                        id="rcUrl"
                        placeholder="https://logs-you.example.com"
                        autoComplete="off"
                        spellcheck={false}
                        value={url}
                        onInput={(e) => setUrl(e.currentTarget.value)}
                    />
                </div>
                <div class="field">
                    <label class="label" for="rcPort">
                        Port the tunnel forwards to
                    </label>
                    <input
                        id="rcPort"
                        inputMode="numeric"
                        value={port}
                        onInput={(e) => setPort(e.currentTarget.value)}
                    />
                </div>
            </div>
            <div class="field mt-sm">
                <span class="label req">Environments ChatGPT may read</span>
                <div class="chips">
                    {r.environments.map((e) => (
                        <label
                            class="chip"
                            key={e.name}
                            title={
                                e.editable
                                    ? e.description
                                    : "Defined in another file: offer it there"
                            }
                        >
                            <input
                                type="checkbox"
                                disabled={!e.editable}
                                checked={offered.includes(e.name)}
                                onChange={(ev) =>
                                    setOffered(
                                        ev.currentTarget.checked
                                            ? [...offered, e.name]
                                            : offered.filter(
                                                  (n) => n !== e.name,
                                              ),
                                    )
                                }
                            />
                            {e.name}
                        </label>
                    ))}
                </div>
                <div class="hint">
                    Only these are offered to ChatGPT; the admin page never is.
                </div>
            </div>
            <div class="row mt-sm">
                <AsyncButton
                    class="btn primary sm"
                    id="rcOn"
                    disabled={!url.trim() || !offered.length}
                    run={save}
                >
                    {r.enabled ? "Save" : "Turn on"}
                </AsyncButton>
                {onCancel && (
                    <button type="button" class="btn sm" onClick={onCancel}>
                        Cancel
                    </button>
                )}
            </div>
        </form>
    );
}

// ChatGPT web over the remote channel: the code shown here is typed on the page ChatGPT opens.
export function RemoteCard() {
    const [r, setR] = useState<Remote | null>(null),
        [error, setError] = useState(""),
        [editing, setEditing] = useState(false),
        [code, setCode] = useState<{ code: string; expires: number } | null>(
            null,
        );
    const load = async () => {
        try {
            const got = await api<Remote>("/api/remote");
            setR(got);
            setError("");
            return got;
        } catch (e) {
            setError(errorText(e));
            return null;
        }
    };
    useEffect(() => {
        if (currentPage.value === "connect" && state.value) void load();
    }, [currentPage.value, !!state.value]);
    const unpair = async (body: object, done: string) => {
        await api("/api/remote/revoke", "POST", body);
        toast(done);
        await load();
    };
    const saved = async (enabled: boolean, s: Saved) => {
        setEditing(false);
        if (!s.restarting) {
            toast("Saved. Restart the server to apply it", "info");
            await load();
            return;
        }
        toast("Saved. Restarting the server…", "info");
        const back = await waitFor(async () => {
            const got = await api<Remote>("/api/remote");
            return got.enabled === enabled && !!got.running === enabled;
        }, 60);
        await load();
        if (back)
            toast(enabled ? "ChatGPT web is on" : "ChatGPT web is off", "ok");
        else
            toast(
                "The server did not come back with the channel; see its log",
                "fail",
            );
    };
    const turnOff = async () => {
        const s = await api<Saved>("/api/remote/settings", "POST", {
            enabled: false,
            instances: r?.instances || [],
        });
        await saved(false, s);
    };
    return (
        <div class="card" id="rcCard">
            <div class="card-head">
                <h2>ChatGPT web</h2>
                <span class="spacer" />
                {error ? (
                    <span class="badge fail">not loaded</span>
                ) : r ? (
                    badge(r)
                ) : null}
            </div>
            {error && <div class="inline-msg">{error}</div>}
            {r?.repository_file && (
                <div class="muted small">
                    This page edits a repository's own config file, which may
                    not open the channel. Turn it on from the admin page of your
                    own server (graylog-mcp start).
                </div>
            )}
            {r && !r.repository_file && (!r.enabled || editing) && (
                <RemoteForm
                    r={r}
                    onSaved={saved}
                    onCancel={editing ? () => setEditing(false) : undefined}
                />
            )}
            {r?.enabled && !editing && (
                <>
                    <div class="muted small">
                        In ChatGPT, add a connector with the address{" "}
                        <b class="mono anywhere">{r.mcp_url}</b> and OAuth. It
                        opens a page asking for a pairing code: make one here
                        (one use, 10 minutes). Environments offered:{" "}
                        <b>{r.instances.join(", ") || "none"}</b>. Your tunnel
                        forwards to port <b class="mono">{r.port}</b>.
                    </div>
                    <div class="row mt-xs">
                        <AsyncButton
                            class="btn sm"
                            id="rcCode"
                            run={async () => {
                                setCode(
                                    await api<{
                                        code: string;
                                        expires: number;
                                    }>("/api/remote/code", "POST", {}),
                                );
                            }}
                        >
                            New pairing code
                        </AsyncButton>
                        {code && (
                            <span>
                                <b class="mono" id="rcShown">
                                    {code.code}
                                </b>{" "}
                                <span class="muted small">
                                    until{" "}
                                    {new Date(
                                        code.expires * 1000,
                                    ).toLocaleTimeString()}
                                </span>
                            </span>
                        )}
                        <span class="spacer" />
                        {!r.repository_file && (
                            <>
                                <button
                                    type="button"
                                    class="btn sm"
                                    id="rcEdit"
                                    onClick={() => setEditing(true)}
                                >
                                    Change
                                </button>
                                <AsyncButton
                                    class="btn sm"
                                    id="rcOff"
                                    run={turnOff}
                                >
                                    Turn off
                                </AsyncButton>
                            </>
                        )}
                    </div>
                </>
            )}
            {!!r?.clients.length && (
                <table class="mt-xs">
                    <thead>
                        <tr>
                            <th>Paired client</th>
                            <th>Paired</th>
                            <th>Last used</th>
                            <th />
                        </tr>
                    </thead>
                    <tbody>
                        {r.clients.map((c) => (
                            <tr key={c.client_id}>
                                <td>
                                    {c.name}
                                    {!c.active && (
                                        <span class="muted small">
                                            {" "}
                                            (signed out)
                                        </span>
                                    )}
                                </td>
                                <td>{when(c.paired)}</td>
                                <td>{when(c.used)}</td>
                                <td>
                                    <AsyncButton
                                        class="btn sm"
                                        run={() =>
                                            unpair(
                                                { client_id: c.client_id },
                                                `${c.name} unpaired`,
                                            )
                                        }
                                    >
                                        Unpair
                                    </AsyncButton>
                                </td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            )}
            {(r?.clients.length || 0) > 1 && (
                <AsyncButton
                    class="btn sm mt-xs"
                    run={() => unpair({ all: true }, "Every client unpaired")}
                >
                    Unpair all
                </AsyncButton>
            )}
        </div>
    );
}
