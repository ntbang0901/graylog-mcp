import { useEffect, useState } from "preact/hooks";
import { api, errorText } from "../api";
import {
    attempt,
    byGroup,
    confirmBox,
    currentEnv,
    envHealth,
    envTests,
    instances,
    label,
    loadState,
    navigate,
    openEnv,
    runDoctor,
    state,
    toast,
} from "../store";
import type {
    Fields,
    Group,
    Instance,
    Repo,
    TestResult,
} from "../scripts/types";
import {
    AsyncButton,
    Icon,
    Page,
    StateTag,
    plural,
    show,
    whenShort,
} from "./UI";
function SecretBadge({ e }: { e: Instance }) {
    const kind = e.auth === "basic" ? "password" : "token";
    return (
        <span
            class={`badge ${e.secret_source === "env" || e.secret_source === "saved" ? "ok" : "warn"}`}
        >
            {kind}{" "}
            {e.secret_source === "env"
                ? "from environment"
                : e.secret_source === "saved"
                  ? "saved"
                  : "missing"}
        </span>
    );
}
function FocusBadge({ f }: { f?: Fields | null }) {
    if (!f || (f.service === undefined && !f.streams))
        return <span class="badge info">focus: auto</span>;
    if (f.service === false && !f.streams)
        return <span class="badge">focus: off</span>;
    const parts = [
        f.service === false
            ? "every service"
            : f.service
              ? show(f.service)
              : "auto",
    ];
    if (f.streams) parts.push(`streams ${f.streams.join(", ")}`);
    return <span class="badge info">focus: {parts.join("; ")}</span>;
}
async function testRow(e: Instance) {
    envTests.value = { ...envTests.value, [e.name]: { ok: false, busy: true } };
    try {
        const r = await api<TestResult>("/api/test", "POST", {
            name: e.name.replace("/", "-"),
            fields: { ...e.fields, auth: e.auth },
            token: null,
        });
        envTests.value = {
            ...envTests.value,
            [e.name]: { ...r, at: Date.now() / 1000 },
        };
    } catch (err) {
        envTests.value = {
            ...envTests.value,
            [e.name]: {
                ok: false,
                error: errorText(err),
                at: Date.now() / 1000,
            },
        };
    }
}
function EnvRows({ items }: { items: Instance[] }) {
    return (
        <div class="table-wrap">
            <table class="envtable acts">
                <thead>
                    <tr>
                        <th>Environment</th>
                        <th>Health</th>
                        <th>URL</th>
                        <th>Credentials</th>
                        <th>
                            <span class="hidden">Actions</span>
                        </th>
                    </tr>
                </thead>
                <tbody>
                    {items.map((e) => {
                        const t = envTests.value[e.name];
                        return (
                            <tr
                                key={e.name}
                                class={
                                    currentEnv.value === e.name
                                        ? "ctx"
                                        : undefined
                                }
                                onClick={() => openEnv(e)}
                            >
                                <td>
                                    <div class="envname">
                                        <b>{e.environment || e.name}</b>
                                        {e.default && (
                                            <span class="badge info">
                                                default
                                            </span>
                                        )}
                                        {e.invalid && (
                                            <span class="badge fail">
                                                needs a fix
                                            </span>
                                        )}
                                    </div>
                                    {e.description && (
                                        <div class="muted small">
                                            {e.description}
                                        </div>
                                    )}
                                </td>
                                <td>
                                    <StateTag cls={envHealth(e).cls} />
                                    {t && (
                                        <div
                                            class={`rtest${!t.busy && !t.ok ? " bad" : ""}`}
                                        >
                                            {t.busy
                                                ? "testing…"
                                                : t.ok
                                                  ? `tested ${whenShort(t.at || 0)} · Graylog ${t.version} · ${plural(t.streams || 0, "stream")}`
                                                  : t.error}
                                        </div>
                                    )}
                                </td>
                                <td class="mono small url" title={e.url}>
                                    {e.url}
                                </td>
                                <td>
                                    <SecretBadge e={e} />{" "}
                                    <span class="faint small">
                                        {e.auth === "basic"
                                            ? "user + password"
                                            : ""}
                                    </span>
                                </td>
                                <td>
                                    {e.local ? (
                                        <>
                                            <button
                                                class="btn sm ghost icon"
                                                aria-label={`Test ${label(e)}`}
                                                title="Test the connection"
                                                disabled={t?.busy}
                                                onClick={(ev) => {
                                                    ev.stopPropagation();
                                                    void testRow(e);
                                                }}
                                            >
                                                <Icon name="ok" />
                                            </button>
                                            <button
                                                class="btn sm ghost icon"
                                                aria-label={`Edit ${label(e)}`}
                                                title="Edit"
                                                onClick={(ev) => {
                                                    ev.stopPropagation();
                                                    openEnv(e);
                                                }}
                                            >
                                                <Icon name="edit" />
                                            </button>
                                            <button
                                                class="btn sm ghost icon danger"
                                                aria-label={`Delete ${label(e)}`}
                                                title="Delete"
                                                onClick={(ev) => {
                                                    ev.stopPropagation();
                                                    void attempt(async () => {
                                                        if (
                                                            await confirmBox(
                                                                `Delete ${e.name}?`,
                                                                "It is removed from the config file. Saved tokens and passwords stay on this machine.",
                                                                "Delete",
                                                                true,
                                                            )
                                                        ) {
                                                            await api(
                                                                `/api/instances/${encodeURIComponent(e.name)}`,
                                                                "DELETE",
                                                            );
                                                            toast(
                                                                `Deleted ${e.name}`,
                                                            );
                                                            await loadState();
                                                            void runDoctor();
                                                        }
                                                    });
                                                }}
                                            >
                                                <Icon name="trash" />
                                            </button>
                                        </>
                                    ) : (
                                        <span class="muted small">
                                            from the shared file
                                        </span>
                                    )}
                                </td>
                            </tr>
                        );
                    })}
                </tbody>
            </table>
        </div>
    );
}
function Repository({
    r,
    group,
    sharedUp,
}: {
    r: Repo;
    group: string;
    sharedUp: boolean;
}) {
    const f = r.focus || {},
        [open, setOpen] = useState(false),
        [service, setService] = useState(
            f.service === false ? "" : show(f.service),
        ),
        [streams, setStreams] = useState(show(f.streams)),
        [off, setOff] = useState(f.service === false),
        toShared =
            sharedUp &&
            (r.claude_code === "stdio" || r.claude_code === "stale"),
        canSetup =
            r.kind === "path" &&
            r.exists &&
            (!(r.project_config && r.claude_code) || toShared),
        canFocus = r.kind === "path" && r.exists;
    return (
        <div class="repo">
            <div>
                <span class="mono">{r.entry}</span>
                <div class="meta">
                    {r.kind === "url" ? (
                        <span class="badge">matched by git remote</span>
                    ) : !r.exists ? (
                        <span class="badge fail">folder not found</span>
                    ) : (
                        <>
                            {r.remote ? (
                                <span class="badge mono">{r.remote}</span>
                            ) : (
                                <span class="badge">no git remote</span>
                            )}{" "}
                            <span
                                class={`badge ${r.project_config ? "ok" : "warn"}`}
                            >
                                {r.project_config
                                    ? ".graylog-mcp.toml"
                                    : "not set up"}
                            </span>{" "}
                            {r.claude_code === "shared" ? (
                                <span class="badge ok">
                                    Claude Code · shared server
                                </span>
                            ) : r.claude_code === "stale" ? (
                                <span class="badge warn">
                                    Claude Code · old port
                                </span>
                            ) : r.claude_code ? (
                                <span
                                    class={`badge${sharedUp ? " warn" : " ok"}`}
                                >
                                    Claude Code · own process
                                </span>
                            ) : null}{" "}
                            {r.project_config && <FocusBadge f={r.focus} />}
                        </>
                    )}
                </div>
            </div>
            <div class="actions">
                {canSetup && (
                    <AsyncButton
                        class="btn sm"
                        run={async () => {
                            const result = await api<{ source: string }>(
                                "/api/repos/setup",
                                "POST",
                                { group, repo: r.entry },
                            );
                            toast(
                                result.source === "shared"
                                    ? "Claude Code now connects to the shared server"
                                    : "Repository set up",
                            );
                            await loadState();
                        }}
                    >
                        {toShared
                            ? r.claude_code === "stale"
                                ? "Update port"
                                : "Use shared server"
                            : "Set up"}
                    </AsyncButton>
                )}
                {canFocus && (
                    <button
                        class="btn sm"
                        aria-expanded={open}
                        onClick={() => setOpen(!open)}
                    >
                        Focus
                    </button>
                )}
                <AsyncButton
                    class="btn sm ghost icon danger"
                    aria-label={`Remove ${r.entry}`}
                    title="Remove"
                    run={async () => {
                        if (
                            await confirmBox(
                                `Remove ${r.entry}?`,
                                `It is detached from ${group}. Files in the repository are not touched.`,
                                "Remove",
                                true,
                            )
                        ) {
                            await api("/api/repos/remove", "POST", {
                                group,
                                repo: r.entry,
                            });
                            toast("Removed");
                            await loadState();
                        }
                    }}
                >
                    <Icon name="trash" />
                </AsyncButton>
            </div>
            {canFocus && (
                <div class={`focus-form${open ? "" : " hidden"}`}>
                    <div class="small muted">
                        What the MCP server searches by default inside this
                        repository. Other services and streams are searched only
                        when asked for.
                    </div>
                    <div class="grid2">
                        <div class="field">
                            <span class="label">Service</span>
                            <input
                                class="fservice"
                                value={service}
                                onInput={(e) =>
                                    setService(e.currentTarget.value)
                                }
                                placeholder="auto: guessed from the repository name"
                                spellcheck={false}
                            />
                            <span class="hint">
                                Value of the service field (e.g. application),
                                comma-separated for several.
                            </span>
                        </div>
                        <div class="field">
                            <span class="label">Streams</span>
                            <input
                                class="fstreams"
                                value={streams}
                                onInput={(e) =>
                                    setStreams(e.currentTarget.value)
                                }
                                placeholder="every stream"
                                spellcheck={false}
                            />
                            <span class="hint">
                                Stream titles or ids, comma-separated.
                            </span>
                        </div>
                    </div>
                    <div class="actions">
                        <label class="check small">
                            <input
                                type="checkbox"
                                class="foff"
                                checked={off}
                                onChange={(e) =>
                                    setOff(e.currentTarget.checked)
                                }
                            />
                            No service filter (search every service)
                        </label>
                        <span class="spacer" />
                        <button
                            class="btn sm ghost"
                            onClick={() => setOpen(false)}
                        >
                            Cancel
                        </button>
                        <AsyncButton
                            class="btn sm primary"
                            run={async () => {
                                const result = await api<{ focus: Fields }>(
                                    "/api/repos/focus",
                                    "POST",
                                    {
                                        group,
                                        repo: r.entry,
                                        service,
                                        streams,
                                        off,
                                    },
                                );
                                toast(
                                    `Focus saved in .graylog-mcp.toml${result.focus.service ? `: ${show(result.focus.service)}` : ""}`,
                                );
                                setOpen(false);
                                await loadState();
                            }}
                        >
                            Save focus
                        </AsyncButton>
                    </div>
                </div>
            )}
        </div>
    );
}
function GroupCard({
    name,
    info,
    envs,
    shown,
}: {
    name: string;
    info: Partial<Group>;
    envs: Instance[];
    shown: Instance[];
}) {
    const [description, setDescription] = useState(info.description || ""),
        [defaultEnv, setDefaultEnv] = useState(info.default_environment || ""),
        [repo, setRepo] = useState(""),
        [project, setProject] = useState(true),
        [claude, setClaude] = useState(true),
        sharedUp = !!state.value?.shared.running;
    useEffect(() => {
        setDescription(info.description || "");
        setDefaultEnv(info.default_environment || "");
    }, [info.description, info.default_environment]);
    const saveGroup = async (
        desc = description,
        env = defaultEnv,
        extra = {},
    ) => {
        await api("/api/groups", "POST", {
            group: name,
            description: desc.trim(),
            default_environment: env,
            ...extra,
        });
        toast(`Saved ${name}`);
        await loadState();
    };
    const addRepo = async () => {
        if (!repo.trim()) throw new Error("Enter the folder of the repository");
        const r = await api<Fields>("/api/repos", "POST", {
            group: name,
            repo: repo.trim(),
            project_config: project,
            claude_code: claude,
        });
        toast(
            `Added to ${name}${r.remote ? ` (${r.remote})` : ""}${r.project_config ? ", set up" : ""}${r.source === "shared" ? ", via the shared server" : ""}`,
        );
        setRepo("");
        await loadState();
    };
    if (!name)
        return (
            <div class="card gcard">
                <div class="card-head">
                    <h2>Without a group</h2>
                    <span class="spacer" />
                    <button class="btn sm ghost" onClick={() => openEnv(null)}>
                        <Icon name="plus" />
                        Add environment
                    </button>
                </div>
                <EnvRows items={shown.length ? shown : envs} />
            </div>
        );
    const bad = envs.filter((e) => envHealth(e).cls === "fail").length;
    return (
        <div class="card gcard">
            <div class="card-head">
                <div class="ghead">
                    <h2>{name}</h2>
                    <span class="muted small">
                        {plural(envs.length, "environment")}
                        {bad > 0 && (
                            <>
                                {" "}
                                · <span class="bad-text">{bad} failing</span>
                            </>
                        )}
                    </span>
                </div>
                <input
                    class="inline-edit gdesc"
                    value={description}
                    placeholder="Add a description…"
                    aria-label={`Description of ${name}`}
                    onInput={(e) => setDescription(e.currentTarget.value)}
                    onChange={(e) => {
                        void attempt(() => saveGroup(e.currentTarget.value));
                    }}
                    onKeyDown={(e) => {
                        if (e.key === "Enter") e.currentTarget.blur();
                    }}
                />
                {state.value?.default_group === name ? (
                    <span class="badge info">default group</span>
                ) : (
                    <AsyncButton
                        class="btn sm ghost"
                        title="Used when a question names no system"
                        run={() =>
                            saveGroup(description, defaultEnv, {
                                make_default: true,
                            })
                        }
                    >
                        Make default
                    </AsyncButton>
                )}
            </div>
            <div class="gmeta">
                <span>Default environment</span>
                <select
                    class="genv"
                    aria-label={`Default environment of ${name}`}
                    value={defaultEnv}
                    onChange={(e) => {
                        const value = e.currentTarget.value;
                        setDefaultEnv(value);
                        void attempt(() => saveGroup(description, value));
                    }}
                >
                    <option value="">(none)</option>
                    {(
                        info.environments ||
                        envs.map((e) => e.environment || "")
                    ).map((e) => (
                        <option value={e}>{e}</option>
                    ))}
                </select>
                <span class="faint small">
                    used when a question names only this group
                </span>
            </div>
            {envs.length ? (
                <EnvRows items={shown.length ? shown : envs} />
            ) : (
                <div class="inline-msg">No environment in this group yet.</div>
            )}
            <div class="mt-xs">
                <button
                    class="btn sm ghost"
                    onClick={() => openEnv(null, name)}
                >
                    <Icon name="plus" />
                    Add environment to {name}
                </button>
            </div>
            <div class="section-title">
                Repositories{" "}
                <span class="muted small">
                    inside these, {name} is the default
                </span>
            </div>
            {info.repos?.length ? (
                info.repos.map((r) => (
                    <Repository
                        key={r.entry}
                        r={r}
                        group={name}
                        sharedUp={sharedUp}
                    />
                ))
            ) : (
                <div class="inline-msg">No repository yet.</div>
            )}
            <div class="addrepo">
                <input
                    class="rpath"
                    placeholder="Folder on this machine, e.g. ~/code/payment-api"
                    spellcheck={false}
                    aria-label="Repository folder"
                    value={repo}
                    onInput={(e) => setRepo(e.currentTarget.value)}
                    onKeyDown={(e) => {
                        if (e.key === "Enter") void attempt(addRepo);
                    }}
                />
                <AsyncButton class="btn" run={addRepo}>
                    Add repository
                </AsyncButton>
                <label class="check small">
                    <input
                        type="checkbox"
                        class="rproject"
                        checked={project}
                        onChange={(e) => setProject(e.currentTarget.checked)}
                    />
                    write .graylog-mcp.toml
                </label>
                <label class="check small">
                    <input
                        type="checkbox"
                        class="rclaude"
                        checked={claude}
                        onChange={(e) => setClaude(e.currentTarget.checked)}
                    />
                    {sharedUp
                        ? "connect Claude Code"
                        : "register Claude Code (.mcp.json)"}
                </label>
            </div>
        </div>
    );
}
export function Environments() {
    const [search, setSearch] = useState(""),
        q = search.trim().toLowerCase(),
        all = instances.value,
        groups = byGroup(all).map(([g]) => g);
    for (const g of state.value?.groups || [])
        if (!groups.includes(g.name)) groups.push(g.name);
    const cards = groups
        .map((g) => {
            const info: Partial<Group> =
                    state.value?.groups.find((x) => x.name === g) || {},
                envs = all.filter((e) => (e.group || "") === g),
                shown = envs.filter(
                    (e) =>
                        !q ||
                        [e.name, e.group, e.url, e.description]
                            .join(" ")
                            .toLowerCase()
                            .includes(q),
                ),
                repoText = (info.repos || [])
                    .map((r) => `${r.entry} ${r.remote || ""}`)
                    .join(" ")
                    .toLowerCase();
            if (
                q &&
                !shown.length &&
                !g.toLowerCase().includes(q) &&
                !repoText.includes(q)
            )
                return null;
            return (
                <GroupCard
                    key={g}
                    name={g}
                    info={info}
                    envs={envs}
                    shown={shown}
                />
            );
        })
        .filter(Boolean);
    return (
        <Page
            id="envs"
            tools={
                <button class="btn ghost sm" onClick={() => navigate("config")}>
                    Raw file
                </button>
            }
        >
            <div class={`row center mb-lg${all.length ? "" : " hidden"}`}>
                <div class="field grow-search">
                    <input
                        id="envSearch"
                        placeholder="Filter by name, group, URL or repository…"
                        aria-label="Filter environments"
                        value={search}
                        onInput={(e) => setSearch(e.currentTarget.value)}
                    />
                </div>
                <span class="spacer" />
                <button class="btn primary" onClick={() => openEnv(null)}>
                    <Icon name="plus" />
                    Add environment
                </button>
            </div>
            <div id="groupCards">
                {!all.length ? (
                    <div class="card empty">
                        <h3>No environment yet</h3>
                        <div>
                            Each environment points to one Graylog. Name it as
                            you like: dev, uat, prod… and put it in a group
                            (system) if you have several.
                        </div>
                        <div class="actions">
                            <button
                                class="btn primary"
                                onClick={() => openEnv(null)}
                            >
                                <Icon name="plus" />
                                Add environment
                            </button>
                        </div>
                    </div>
                ) : cards.length ? (
                    cards
                ) : (
                    <div class="card empty">
                        <h3>No match</h3>
                    </div>
                )}
            </div>
        </Page>
    );
}
