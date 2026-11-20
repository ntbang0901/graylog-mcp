import { useEffect, useRef, useState } from "preact/hooks";
import { api, errorText } from "../api";
import { confirmBox, currentPage, state, toast } from "../store";
import type {
    Fields,
    ProviderDeclared,
    ProvidersListing,
    ProviderTest,
} from "../scripts/types";
import { AsyncButton, ENV_NAME, Icon, Page, Refresh, plural } from "./UI";

const TASKS = ["understand", "score"];
const KEY_SOURCE: Record<string, string> = {
    saved: "saved for this user",
    env: "set in the server's environment",
};
// a command typed as one line splits at spaces, as lines keeps each line whole (an argument with spaces)
const commandOf = (text: string) =>
    (text.includes("\n") ? text.split("\n") : text.split(/\s+/))
        .map((s) => s.trim())
        .filter(Boolean);

async function saveKey(name: string, value: string) {
    await api("/api/secrets", "POST", { name, value });
}

function Tested({ t }: { t: ProviderTest }) {
    if (!t.ok)
        return (
            <div class="ed-state bad mt-xs" role="alert">
                {t.error}
                {t.ms != null ? ` (${t.ms} ms)` : ""}
            </div>
        );
    const intent = (t.answer as Fields)?.intent?.choice;
    return (
        <div class="pv-ok mt-xs" role="status">
            <Icon name="ok" /> Answered in {t.ms} ms
            {intent ? (
                <>
                    : “{t.question}” read as <b>{String(intent)}</b>
                </>
            ) : (
                ""
            )}
        </div>
    );
}

function Card({
    p,
    active,
    editable,
    reload,
}: {
    p: ProviderDeclared;
    active: boolean;
    editable: boolean;
    reload: () => Promise<void>;
}) {
    const [tested, setTested] = useState<ProviderTest | null>(null),
        [key, setKey] = useState("");
    const needsKey = p.kind !== "command" && !!p.api_key_env;
    return (
        <div class="pv-card" id={`pv-${p.name}`}>
            <div class="card-head">
                <h3 class="mono">{p.name}</h3>
                <span class="badge">{p.kind}</span>
                {active && <span class="badge ok">in use</span>}
            </div>
            <dl class="kv">
                <dt>can</dt>
                <dd>{(p.can || TASKS).join(", ")}</dd>
                {p.kind === "command" ? (
                    <>
                        <dt>command</dt>
                        <dd>{(p.command || []).join(" ") || "-"}</dd>
                    </>
                ) : (
                    <>
                        <dt>endpoint</dt>
                        <dd>{p.base_url || "the default"}</dd>
                        <dt>model</dt>
                        <dd>{p.model || "the default"}</dd>
                    </>
                )}
                {p.api_key_env && (
                    <>
                        <dt>key</dt>
                        <dd>
                            {p.api_key_env} ·{" "}
                            <span class={p.key_source ? "" : "warn-text"}>
                                {p.key_source
                                    ? KEY_SOURCE[p.key_source]
                                    : "not saved"}
                            </span>
                        </dd>
                    </>
                )}
                {p.timeout != null && (
                    <>
                        <dt>timeout</dt>
                        <dd>{p.timeout} s</dd>
                    </>
                )}
            </dl>
            <div class="actions mt-sm">
                <AsyncButton
                    class="btn sm"
                    title="Hand it one fixed sample question, nothing of yours"
                    run={async () => {
                        setTested(null);
                        try {
                            setTested(
                                await api<ProviderTest>(
                                    "/api/providers/test",
                                    "POST",
                                    { name: p.name },
                                ),
                            );
                        } catch (e) {
                            setTested({ ok: false, error: errorText(e) });
                        }
                    }}
                >
                    Test
                </AsyncButton>
                <AsyncButton
                    class="btn sm danger"
                    disabled={!editable}
                    run={async () => {
                        if (
                            !(await confirmBox(
                                `Remove ${p.name}?`,
                                "It is taken out of the config file and of the order. A saved key stays saved.",
                                "Remove",
                                true,
                            ))
                        )
                            return;
                        await api("/api/providers/remove", "POST", {
                            name: p.name,
                        });
                        toast(`Removed ${p.name}`);
                        await reload();
                    }}
                >
                    Remove
                </AsyncButton>
            </div>
            {needsKey && (
                <form class="pv-key mt-xs" onSubmit={(e) => e.preventDefault()}>
                    <input
                        type="password"
                        autoComplete="off"
                        aria-label={`Key for ${p.api_key_env}`}
                        placeholder={`key for ${p.api_key_env}`}
                        value={key}
                        onInput={(e) => setKey(e.currentTarget.value)}
                    />
                    <AsyncButton
                        class="btn sm"
                        type="submit"
                        disabled={!key}
                        run={async () => {
                            await saveKey(p.api_key_env!, key);
                            setKey("");
                            toast(`Saved ${p.api_key_env} for this user`);
                            await reload();
                        }}
                    >
                        Save key
                    </AsyncButton>
                </form>
            )}
            {tested && <Tested t={tested} />}
        </div>
    );
}

