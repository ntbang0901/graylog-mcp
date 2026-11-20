import { useEffect, useState } from "preact/hooks";
import { api, errorText } from "../api";
import { currentPage, state, toast } from "../store";
import { AsyncButton } from "./UI";

type Paired = {
    client_id: string;
    name: string;
    paired: number;
    used: number | null;
    active: boolean;
};
type Remote = {
    enabled: boolean;
    error: string | null;
    public_url?: string;
    port?: number;
    mcp_url?: string | null;
    instances: string[];
    clients: Paired[];
};

const SNIPPET = `[http.oauth]
enabled = true
public_url = "https://logs-you.example.com"  # your tunnel's address
# port = 8766                                 # the port the tunnel forwards to

[instances.dev]
remote_access = true                          # each environment ChatGPT may read`;

const when = (t: number | null) =>
    t ? new Date(t * 1000).toLocaleString() : "never";

// ChatGPT web over the remote channel: the code shown here is typed on the page ChatGPT opens.
export function RemoteCard() {
    const [r, setR] = useState<Remote | null>(null),
        [error, setError] = useState(""),
        [code, setCode] = useState<{ code: string; expires: number } | null>(
            null,
        );
    const load = async () => {
        try {
            setR(await api<Remote>("/api/remote"));
            setError("");
        } catch (e) {
            setError(errorText(e));
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
    return (
        <div class="card" id="rcCard">
            <div class="card-head">
                <h2>ChatGPT web</h2>
                <span class="spacer" />
                {error ? (
                    <span class="badge fail">not loaded</span>
                ) : r ? (
                    <span class={r.enabled ? "badge ok" : "badge"}>
                        {r.enabled ? "on" : "off"}
                    </span>
                ) : null}
            </div>
            {error && <div class="inline-msg">{error}</div>}
            {r && !r.enabled && (
                <>
                    <div class="muted small">
                        Off. ChatGPT reaches a server on the internet only: run
                        a tunnel you are allowed to use (cloudflared, ngrok,
                        ChatGPT's own) to the channel's port, then turn it on in
                        your config file and restart the server. Only the
                        environments marked remote_access are offered, the admin
                        page never.
                    </div>
                    <pre class="mt-xs">{SNIPPET}</pre>
                </>
            )}
            {r?.enabled && (
                <>
                    <div class="muted small">
                        In ChatGPT, add a connector with the address{" "}
                        <b class="mono anywhere">{r.mcp_url}</b> and OAuth. It
                        opens a page asking for a pairing code: make one here
                        (one use, 10 minutes). Environments offered:{" "}
                        <b>{r.instances.join(", ") || "none"}</b>.
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
