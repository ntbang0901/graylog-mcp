import type { ComponentChildren } from "preact";
import { useEffect, useLayoutEffect, useRef, useState } from "preact/hooks";
import {
    banner,
    checks,
    confirmation,
    context,
    currentEnv,
    currentPage,
    dirty,
    envEditor,
    envHealth,
    envPickerOpen,
    failing,
    followAddress,
    instances,
    label,
    mode,
    navigate,
    pageTitle,
    paletteOpen,
    setEnv,
    settingsOpen,
    setup,
    state,
    theme,
    usage,
} from "../store";
import { attempt } from "../store";
import {
    SCHEMES,
    originOf,
    setMode,
    setPalette,
    toggleTheme,
} from "../appearance";
import { token } from "../api";
import { Icon, modKey, AsyncButton } from "./UI";
import { Settings } from "./Settings";
import { ConfirmDialog } from "./ConfirmDialog";
import { Toasts } from "./Toasts";

function EnvPicker() {
    const [q, setQ] = useState(""),
        [sel, setSel] = useState(0),
        [pos, setPos] = useState({ top: 0, left: 0 }),
        ref = useRef<HTMLDivElement>(null),
        input = useRef<HTMLInputElement>(null);
    const open = envPickerOpen.value;
    const options = [
        { key: null as string | null, text: "All environments", e: null },
        ...instances.value.map((e) => ({ key: e.name, text: label(e), e })),
    ].filter((o) => !q || o.text.toLowerCase().includes(q.toLowerCase()));
    const selected = Math.min(sel, Math.max(0, options.length - 1));
    const close = (back = true) => {
        envPickerOpen.value = false;
        if (back) document.getElementById("envBtn")?.focus();
    };
    useLayoutEffect(() => {
        if (!open) return;
        const b = document.getElementById("envBtn"),
            r = b?.getBoundingClientRect();
        if (r)
            setPos({
                top: r.bottom + 6,
                left: Math.max(8, Math.min(r.left, innerWidth - 328)),
            });
        setQ("");
        setSel(
            Math.max(
                0,
                [null, ...instances.value.map((e) => e.name)].indexOf(
                    currentEnv.value,
                ),
            ),
        );
        input.current?.focus();
    }, [open]);
    useEffect(() => {
        const down = (e: PointerEvent) => {
            if (
                open &&
                !(e.target instanceof Element && e.target.closest("#envBtn")) &&
                !ref.current?.contains(e.target as Node)
            )
                close(false);
        };
        document.addEventListener("pointerdown", down);
        return () => document.removeEventListener("pointerdown", down);
    }, [open]);
    useEffect(() => {
        if (open)
            document
                .getElementById(`eo${selected}`)
                ?.scrollIntoView({ block: "nearest" });
    }, [selected, open]);
    return (
        <div
            ref={ref}
            class={`pop${open ? " open" : ""}`}
            id="envPop"
            role="dialog"
            aria-label="Choose environment"
            style={{ top: `${pos.top}px`, left: `${pos.left}px` }}
        >
            <input
                ref={input}
                id="envFilter"
                placeholder="Filter environments…"
                autoComplete="off"
                role="combobox"
                aria-expanded={open}
                aria-controls="envOptions"
                aria-autocomplete="list"
                aria-activedescendant={
                    options.length ? `eo${selected}` : undefined
                }
                value={q}
                onInput={(e) => {
                    setQ(e.currentTarget.value);
                    setSel(0);
                }}
                onKeyDown={(e) => {
                    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
                        e.preventDefault();
                        setSel(
                            (selected +
                                (e.key === "ArrowDown" ? 1 : -1) +
                                options.length) %
                                Math.max(1, options.length),
                        );
                    } else if (e.key === "Enter") {
                        e.preventDefault();
                        if (options[selected]) {
                            setEnv(options[selected].key);
                            close();
                        }
                    } else if (e.key === "Escape") {
                        e.preventDefault();
                        close();
                    }
                }}
            />
            <ul id="envOptions" role="listbox" aria-label="Environments">
                {options.map((o, i) => (
                    <>
                        {o.e?.group &&
                            o.e.group !== options[i - 1]?.e?.group && (
                                <li class="g glabel" role="presentation">
                                    {o.e.group}
                                </li>
                            )}
                        <li
                            class="opt"
                            role="option"
                            id={`eo${i}`}
                            aria-selected={i === selected}
                            onPointerMove={() => setSel(i)}
                            onClick={() => {
                                setEnv(o.key);
                                close();
                            }}
                        >
                            <span class="mark">
                                {o.key === currentEnv.value ? "✓" : ""}
                            </span>
                            <span class={o.e ? "k" : ""}>{o.text}</span>
                            <span
                                class={`s${o.e && envHealth(o.e).cls === "fail" ? " bad" : ""}`}
                            >
                                {!o.e
                                    ? instances.value.length
                                    : envHealth(o.e).cls === "fail"
                                      ? "failing"
                                      : envHealth(o.e).cls === "warn"
                                        ? "warning"
                                        : ""}
                            </span>
                        </li>
                    </>
                ))}
                {!options.length && (
                    <li class="muted small empty-opt">
                        No environment matches
                    </li>
                )}
            </ul>
        </div>
    );
}
export function Shell({
    children,
    overlays,
}: {
    children: ComponentChildren;
    overlays?: ComponentChildren;
}) {
    useEffect(() => {
        document.title = `${pageTitle.value} · graylog-mcp`;
        document
            .querySelector<HTMLElement>('.nav[aria-current="page"]')
            ?.scrollIntoView({ block: "nearest", inline: "nearest" });
    }, [currentPage.value]);
    useEffect(() => {
        const mq = matchMedia("(prefers-color-scheme: dark)"),
            change = () => {
                if (mode.value === "system") setMode("system");
            };
        mq.addEventListener("change", change);
        return () => mq.removeEventListener("change", change);
    }, []);
    useEffect(() => {
        let pending: ReturnType<typeof setTimeout> | undefined;
        const key = (e: KeyboardEvent) => {
            const k = e.key.toLowerCase(),
                mod = e.metaKey || e.ctrlKey;
            if (mod && k === "k") {
                e.preventDefault();
                envPickerOpen.value = false;
                paletteOpen.value = true;
                return;
            }
            if (mod && k === "s" && dirty.value) {
                e.preventDefault();
                void attempt(dirty.value.save);
                return;
            }
            if (
                mod &&
                e.key === "Enter" &&
                currentPage.value === "playground"
            ) {
                document.getElementById("runTool")?.click();
                return;
            }
            if (e.key === "Escape") {
                if (envPickerOpen.value) {
                    envPickerOpen.value = false;
                    document.getElementById("envBtn")?.focus();
                }
                return;
            }
            if (
                mod ||
                e.altKey ||
                (e.target as HTMLElement).closest(
                    "input,textarea,select,[contenteditable]",
                ) ||
                confirmation.value ||
                paletteOpen.value ||
                envPickerOpen.value ||
                document.querySelector("dialog[open]")
            )
                return;
            const go: Record<string, string> = {
                o: "home",
                a: "activity",
                l: "logs",
                e: "envs",
                f: "fields",
                r: "redaction",
                p: "playground",
                c: "connect",
            };
            if (pending) {
                e.preventDefault();
                clearTimeout(pending);
                pending = undefined;
                if (go[k]) {
                    e.preventDefault();
                    void navigate(go[k]);
                }
                return;
            }
            if (k === "/") {
                e.preventDefault();
                paletteOpen.value = true;
            } else if (k === ",") {
                e.preventDefault();
                settingsOpen.value = true;
            } else if (k === "t")
                toggleTheme(originOf(document.getElementById("themeBtn")));
            else if (k === "e") {
                e.preventDefault();
                envPickerOpen.value = true;
            } else if (k === "g")
                pending = setTimeout(() => {
                    pending = undefined;
                }, 1200);
            else if (k >= "1" && k <= String(SCHEMES.length) && k.length === 1)
                setPalette(SCHEMES[+k - 1].id);
        };
        const before = (e: BeforeUnloadEvent) => {
            if (dirty.value) {
                e.preventDefault();
                e.returnValue = "";
            }
        };
        document.addEventListener("keydown", key);
        window.addEventListener("beforeunload", before);
        window.addEventListener("popstate", followAddress);
        return () => {
            clearTimeout(pending);
            document.removeEventListener("keydown", key);
            window.removeEventListener("beforeunload", before);
            window.removeEventListener("popstate", followAddress);
        };
    }, []);
    const up = setup.value ? setup.value.server.running : null,
        s = state.value,
        d = dirty.value;
    const navIcons = {
        home: (
            <svg class="i" viewBox="0 0 24 24" aria-hidden="true">
                <path d="M3 11l9-7 9 7" />
                <path d="M5 10v10h14V10" />
            </svg>
        ),
        activity: (
            <svg class="i" viewBox="0 0 24 24" aria-hidden="true">
                <path d="M3 12h4l3-8 4 16 3-8h4" />
            </svg>
        ),
        logs: (
            <svg class="i" viewBox="0 0 24 24" aria-hidden="true">
                <path d="M4 6h16M4 12h10M4 18h13" />
            </svg>
        ),
        envs: (
            <svg class="i" viewBox="0 0 24 24" aria-hidden="true">
                <rect x="3" y="4" width="18" height="6" rx="1.5" />
                <rect x="3" y="14" width="18" height="6" rx="1.5" />
                <path d="M7 7h.01M7 17h.01" />
            </svg>
        ),
        fields: (
            <svg class="i" viewBox="0 0 24 24" aria-hidden="true">
                <path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12" />
                <circle cx="16" cy="6" r="2" />
                <circle cx="10" cy="12" r="2" />
                <circle cx="18" cy="18" r="2" />
            </svg>
        ),
        redaction: (
            <svg class="i" viewBox="0 0 24 24" aria-hidden="true">
                <path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6l8-3z" />
            </svg>
        ),
        playground: (
            <svg class="i" viewBox="0 0 24 24" aria-hidden="true">
                <path d="M6 4l14 8-14 8V4z" />
            </svg>
        ),
        connect: (
            <svg class="i" viewBox="0 0 24 24" aria-hidden="true">
                <path d="M10 14a4 4 0 005.7 0l3-3a4 4 0 00-5.7-5.7l-1 1" />
                <path d="M14 10a4 4 0 00-5.7 0l-3 3a4 4 0 005.7 5.7l1-1" />
            </svg>
        ),
    };
    const nav = (
        id: string,
        title: string,
        icon: Parameters<typeof Icon>[0]["name"],
        count = 0,
    ) => (
        <button
            class="nav"
            data-page={id}
            aria-current={currentPage.value === id ? "page" : undefined}
            onClick={() => navigate(id)}
        >
            {navIcons[id as keyof typeof navIcons]}
            <span class="label-text">{title}</span>
            {
                <span
                    id={
                        id === "envs"
                            ? "navEnvs"
                            : id === "activity"
                              ? "navActs"
                              : undefined
                    }
                    class={`badge fail count${count ? "" : " hidden"}`}
                    title={`${count} ${id === "envs" ? "failing" : "errors in this range"}`}
                >
                    {count}
                </span>
            }
        </button>
    );
    return (
        <>
            <div class="app">
                <nav class="sidebar" aria-label="Main">
                    <div class="brand">
                        <span class="logo">
                            <svg
                                class="i"
                                viewBox="0 0 24 24"
                                aria-hidden="true"
                            >
                                <path d="M4 16l5-6 4 4 7-9" />
                            </svg>
                        </span>
                        <span class="brand-name">graylog-mcp</span>
                    </div>
                    <div class="navgroup">
                        <div class="glabel">Monitor</div>
                        {nav("home", "Overview", "page")}
                        {nav(
                            "activity",
                            "Activity",
                            "bolt",
                            usage.value?.totals.errors,
                        )}
                        {nav("logs", "Logs", "lines")}
                    </div>
                    <div class="navgroup">
                        <div class="glabel">Configure</div>
                        {nav("envs", "Environments", "db", failing.value)}
                        {nav("fields", "Log fields", "edit")}
                        {nav("redaction", "Redaction", "key")}
                    </div>
                    <div class="navgroup">
                        <div class="glabel">Tools</div>
                        {nav("playground", "Playground", "page")}
                        {nav("connect", "Connect", "page")}
                    </div>
                    <div class="foot">
                        <span class="ver" id="ver">
                            {setup.value?.install.commit
                                ? `v${setup.value.install.version} · ${setup.value.install.commit.slice(0, 7)}`
                                : s
                                  ? `v${s.version}`
                                  : ""}
                        </span>
                    </div>
                </nav>
                <div class="main">
                    <header class="topbar">
                        <span class="crumb">
                            graylog-mcp /{" "}
                            <b id="crumb">{pageTitle.value.toLowerCase()}</b>
                        </span>
                        <div class="right">
                            <button
                                class="ctl envbtn"
                                id="envBtn"
                                aria-haspopup="listbox"
                                aria-expanded={envPickerOpen.value}
                                aria-controls="envPop"
                                title="Environment (E)"
                                onClick={() => {
                                    envPickerOpen.value = !envPickerOpen.value;
                                }}
                            >
                                <span class="k">env</span>
                                <span class="v" id="envVal">
                                    {context.value
                                        ? label(context.value)
                                        : "All environments"}
                                </span>
                                <svg
                                    class="i"
                                    viewBox="0 0 24 24"
                                    aria-hidden="true"
                                >
                                    <path d="M6 9l6 6 6-6" />
                                </svg>
                            </button>
                            <button
                                class="kbar"
                                id="paletteBtn"
                                aria-label={`Jump to or do… ${modKey()} K`}
                                title="Search and run commands"
                                onClick={() => {
                                    paletteOpen.value = true;
                                }}
                            >
                                <Icon name="search" />
                                <span>Jump to or do…</span>
                                <span class="kbd" id="kbdHint">
                                    {modKey()} K
                                </span>
                            </button>
                            <button
                                class="ctl icon-btn"
                                id="settingsBtn"
                                aria-label="Settings"
                                title="Settings (,)"
                                onClick={() => {
                                    settingsOpen.value = true;
                                }}
                            >
                                <svg
                                    class="i"
                                    viewBox="0 0 24 24"
                                    aria-hidden="true"
                                >
                                    <circle cx="12" cy="12" r="3" />
                                    <path d="M19.4 15a1.7 1.7 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.8-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 11-4 0v-.1a1.7 1.7 0 00-1.1-1.5 1.7 1.7 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.8 1.7 1.7 0 00-1.5-1H3a2 2 0 110-4h.1a1.7 1.7 0 001.5-1.1 1.7 1.7 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.8.3H9a1.7 1.7 0 001-1.5V3a2 2 0 114 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.8V9a1.7 1.7 0 001.5 1H21a2 2 0 110 4h-.1a1.7 1.7 0 00-1.5 1z" />
                                </svg>
                            </button>
                            <button
                                class="ctl icon-btn"
                                id="themeBtn"
                                aria-label={`Switch to ${theme.value === "dark" ? "light" : "dark"} theme`}
                                title="Light or dark (T)"
                                onClick={(e) =>
                                    toggleTheme(originOf(e.currentTarget))
                                }
                            >
                                <Icon
                                    name={
                                        theme.value === "dark" ? "sun" : "moon"
                                    }
                                />
                            </button>
                        </div>
                    </header>
                    <main class="content">
                        <div id="banner">
                            {!token.value ? (
                                <div class="banner fail">
                                    <Icon name="fail" />
                                    <div class="text">
                                        This browser is not signed in to this
                                        page. Run <code>graylog-mcp ui</code>{" "}
                                        once (or <code>graylog-mcp start</code>{" "}
                                        for the shared server): it opens the
                                        page with its access token. Then reload
                                        this tab.
                                    </div>
                                </div>
                            ) : banner.value ? (
                                <div class="banner fail">
                                    <Icon name="fail" />
                                    <div class="text">{banner.value}</div>
                                </div>
                            ) : s?.error ? (
                                <div class="banner fail">
                                    <Icon name="fail" />
                                    <div class="text">
                                        <b>The config file has a problem.</b>{" "}
                                        {s.error}
                                        <div class="small mt-xs">
                                            Fix it in{" "}
                                            <a
                                                href="#envs"
                                                onClick={(e) => {
                                                    e.preventDefault();
                                                    void navigate("envs");
                                                }}
                                            >
                                                Environments
                                            </a>{" "}
                                            or the{" "}
                                            <a
                                                href="#config"
                                                onClick={(e) => {
                                                    e.preventDefault();
                                                    void navigate("config");
                                                }}
                                            >
                                                config file
                                            </a>
                                            .
                                        </div>
                                    </div>
                                </div>
                            ) : null}
                        </div>
                        {children}
                    </main>
                    <footer class="statusbar">
                        <span
                            class={`srv${up === null ? "" : up ? " up" : " down"}`}
                            id="sbServer"
                        >
                            <span class="dot" />
                            <span id="sbServerText">
                                {up === null
                                    ? "checking the server…"
                                    : up
                                      ? "server running"
                                      : "server not running"}
                            </span>
                        </span>
                        <span class="sep" aria-hidden="true">
                            │
                        </span>
                        <a
                            href="#config"
                            id="sbConfig"
                            title="Open the raw config file"
                            hidden={!s?.config_path}
                            onClick={(e) => {
                                e.preventDefault();
                                void navigate("config");
                            }}
                        >
                            {s?.config_path}
                        </a>
                        <span class="sep" aria-hidden="true">
                            │
                        </span>
                        <button
                            class={`badge ${s?.error ? "fail" : !s?.exists ? "warn" : "ok"}`}
                            id="cfgStatus"
                            title={s?.config_path}
                            onClick={() => navigate("config")}
                        >
                            {s
                                ? s.error
                                    ? "config needs a fix"
                                    : !s.exists
                                      ? "no config yet"
                                      : "config valid"
                                : ""}
                        </button>
                        <span
                            class={`sbdirty${d ? "" : " hidden"}`}
                            id="sbDirty"
                        >
                            {d && `unsaved changes in ${d.what}`}
                        </span>
                    </footer>
                </div>
            </div>
            <EnvPicker />
            <Settings />
            {overlays}
            <div
                class={`savebar${d ? "" : " hidden"}`}
                id="savebar"
                role="region"
                aria-label="Unsaved changes"
            >
                <span>
                    <b>Unsaved changes</b>{" "}
                    <span class="muted" id="savebarWhat">
                        {d && `in ${d.what}`}
                    </span>
                </span>
                <button
                    class="btn sm ghost"
                    id="discardBtn"
                    onClick={() => {
                        const prev = dirty.value;
                        dirty.value = null;
                        prev?.discard();
                    }}
                >
                    Discard
                </button>
                <AsyncButton
                    class="btn sm primary"
                    id="saveBtn"
                    run={() => dirty.value?.save()}
                >
                    Save{" "}
                    <span class="kbd" id="saveKbd">
                        {modKey()} S
                    </span>
                </AsyncButton>
            </div>
            <ConfirmDialog />
            <Toasts />
        </>
    );
}