function Order({
    d,
    reload,
}: {
    d: ProvidersListing;
    reload: () => Promise<void>;
}) {
    const order = d.order,
        names = d.declared.map((p) => p.name),
        unused = names.filter((n) => !order?.includes(n)),
        none = order !== null && order.length === 0;
    const put = async (next: string[]) => {
        await api("/api/providers/order", "POST", { order: next });
        await reload();
    };
    const move = (i: number, by: number) => {
        const next = [...(order || [])];
        [next[i], next[i + by]] = [next[i + by], next[i]];
        return put(next);
    };
    return (
        <div class="card" id="pvOrder">
            <div class="card-head">
                <h2>Order</h2>
            </div>
            <p class="hint">
                The first provider that can do a task is asked; the next one
                only when the one before failed.{" "}
                {d.active.length
                    ? `In use now: ${d.active.join(", ")}.`
                    : "In use now: none, ask reads by rule."}
            </p>
            <label class="check mt-xs">
                <input
                    type="checkbox"
                    disabled={!d.editable}
                    checked={none}
                    onChange={(e) =>
                        void put(e.currentTarget.checked ? [] : names).catch(
                            (err) => toast(errorText(err), "fail"),
                        )
                    }
                />
                Use no provider: the rules alone, also when a key is saved
            </label>
            {order === null && (
                <div class="hint mt-xs">
                    No order is written yet: with a TypeSafe key saved, Jev is
                    the one provider; without one, the rules alone.
                </div>
            )}
            {!!order?.length && (
                <ol class="pv-order mt-xs">
                    {order.map((n, i) => (
                        <li key={n}>
                            <span class="mono">{n}</span>
                            <span class="spacer" />
                            <AsyncButton
                                class="btn sm ghost icon"
                                aria-label={`Move ${n} up`}
                                disabled={!d.editable || i === 0}
                                run={() => move(i, -1)}
                            >
                                ↑
                            </AsyncButton>
                            <AsyncButton
                                class="btn sm ghost icon"
                                aria-label={`Move ${n} down`}
                                disabled={!d.editable || i === order.length - 1}
                                run={() => move(i, 1)}
                            >
                                ↓
                            </AsyncButton>
                            <AsyncButton
                                class="btn sm ghost"
                                disabled={!d.editable}
                                run={() => put(order.filter((x) => x !== n))}
                            >
                                Stop using
                            </AsyncButton>
                        </li>
                    ))}
                </ol>
            )}
            {unused.length > 0 && (
                <div class="actions mt-xs">
                    <span class="muted small">Declared, not in use:</span>
                    {unused.map((n) => (
                        <AsyncButton
                            key={n}
                            class="btn sm"
                            disabled={!d.editable}
                            run={() => put([...(order || []), n])}
                        >
                            Use {n}
                        </AsyncButton>
                    ))}
                </div>
            )}
        </div>
    );
}

// the server lists a kind's keys sorted by name; a key it adds later goes last
const FIELD_ORDER = ["command", "base_url", "model", "api_key_env", "timeout"];
const rank = (k: string) =>
    FIELD_ORDER.includes(k) ? FIELD_ORDER.indexOf(k) : FIELD_ORDER.length;
