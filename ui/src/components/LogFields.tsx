import { useEffect, useRef, useState } from "preact/hooks";
import { api, errorText } from "../api";
import {
    currentEnv,
    currentPage,
    detectRequest,
    dirty,
    fieldsScope,
    instances,
    loadState,
    navigate,
    runDoctor,
    scopeChosen,
    scopes,
    state,
    toast,
    confirmBox,
    attempt,
} from "../store";
import type { Fields, ScopeData } from "../scripts/types";
import { SETTING_META, SETTING_SECTIONS } from "../fields";
import { AsyncButton, Page, show, num } from "./UI";
function ancestors(scope: string) {
    if (scope.startsWith("instance:")) {
        const e = instances.value.find((e) => e.name === scope.slice(9));
        return [
            ...(e?.group ? [`group:${e.group}`] : []),
            ...(e?.environment ? [`env:${e.environment}`] : []),
            "global",
        ];
    }
    return scope === "global" ? [] : ["global"];
}
const scopeShort = (sc: string) =>
    sc === "global"
        ? "all environments"
        : sc.startsWith("env:")
          ? `env ${sc.slice(4)}`
          : sc.slice(sc.indexOf(":") + 1);
export function LogFields() {
    const scope = fieldsScope.value,
        [data, setData] = useState<ScopeData | null>(null),
        [values, setValues] = useState<Record<string, string>>({}),
        [error, setError] = useState(""),
        [loading, setLoading] = useState(false),
        [detectEnv, setDetectEnv] = useState(""),
        [detectRange, setDetectRange] = useState("24h"),
        [detection, setDetection] = useState<Fields | null>(null),
        [detectError, setDetectError] = useState(""),
        [detectBusy, setDetectBusy] = useState(false),
        [selected, setSelected] = useState<Set<string>>(new Set()),
        [fresh, setFresh] = useState<Set<string>>(new Set()),
        seq = useRef(0);
    const scopeLabel = () =>
        scopes.value.find((s) => s.scope === scope)?.label || "this level";
    const load = async () => {
        const id = ++seq.current;
        setLoading(true);
        setError("");
        try {
            const [next, ...parent] = await Promise.all([
                api<ScopeData>(`/api/scope?scope=${encodeURIComponent(scope)}`),
                ...ancestors(scope).map((sc) =>
                    api<ScopeData>(`/api/scope?scope=${encodeURIComponent(sc)}`)
                        .then((d) => ({ sc, values: d.values }))
                        .catch(() => null),
                ),
            ]);
            if (id !== seq.current) return;
            next.from = Object.fromEntries(
                next.keys.map((k) => {
                    const hit = parent.find(
                        (a) => a && a.values[k] !== undefined,
                    );
                    return [k, hit ? scopeShort(hit.sc) : "default"];
                }),
            );
            setData(next);
            setValues(
                Object.fromEntries(
                    next.keys.map((k) => [k, show(next.values[k])]),
                ),
            );
            scopes.value = next.scopes;
            const inst =
                instances.value.find((e) => scope === `instance:${e.name}`) ||
                instances.value.find(
                    (e) => scope === `group:${e.group}` && e.default,
                ) ||
                instances.value.find((e) => scope === `group:${e.group}`) ||
                instances.value.find((e) => e.name === currentEnv.value) ||
                instances.value.find((e) => e.default) ||
                instances.value[0];
            if (inst) setDetectEnv(inst.name);
        } catch (e) {
            if (id === seq.current) {
                setError(
                    `Could not load these settings: ${errorText(e)}. Pick the level again or reopen this page to try again.`,
                );
                toast(errorText(e), "fail");
            }
        } finally {
            if (id === seq.current) setLoading(false);
        }
    };
    useEffect(() => {
        if (currentPage.value === "fields" && state.value) void load();
        return () => {
            seq.current++;
        };
    }, [scope, currentPage.value, !!state.value]);
    useEffect(() => {
        if (
            currentPage.value === "fields" &&
            !scopeChosen.value &&
            !dirty.value
        )
            fieldsScope.value = currentEnv.value
                ? `instance:${currentEnv.value}`
                : "global";
    }, [currentEnv.value, currentPage.value]);
    const payload = (vs: Record<string, string>, clear = false) =>
        Object.fromEntries(
            (data?.keys || []).map((k) => {
                let v: string | boolean | null = clear
                    ? ""
                    : vs[k]?.trim() || "";
                if (SETTING_META[k]?.[1] === "bool")
                    v = v === "" ? null : v === "true";
                return [k, v];
            }),
        );
    const change = (key: string, value: string) => {
        const next = { ...values, [key]: value };
        setValues(next);
        dirty.value = {
            what: scopeLabel(),
            save: async () => {
                await api("/api/scope", "POST", {
                    scope,
                    values: payload(next),
                });
                dirty.value = null;
                toast("Settings saved");
                await loadState();
                await load();
            },
            discard: () => {
                void load();
            },
        };
    };
    const switchScope = async (next: string) => {
        if (next === scope) return;
        if (
            dirty.value &&
            !(await confirmBox(
                "Switch without saving?",
                `Your changes to ${dirty.value.what} are not saved.`,
                "Discard changes",
                true,
            ))
        )
            return;
        dirty.value = null;
        scopeChosen.value = true;
        fieldsScope.value = next;
        setDetection(null);
        setFresh(new Set());
    };
    const runDetect = async () => {
        setDetectBusy(true);
        setDetection(null);
        setDetectError("");
        try {
            const r = await api<Fields>("/api/detect", "POST", {
                instance: detectEnv,
                range: detectRange,
            });
            setDetection(r);
            setSelected(
                new Set([
                    ...Object.keys(r.suggested || {}),
                    ...(r.app_packages?.length ? ["__app_packages"] : []),
                ]),
            );
        } catch (e) {
            setDetectError(errorText(e));
            throw e;
        } finally {
            setDetectBusy(false);
        }
    };
    useEffect(() => {
        if (detectRequest.value && currentPage.value === "fields" && detectEnv)
            void attempt(runDetect);
    }, [detectRequest.value]);
    const renderInput = (key: string) => {
        const [title, type, hint] = SETTING_META[key] || [key, "text", ""],
            value = data?.values[key],
            eff = data?.effective[key],
            inherited = eff == null || eff === "" ? "not set" : show(eff),
            id = `s_${key.replace(/\W/g, "_")}`,
            select =
                type === "bool" ||
                type.startsWith("select:") ||
                type === "groups" ||
                type === "instances";
        const options =
            type === "bool"
                ? [
                      ["true", "yes"],
                      ["false", "no"],
                  ]
                : type === "groups"
                  ? (state.value?.groups || []).map((g) => [g.name, g.name])
                  : type === "instances"
                    ? instances.value.map((e) => [e.name, e.name])
                    : type
                          .slice(7)
                          .split(",")
                          .map((o) => [o, o]);
        return (
            <div class={`field${fresh.has(key) ? " fresh" : ""}`} key={key}>
                <label class="label" for={id}>
                    {title}
                    {value !== undefined ? (
                        <>
                            <span class="badge info">set here</span>
                            <button
                                type="button"
                                class="clr"
                                aria-label={`Clear ${title} at this level`}
                                title="Clear at this level, so it is inherited again"
                                onClick={() => {
                                    change(key, "");
                                    document.getElementById(id)?.focus();
                                }}
                            >
                                ✕
                            </button>
                        </>
                    ) : (
                        <span
                            class="from"
                            title="Where the value in effect comes from"
                        >
                            ← {data?.from?.[key] || "default"}
                        </span>
                    )}
                </label>
                {select ? (
                    <select
                        id={id}
                        value={values[key] || ""}
                        onChange={(e) => change(key, e.currentTarget.value)}
                    >
                        <option value="">Inherit ({inherited})</option>
                        {options.map(([v, t]) => (
                            <option value={v}>{t}</option>
                        ))}
                    </select>
                ) : (
                    <input
                        id={id}
                        {...(type === "number"
                            ? { type: "number" as const, min: "1" }
                            : { type: "text" as const })}
                        value={values[key] || ""}
                        placeholder={inherited}
                        spellcheck={false}
                        onInput={(e) => change(key, e.currentTarget.value)}
                    />
                )}
                {hint && <span class="hint">{hint}</span>}
            </div>
        );
    };
    const sug = (k: string, name: string, to: unknown) => (
        <label class="sug">
            <input
                type="checkbox"
                checked={selected.has(k)}
                aria-label={`Apply ${name}`}
                onChange={(e) => {
                    const next = new Set(selected);
                    if (e.currentTarget.checked) next.add(k);
                    else next.delete(k);
                    setSelected(next);
                }}
            />
            <span class="sn">
                <b>{name}</b>
                <span class="mono small muted">
                    now:{" "}
                    {data?.effective[
                        k === "__app_packages" ? "app_packages" : k
                    ] == null ? (
                        <span class="faint">not set</span>
                    ) : (
                        show(
                            data.effective[
                                k === "__app_packages" ? "app_packages" : k
                            ],
                        )
                    )}
                </span>
                <span class="mono small">
                    →{" "}
                    {show(
                        to && typeof to === "object" && !Array.isArray(to)
                            ? Object.entries(to).map(([a, b]) => `${a}: ${b}`)
                            : to,
                    )}
                </span>
            </span>
        </label>
    );
    const levels = [...ancestors(scope)].reverse().concat(scope),
        options = scopes.value.some((s) => s.scope === scope)
            ? scopes.value
            : [...scopes.value, { scope, label: scope }];
    return (
        <Page
            id="fields"
            tools={
                <button class="btn ghost sm" onClick={() => navigate("config")}>
                    Raw file
                </button>
            }
        >
            <div class="scopebar">
                <nav
                    class="crumbs"
                    id="scopeCrumbs"
                    aria-label="Level being edited"
                >
                    {levels.map((sc, i) => (
                        <>
                            {i > 0 && (
                                <span class="sep" aria-hidden="true">
                                    ›
                                </span>
                            )}
                            <button
                                aria-current={sc === scope ? "true" : undefined}
                                onClick={() => switchScope(sc)}
                            >
                                {scopeShort(sc)}
                            </button>
                        </>
                    ))}
                </nav>
                <span class="spacer" />
                <div class="field row-field">
                    <label class="label" for="s_scope">
                        Other level
                    </label>
                    <select
                        id="s_scope"
                        value={scope}
                        onChange={(e) => {
                            void switchScope(e.currentTarget.value);
                        }}
                    >
                        {options.map((s) => (
                            <option value={s.scope}>{s.label}</option>
                        ))}
                    </select>
                </div>
                <AsyncButton
                    class="btn ghost danger"
                    id="resetScope"
                    disabled={!data || loading || !!error}
                    run={async () => {
                        if (
                            !(await confirmBox(
                                `Clear ${scopeLabel()}?`,
                                "Every setting written at this level is removed and inherited again.",
                                "Clear",
                                true,
                            ))
                        )
                            return;
                        await api("/api/scope", "POST", {
                            scope,
                            values: payload(values, true),
                        });
                        dirty.value = null;
                        toast("Cleared");
                        await loadState();
                        await load();
                    }}
                >
                    Clear this level
                </AsyncButton>
            </div>
            <div class="hint mb-lg">
                An empty field inherits: global → environment → group → single
                instance. The grey text is the value in effect, and the tag says
                which level it comes from.
            </div>
            <div class="fgrid">
                <div id="s_form">
                    {error ? (
                        <div class="inline-msg" role="alert">
                            {error}
                        </div>
                    ) : loading || !data ? (
                        <div class="inline-msg" role="status">
                            Loading…
                        </div>
                    ) : (
                        SETTING_SECTIONS.map(([title, sub, names]) => {
                            const keys = names.filter((k) =>
                                data.keys.includes(k),
                            );
                            return (
                                !!keys.length && (
                                    <div class="card">
                                        <div class="card-head">
                                            <h2>{title}</h2>
                                            <span class="muted small">
                                                {sub}
                                            </span>
                                        </div>
                                        <div class="grid2">
                                            {keys.map(renderInput)}
                                        </div>
                                    </div>
                                )
                            );
                        })
                    )}
                </div>
                <aside class="fside" aria-label="Detect from your logs">
                    <div class="card">
                        <div class="card-head">
                            <h2>Detect from your logs</h2>
                        </div>
                        <div class="muted small mb-xs">
                            Read-only: counts which fields your messages carry.
                        </div>
                        <div class="row">
                            <div class="field">
                                <label class="label" for="d_env">
                                    Environment
                                </label>
                                <select
                                    id="d_env"
                                    value={detectEnv}
                                    onChange={(e) =>
                                        setDetectEnv(e.currentTarget.value)
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
                            <div class="field grow-none">
                                <label class="label" for="d_range">
                                    Look at
                                </label>
                                <select
                                    id="d_range"
                                    value={detectRange}
                                    onChange={(e) =>
                                        setDetectRange(e.currentTarget.value)
                                    }
                                >
                                    <option value="1h">last hour</option>
                                    <option value="24h">last 24 hours</option>
                                    <option value="7d">last 7 days</option>
                                </select>
                            </div>
                            <AsyncButton
                                class="btn primary"
                                id="runDetect"
                                disabled={!detectEnv || detectBusy}
                                run={runDetect}
                            >
                                Detect fields
                            </AsyncButton>
                        </div>
                        <div id="detectOut">
                            {detectBusy ? (
                                <div class="inline-msg mt-sm">
                                    Reading your logs…
                                </div>
                            ) : detectError ? (
                                <div class="inline-msg mt-sm" role="alert">
                                    {detectError}
                                </div>
                            ) : detection ? (
                                !Object.keys(detection.suggested || {})
                                    .length ? (
                                    <div class="inline-msg mt-sm">
                                        Nothing to suggest.
                                        {detection.hint || ""}
                                    </div>
                                ) : (
                                    <>
                                        <div class="section-title">
                                            Suggestions:{" "}
                                            {num(detection.total_messages)}{" "}
                                            messages in {detection.instance}
                                            {detection.error_query_reason
                                                ? `, ${detection.error_query_reason}`
                                                : ""}
                                        </div>
                                        <div class="sugs">
                                            {Object.entries(
                                                detection.suggested as Fields,
                                            ).map(([k, v]) =>
                                                sug(
                                                    k,
                                                    SETTING_META[k]?.[0] || k,
                                                    v,
                                                ),
                                            )}
                                            {!!detection.app_packages?.length &&
                                                sug(
                                                    "__app_packages",
                                                    "Application packages",
                                                    detection.app_packages,
                                                )}
                                        </div>
                                        <div class="actions mt-sm">
                                            <AsyncButton
                                                class="btn primary"
                                                id="applyDetect"
                                                run={async () => {
                                                    const suggested =
                                                        Object.fromEntries(
                                                            Object.entries(
                                                                detection.suggested as Fields,
                                                            ).filter(([k]) =>
                                                                selected.has(k),
                                                            ),
                                                        );
                                                    await api(
                                                        "/api/detect/apply",
                                                        "POST",
                                                        {
                                                            suggested,
                                                            scope,
                                                            app_packages:
                                                                selected.has(
                                                                    "__app_packages",
                                                                )
                                                                    ? detection.app_packages
                                                                    : [],
                                                        },
                                                    );
                                                    setFresh(
                                                        new Set(
                                                            [...selected].map(
                                                                (k) =>
                                                                    k ===
                                                                    "__app_packages"
                                                                        ? "app_packages"
                                                                        : k,
                                                            ),
                                                        ),
                                                    );
                                                    toast(
                                                        `Applied to ${scopeLabel()}`,
                                                    );
                                                    setDetection(null);
                                                    dirty.value = null;
                                                    await loadState();
                                                    await load();
                                                    void runDoctor();
                                                }}
                                            >
                                                Apply to {scopeLabel()}
                                            </AsyncButton>
                                            <details class="full-line">
                                                <summary>
                                                    Evidence (coverage = share
                                                    of messages carrying the
                                                    field)
                                                </summary>
                                                <div class="table-wrap">
                                                    <table>
                                                        <thead>
                                                            <tr>
                                                                <th>Kind</th>
                                                                <th>Field</th>
                                                                <th>
                                                                    Coverage
                                                                </th>
                                                                <th>
                                                                    Sample
                                                                    values
                                                                    (redacted)
                                                                </th>
                                                            </tr>
                                                        </thead>
                                                        <tbody>
                                                            {Object.entries(
                                                                (detection.evidence as Record<
                                                                    string,
                                                                    Fields[]
                                                                >) || {},
                                                            ).flatMap(
                                                                ([
                                                                    kind,
                                                                    items,
                                                                ]) =>
                                                                    items
                                                                        .slice(
                                                                            0,
                                                                            3,
                                                                        )
                                                                        .map(
                                                                            (
                                                                                i,
                                                                            ) => (
                                                                                <tr>
                                                                                    <td>
                                                                                        {
                                                                                            kind
                                                                                        }
                                                                                    </td>
                                                                                    <td class="mono small">
                                                                                        {
                                                                                            i.field
                                                                                        }
                                                                                    </td>
                                                                                    <td>
                                                                                        <div class="cov">
                                                                                            <div class="bar">
                                                                                                <div
                                                                                                    style={{
                                                                                                        width: `${Math.min(100, i.coverage_pct)}%`,
                                                                                                    }}
                                                                                                />
                                                                                            </div>
                                                                                            <span class="small">
                                                                                                {
                                                                                                    i.coverage_pct
                                                                                                }

                                                                                                %
                                                                                            </span>
                                                                                        </div>
                                                                                    </td>
                                                                                    <td class="muted small">
                                                                                        {(
                                                                                            i.samples ||
                                                                                            []
                                                                                        ).join(
                                                                                            ", ",
                                                                                        )}
                                                                                    </td>
                                                                                </tr>
                                                                            ),
                                                                        ),
                                                            )}
                                                        </tbody>
                                                    </table>
                                                </div>
                                            </details>
                                        </div>
                                    </>
                                )
                            ) : null}
                        </div>
                    </div>
                </aside>
            </div>
        </Page>
    );
}
