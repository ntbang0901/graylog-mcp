import { useLayoutEffect, useRef, useState } from "preact/hooks";
import { api } from "../api";
import {
    checks,
    envEditor,
    envTests,
    fieldsScope,
    instances,
    label,
    loadSetup,
    loadState,
    navigate,
    runDoctor,
    scopeChosen,
    setup,
    state,
    toast,
} from "../store";
import type { Fields, TestResult } from "../scripts/types";
import {
    AsyncButton,
    Close,
    Dialog,
    ENV_NAME,
    Icon,
    Segment,
    Tabs,
    Panel,
    num,
} from "./UI";
interface Form {
    group: string;
    name: string;
    url: string;
    description: string;
    auth: string;
    username: string;
    secret_env: string;
    secret: string;
    verify_tls: boolean;
    ca_bundle: string;
    proxy: string;
    timezone: string;
    timeout: string;
    default: boolean;
}
const empty: Form = {
    group: "",
    name: "",
    url: "",
    description: "",
    auth: "token",
    username: "",
    secret_env: "",
    secret: "",
    verify_tls: true,
    ca_bundle: "",
    proxy: "",
    timezone: "",
    timeout: "",
    default: false,
};
export function EnvDrawer() {
    const editor = envEditor.value,
        e = editor?.entry,
        [form, setForm] = useState<Form>(empty),
        [errors, setErrors] = useState<Record<string, string>>({}),
        [tab, setTab] = useState("conn"),
        [result, setResult] = useState<TestResult | null>(null),
        [offer, setOffer] = useState(false),
        body = useRef<HTMLDivElement>(null);
    useLayoutEffect(() => {
        if (!editor) return;
        const f = editor.entry?.fields || {},
            entry = editor.entry;
        setForm({
            group: entry?.group || editor.group || "",
            name: entry?.environment || entry?.name || "",
            url: f.url || "",
            description: f.description || "",
            auth: entry?.auth || "token",
            username: f.username || "",
            secret_env:
                (entry?.auth === "basic" ? f.password_env : f.token_env) || "",
            secret: "",
            verify_tls: f.verify_tls !== false,
            ca_bundle: f.ca_bundle || "",
            proxy: f.proxy || "",
            timezone: f.timezone || "",
            timeout: f.timeout ? String(f.timeout) : "",
            default: entry?.default || !instances.value.length,
        });
        setErrors({});
        setTab("conn");
        setResult(entry ? envTests.value[entry.name] || null : null);
        setOffer(false);
        if (body.current) body.current.scrollTop = 0;
        const timer = setTimeout(
            () =>
                document
                    .getElementById(
                        entry
                            ? entry.secret_set
                                ? "f_url"
                                : "f_secret"
                            : "f_url",
                    )
                    ?.focus(),
            50,
        );
        return () => clearTimeout(timer);
    }, [editor]);
    const update = <K extends keyof Form>(key: K, value: Form[K]) => {
        setForm((prev) => ({ ...prev, [key]: value }));
        if (key === "name" || key === "url")
            setErrors((prev) => ({ ...prev, [key]: "" }));
    };
    const fullName = () =>
            form.group.trim()
                ? `${form.group.trim()}/${form.name.trim()}`
                : form.name.trim(),
        basic = form.auth === "basic",
        kind = basic ? "password" : "token",
        autoVar = () =>
            `GRAYLOG_${(fullName() || "STAGING").toUpperCase().replace(/[^A-Z0-9]/g, "_")}_${basic ? "PASSWORD" : "TOKEN"}`,
        secretVar = () => form.secret_env.trim() || autoVar();
    function formFields(): Fields {
        return {
            url: form.url.trim(),
            description: form.description.trim(),
            auth: form.auth,
            ca_bundle: form.ca_bundle.trim(),
            proxy: form.proxy.trim(),
            timezone: form.timezone.trim(),
            ...(basic
                ? { username: form.username.trim(), password_env: secretVar() }
                : { token_env: secretVar() }),
            ...(!form.verify_tls ? { verify_tls: false } : {}),
            ...(form.timeout ? { timeout: Number(form.timeout) } : {}),
        };
    }
    const validate = () => {
        const errs = {
            url: !form.url.trim()
                ? "The Graylog address is required"
                : /^https?:\/\//.test(form.url.trim())
                  ? ""
                  : "Start with http:// or https://",
            name: !form.name.trim()
                ? "Give the environment a name"
                : /^[A-Za-z0-9_.-]+$/.test(form.name.trim())
                  ? ""
                  : "Letters, digits, '_', '-' or '.' only",
        };
        setErrors(errs);
        if (errs.url || errs.name) {
            setTab("conn");
            setTimeout(
                () =>
                    document
                        .getElementById(errs.url ? "f_url" : "f_name")
                        ?.focus(),
                0,
            );
            return false;
        }
        return true;
    };
    const close = () => {
        envEditor.value = null;
    };
    const test = async () => {
        const r = await api<TestResult>("/api/test", "POST", {
            name: fullName().replace("/", "-") || "test",
            fields: formFields(),
            token: form.secret || null,
        });
        envTests.value = {
            ...envTests.value,
            [fullName()]: { ...r, at: Date.now() / 1000 },
        };
        return r;
    };
    const save = async () => {
        const name = fullName(),
            secret = form.secret;
        await api("/api/instances", "POST", {
            name,
            original_name: e?.name || null,
            fields: formFields(),
            make_default: form.default,
        });
        if (secret)
            await api("/api/secrets", "POST", {
                name: secretVar(),
                value: secret,
            });
        toast(
            secret
                ? `Saved ${name} and its ${kind} (on this machine only)`
                : `Saved ${name}`,
        );
        close();
        await loadState();
        void runDoctor();
        if (setup.value) void loadSetup();
    };
    const testSave = async () => {
        if (!validate()) return;
        if (!form.secret && !e?.secret_set) return save();
        const r = await test();
        if (r.ok) return save();
        setResult(r);
        setOffer(true);
        setTab("check");
    };
    const mine =
        e && !result
            ? (checks.value || []).filter((c) => c.instance === e.name)
            : [];
    const input = (
        key: Exclude<keyof Form, "default" | "verify_tls">,
        props: Record<string, unknown> = {},
    ) => (
        <input
            {...props}
            id={`f_${key}`}
            value={form[key]}
            onInput={(ev) => update(key, ev.currentTarget.value)}
            class={
                errors[key] ||
                (key === "secret_env" &&
                    form.secret_env &&
                    !ENV_NAME.test(form.secret_env))
                    ? "invalid"
                    : undefined
            }
        />
    );
    return (
        <>
            <Dialog
                id="envDlg"
                class="wide"
                data-noscrim
                aria-labelledby="envformTitle"
                open={!!editor}
                onClose={close}
            >
                <div class="dhead">
                    <h2 id="envformTitle">
                        {e
                            ? `Edit ${label(e)}`
                            : editor?.group
                              ? `Add environment to ${editor.group}`
                              : "Add environment"}
                    </h2>
                    <span class="spacer" />
                    <Close onClick={close} />
                </div>
                <Tabs
                    id="envTabs"
                    label="Environment"
                    options={[
                        ["conn", "Connection"],
                        ["check", "Check"],
                        ["fields", "Fields"],
                    ]}
                    value={tab}
                    onChange={setTab}
                />
                <div class="dbody scroll" ref={body}>
                    <Panel
                        tabs="envTabs"
                        id="conn"
                        value={tab}
                        className="pane"
                    >
                        <div
                            onKeyDown={(ev) => {
                                if (
                                    ev.key === "Enter" &&
                                    (ev.target as HTMLInputElement).tagName ===
                                        "INPUT" &&
                                    (ev.target as HTMLInputElement).type !==
                                        "checkbox"
                                ) {
                                    ev.preventDefault();
                                    void testSave();
                                }
                            }}
                            class="pane"
                        >
                            <div class="field">
                                <label class="label req" for="f_url">
                                    Graylog URL
                                </label>
                                <input
                                    id="f_url"
                                    placeholder="https://graylog.example.com"
                                    autoComplete="off"
                                    spellcheck={false}
                                    value={form.url}
                                    class={errors.url ? "invalid" : undefined}
                                    onInput={(ev) =>
                                        update("url", ev.currentTarget.value)
                                    }
                                    onChange={(ev) => {
                                        if (form.name.trim()) return;
                                        try {
                                            const word = new URL(
                                                ev.currentTarget.value.trim(),
                                            ).hostname
                                                .toLowerCase()
                                                .split(/[.\-_]/)
                                                .find((w) =>
                                                    [
                                                        "prod",
                                                        "production",
                                                        "uat",
                                                        "staging",
                                                        "stage",
                                                        "stg",
                                                        "preprod",
                                                        "dev",
                                                        "develop",
                                                        "test",
                                                        "qa",
                                                        "sandbox",
                                                        "sit",
                                                        "demo",
                                                        "local",
                                                    ].includes(w),
                                                );
                                            if (word)
                                                update(
                                                    "name",
                                                    word === "production"
                                                        ? "prod"
                                                        : word,
                                                );
                                        } catch {
                                            /* incomplete URL */
                                        }
                                    }}
                                />
                                <span
                                    class={`error${errors.url ? "" : " hidden"}`}
                                    id="err_url"
                                >
                                    {errors.url}
                                </span>
                            </div>
                            <div class="grid2">
                                <div class="field">
                                    <label class="label" for="f_group">
                                        Group
                                    </label>
                                    {input("group", {
                                        list: "groupList",
                                        placeholder:
                                            "erp, cxp, payment… (optional)",
                                        autoComplete: "off",
                                    })}
                                </div>
                                <div class="field">
                                    <label class="label req" for="f_name">
                                        Environment
                                    </label>
                                    {input("name", {
                                        list: "envList",
                                        placeholder: "prod, uat, sandbox…",
                                        autoComplete: "off",
                                    })}
                                    <span
                                        class={`error${errors.name ? "" : " hidden"}`}
                                        id="err_name"
                                    >
                                        {errors.name}
                                    </span>
                                </div>
                            </div>
                            <div class="field">
                                <span class="label">Sign in with</span>
                                <Segment
                                    label="Sign in with"
                                    value={form.auth}
                                    onChange={(v) => update("auth", v)}
                                    options={[
                                        ["token", "Access token"],
                                        ["basic", "Username and password"],
                                    ]}
                                />
                            </div>
                            <div
                                class={`field${basic ? "" : " hidden"}`}
                                id="usernameField"
                            >
                                <label class="label req" for="f_username">
                                    Username
                                </label>
                                {input("username", { autoComplete: "off" })}
                            </div>
                            <div class="field">
                                <label
                                    class="label"
                                    id="secretLabel"
                                    for="f_secret"
                                >
                                    {basic ? "Password" : "Token"}
                                </label>
                                {input("secret", {
                                    type: "password",
                                    autoComplete: "new-password",
                                    placeholder:
                                        e?.secret_source === "saved"
                                            ? "•••••••• saved; leave empty to keep it"
                                            : e?.secret_source === "env"
                                              ? `taken from $${e.secret_env}`
                                              : `paste the ${kind}`,
                                })}
                                <span class="hint" id="secretHint">
                                    {e?.secret_source === "saved"
                                        ? "A value is saved on this machine. Type a new one to replace it."
                                        : "Stored only on this machine, never in the repository."}
                                </span>
                            </div>
                            <div class="field">
                                <label class="label" for="f_description">
                                    Description
                                </label>
                                {input("description", {
                                    placeholder:
                                        "shown to the model, e.g. Production",
                                })}
                            </div>
                            <label class="check">
                                <input
                                    type="checkbox"
                                    id="f_default"
                                    checked={form.default}
                                    onChange={(ev) =>
                                        update(
                                            "default",
                                            ev.currentTarget.checked,
                                        )
                                    }
                                />
                                Use as the default environment
                            </label>
                            <details>
                                <summary>
                                    Advanced: TLS, proxy, timeout, timezone,
                                    environment variable
                                </summary>
                                <div class="grid2">
                                    <div class="field">
                                        <label class="check">
                                            <input
                                                type="checkbox"
                                                id="f_verify_tls"
                                                checked={form.verify_tls}
                                                onChange={(ev) =>
                                                    update(
                                                        "verify_tls",
                                                        ev.currentTarget
                                                            .checked,
                                                    )
                                                }
                                            />
                                            Verify TLS certificate
                                        </label>
                                    </div>
                                    <div class="field">
                                        <label class="label" for="f_timeout">
                                            Timeout (seconds)
                                        </label>
                                        {input("timeout", {
                                            type: "number",
                                            min: 1,
                                            placeholder: "30",
                                        })}
                                    </div>
                                    <div class="field">
                                        <label class="label" for="f_ca_bundle">
                                            CA bundle path
                                        </label>
                                        {input("ca_bundle", {
                                            placeholder:
                                                "/etc/ssl/internal-ca.pem",
                                        })}
                                    </div>
                                    <div class="field">
                                        <label class="label" for="f_proxy">
                                            Proxy
                                        </label>
                                        {input("proxy", {
                                            placeholder: "http://proxy:3128",
                                        })}
                                    </div>
                                    <div class="field">
                                        <label class="label" for="f_timezone">
                                            Timezone
                                        </label>
                                        {input("timezone", {
                                            placeholder: "Asia/Ho_Chi_Minh",
                                        })}
                                    </div>
                                    <div class="field">
                                        <label class="label" for="f_secret_env">
                                            Environment variable
                                        </label>
                                        {input("secret_env", {
                                            autoComplete: "off",
                                            spellcheck: false,
                                            placeholder: autoVar(),
                                        })}
                                        <span class="hint">
                                            Can also provide the{" "}
                                            <span id="varKind">{kind}</span>{" "}
                                            (CI, shared machines).
                                        </span>
                                    </div>
                                </div>
                            </details>
                        </div>
                    </Panel>
                    <Panel
                        tabs="envTabs"
                        id="check"
                        value={tab}
                        className="pane"
                    >
                        <div class="muted" id="envCheckNote">
                            {result
                                ? "The last test of this environment:"
                                : e
                                  ? mine.length
                                      ? "The last check of this environment:"
                                      : "Not checked yet. Test now connects with what is entered on the Connection tab."
                                  : "Test now connects with what is entered on the Connection tab, before anything is saved."}
                        </div>
                        <div id="envChecks">
                            {mine.map((c) => (
                                <div class="chk">
                                    <span
                                        class={`badge ${c.status === "ok" ? "ok" : c.status === "warn" ? "warn" : "fail"}`}
                                    >
                                        {c.status}
                                    </span>
                                    <span class="mono small">{c.name}</span>
                                    <span class="small">
                                        {c.detail}
                                        {c.fix && (
                                            <div class="muted">
                                                Fix: {c.fix}
                                            </div>
                                        )}
                                    </span>
                                </div>
                            ))}
                        </div>
                        <div id="envResult" aria-live="polite">
                            {result && (
                                <div
                                    class={`banner ${result.ok ? "ok" : "fail"} flush`}
                                >
                                    <Icon name={result.ok ? "ok" : "fail"} />
                                    <div class="text">
                                        <b>
                                            {result.ok
                                                ? `Connected to Graylog ${result.version}`
                                                : result.error}
                                        </b>
                                        {result.ok ? (
                                            <div class="small muted">
                                                {result.streams} streams,{" "}
                                                {num(result.messages_24h)}{" "}
                                                messages in 24h, messages via{" "}
                                                {result.message_api},
                                                aggregations via{" "}
                                                {result.aggregation_api}
                                            </div>
                                        ) : (
                                            <>
                                                {result.fix && (
                                                    <div class="small muted">
                                                        Fix: {result.fix}
                                                    </div>
                                                )}
                                                {offer && (
                                                    <div class="mt-xs">
                                                        <AsyncButton
                                                            class="btn sm"
                                                            id="saveAnyway"
                                                            run={save}
                                                        >
                                                            Save anyway
                                                        </AsyncButton>
                                                    </div>
                                                )}
                                            </>
                                        )}
                                    </div>
                                </div>
                            )}
                        </div>
                        <div>
                            <AsyncButton
                                class="btn"
                                id="testEnv"
                                run={async () => {
                                    if (validate()) {
                                        const r = await test();
                                        setResult(r);
                                        setOffer(false);
                                        setTab("check");
                                    }
                                }}
                            >
                                Test now
                            </AsyncButton>
                        </div>
                    </Panel>
                    <Panel
                        tabs="envTabs"
                        id="fields"
                        value={tab}
                        className="pane"
                    >
                        <div class="muted" id="envFieldsNote">
                            {e
                                ? "Field names, queries and connection options for this environment are set on the Log fields page, where each value shows whether it is inherited from the group or from all environments."
                                : "Save the environment first. Its log fields are set on the Log fields page."}
                        </div>
                        <div>
                            <button
                                class="btn"
                                id="envFieldsGo"
                                disabled={!e}
                                onClick={async () => {
                                    if (!e) return;
                                    close();
                                    if (await navigate("fields")) {
                                        fieldsScope.value = `instance:${e.name}`;
                                        scopeChosen.value = true;
                                    }
                                }}
                            >
                                Open Log fields for this environment
                            </button>
                        </div>
                    </Panel>
                </div>
                <div class="dfoot">
                    <span class="spacer" />
                    <button class="btn ghost" onClick={close}>
                        Cancel
                    </button>
                    <AsyncButton
                        class="btn primary"
                        id="saveEnv"
                        run={testSave}
                    >
                        Test and save
                    </AsyncButton>
                </div>
            </Dialog>
            <datalist id="groupList">
                {state.value?.groups.map((g) => (
                    <option value={g.name} />
                ))}
            </datalist>
            <datalist id="envList">
                {[
                    ...new Set([
                        ...Object.keys(state.value?.environments || {}),
                        ...instances.value
                            .map((e) => e.environment)
                            .filter(Boolean),
                    ]),
                ].map((n) => (
                    <option value={n || ""} />
                ))}
            </datalist>
        </>
    );
}