const FIELD_HINT: Record<string, string> = {
    base_url: "https://…, or http on 127.0.0.1 / localhost",
    model: "leave empty for the default",
    api_key_env: "the name of the variable, e.g. ACME_GATEWAY_KEY",
    timeout: "seconds, 1 to 300",
};

function AddForm({
    d,
    reload,
}: {
    d: ProvidersListing;
    reload: () => Promise<void>;
}) {
    const kinds = Object.keys(d.kinds),
        [name, setName] = useState(""),
        [kind, setKind] = useState(
            kinds.includes("openai") ? "openai" : kinds[0] || "",
        ),
        [values, setValues] = useState<Record<string, string>>({}),
        [can, setCan] = useState<string[]>(TASKS),
        [use, setUse] = useState(true),
        [key, setKey] = useState(""),
        [error, setError] = useState("");
    const keys = (d.kinds[kind] || [])
            .filter((k) => k !== "can")
            .sort((a, b) => rank(a) - rank(b)),
        env = (values.api_key_env || "").trim(),
        set = (k: string, v: string) => setValues({ ...values, [k]: v });
    const add = async () => {
        const fields: Fields = { kind, can };
        for (const k of keys) {
            const v = (values[k] || "").trim();
            if (!v) continue;
            fields[k] =
                k === "command"
                    ? commandOf(values[k])
                    : k === "timeout"
                      ? Number(v)
                      : v;
        }
        if (env && !ENV_NAME.test(env)) {
            setError(
                "api_key_env: letters, digits and _, not starting with a digit",
            );
            return;
        }
        try {
            await api("/api/providers", "POST", {
                name: name.trim(),
                fields,
                use,
            });
            if (key && env) await saveKey(env, key);
        } catch (e) {
            setError(errorText(e));
            return;
        }
        toast(`Added ${name.trim()}`);
        setName("");
        setValues({});
        setKey("");
        setError("");
        await reload();
    };
    return (
        <div class="card" id="pvAdd">
            <div class="card-head">
                <h2>Add a provider</h2>
            </div>
            <form class="pv-form" onSubmit={(e) => e.preventDefault()}>
                <div class="row">
                    <div class="field">
                        <label class="label req" for="pv_name">
                            Name
                        </label>
                        <input
                            id="pv_name"
                            placeholder="local"
                            autoComplete="off"
                            spellcheck={false}
                            value={name}
                            onInput={(e) => setName(e.currentTarget.value)}
                        />
                    </div>
                    <div class="field">
                        <label class="label" for="pv_kind">
                            Kind
                        </label>
                        <select
                            id="pv_kind"
                            value={kind}
                            onChange={(e) => setKind(e.currentTarget.value)}
                        >
                            {kinds.map((k) => (
                                <option value={k}>{k}</option>
                            ))}
                        </select>
                    </div>
                </div>
                <div class="field mt-sm">
                    <span class="label">Can</span>
                    <div class="chips">
                        {TASKS.map((t) => (
                            <label class="chip" key={t}>
                                <input
                                    type="checkbox"
                                    checked={can.includes(t)}
                                    onChange={(e) =>
                                        setCan(
                                            e.currentTarget.checked
                                                ? TASKS.filter(
                                                      (x) =>
                                                          x === t ||
                                                          can.includes(x),
                                                  )
                                                : can.filter((x) => x !== t),
                                        )
                                    }
                                />
                                {t}
                            </label>
                        ))}
                    </div>
                    <div class="hint">
                        understand: read what a question asks · score: judge
                        which error groups it describes (sees text from the
                        logs)
                    </div>
                </div>
                {keys.map((k) => (
                    <div class="field mt-sm" key={`${kind}:${k}`}>
                        <label class="label mono" for={`pv_${k}`}>
                            {k}
                        </label>
                        {k === "command" ? (
                            <textarea
                                id={`pv_${k}`}
                                rows={3}
                                spellcheck={false}
                                placeholder="acme-llm --json"
                                value={values[k] || ""}
                                onInput={(e) => set(k, e.currentTarget.value)}
                            />
                        ) : k === "timeout" ? (
                            <input
                                id={`pv_${k}`}
                                type="number"
                                min={1}
                                max={300}
                                value={values[k] || ""}
                                onInput={(e) => set(k, e.currentTarget.value)}
                            />
                        ) : (
                            <input
                                id={`pv_${k}`}
                                type="text"
                                autoComplete="off"
                                spellcheck={false}
                                value={values[k] || ""}
                                onInput={(e) => set(k, e.currentTarget.value)}
                            />
                        )}
                        <div class="hint">
                            {k === "command"
                                ? "the program and its arguments: separated by spaces, or one per line"
                                : FIELD_HINT[k] || ""}
                        </div>
                    </div>
                ))}
                {env && kind !== "command" && (
                    <div class="field mt-sm">
                        <label class="label" for="pv_key">
                            Key for {env}
                        </label>
                        <input
                            id="pv_key"
                            type="password"
                            autoComplete="off"
                            placeholder="optional; saved for this user, never in the config file"
                            value={key}
                            onInput={(e) => setKey(e.currentTarget.value)}
                        />
                    </div>
                )}
                <label class="check mt-sm">
                    <input
                        type="checkbox"
                        checked={use}
                        onChange={(e) => setUse(e.currentTarget.checked)}
                    />
                    Use it: add it to the end of the order
                </label>
                {error && (
                    <div class="ed-state bad mt-xs" role="alert">
                        {error}
                    </div>
                )}
                <div class="actions mt-sm">
                    <AsyncButton
                        class="btn primary"
                        type="submit"
                        id="pvAddBtn"
                        disabled={!d.editable || !name.trim() || !can.length}
                        run={add}
                    >
                        Add provider
                    </AsyncButton>
                </div>
            </form>
        </div>
    );
}

// Which models ask may hand a question to: none is needed, and a repository's own file may choose none
export function Providers() {
    const [d, setD] = useState<ProvidersListing | null>(null),
        [error, setError] = useState(""),
        seq = useRef(0);
    const load = async () => {
        const id = ++seq.current;
        try {
            const r = await api<ProvidersListing>("/api/providers");
            if (id !== seq.current) return;
            setD(r);
            setError("");
        } catch (e) {
            if (id === seq.current) setError(errorText(e));
        }
    };
    useEffect(() => {
        if (currentPage.value === "providers" && state.value) void load();
    }, [currentPage.value, state.value]);
    return (
        <Page id="providers" tools={<Refresh run={load} />}>
            <p class="pv-lead">
                Without a provider, ask reads questions by its own rules;
                nothing leaves this machine.
            </p>
            {error && (
                <div class="banner fail">
                    <Icon name="fail" />
                    <div class="text">{error}</div>
                </div>
            )}
            {d && !d.editable && (
                <div class="banner warn">
                    <Icon name="warn" />
                    <div class="text">
                        <span class="mono">{d.config_path}</span> is a
                        repository's file, which may not choose where a question
                        goes. Declare providers in your own config
                        (~/.config/graylog-mcp/config.toml) or in a central file
                        the repository includes.
                    </div>
                </div>
            )}
            {d?.error && (
                <div class="banner fail">
                    <Icon name="fail" />
                    <div class="text">{d.error}</div>
                </div>
            )}
            {d && (
                <>
                    <Order d={d} reload={load} />
                    <div class="card">
                        <div class="card-head">
                            <h2>Declared</h2>
                            <span class="muted small">
                                {plural(d.declared.length, "provider")} in{" "}
                                <span class="mono">{d.config_path}</span>
                            </span>
                        </div>
                        {d.declared.length ? (
                            <div class="pv-cards">
                                {d.declared.map((p) => (
                                    <Card
                                        key={p.name}
                                        p={p}
                                        active={d.active.includes(p.name)}
                                        editable={d.editable}
                                        reload={load}
                                    />
                                ))}
                            </div>
                        ) : (
                            <div class="inline-msg">
                                No provider is declared.
                            </div>
                        )}
                    </div>
                    <AddForm d={d} reload={load} />
                    <p class="hint">
                        The background server (graylog-mcp start) uses these
                        changes and any key saved here within seconds; a server
                        a client started itself needs that client restarted.
                    </p>
                </>
            )}
        </Page>
    );
}
