import { useEffect, useLayoutEffect, useRef, useState } from "preact/hooks";
import {
    currentPage,
    density,
    detectRequest,
    instances,
    label,
    navigate,
    openEnv,
    openEnvByName,
    pages,
    paletteOpen,
    playground,
    runDoctor,
    setEnv,
    settingsOpen,
    setup,
    state,
    theme,
} from "../store";
import {
    SCHEMES,
    originOf,
    setDensity,
    setMode,
    setPalette,
    toggleTheme,
} from "../appearance";
import { autostartOff, checkUpdates, finishSetup, setupSteps } from "../setup";
import { attempt } from "../store";
import { Dialog, Icon, type IconName } from "./UI";
interface Item {
    icon: IconName;
    text: string;
    kind: string;
    run: () => unknown;
}
function paletteItems(): Item[] {
    const items: Item[] = Object.entries(pages).map(([id, [title]]) => ({
        icon: "page",
        text: id === "config" ? "Open raw config" : title,
        kind: "page",
        run: () => navigate(id),
    }));
    items.push(
        {
            icon: "plus",
            text: "Add environment",
            kind: "action",
            run: () => openEnv(null),
        },
        {
            icon: "ok",
            text: "Re-check every environment",
            kind: "action",
            run: async () => {
                if (await navigate("home")) await runDoctor();
            },
        },
    );
    if (setup.value && setupSteps().some((x) => !x.ok))
        items.push({
            icon: "bolt",
            text: "Finish setup",
            kind: "action",
            run: async () => {
                if (await navigate("home")) await finishSetup();
            },
        });
    items.push({
        icon: "ok",
        text: "Check for updates",
        kind: "action",
        run: checkUpdates,
    });
    if (setup.value?.autostart.enabled)
        items.push({
            icon: "page",
            text: "Do not start the server at login",
            kind: "action",
            run: autostartOff,
        });
    items.push(
        {
            icon: "search",
            text: "Detect log fields",
            kind: "action",
            run: async () => {
                if (await navigate("fields")) detectRequest.value++;
            },
        },
        {
            icon: theme.value === "dark" ? "sun" : "moon",
            text: "Toggle light / dark theme",
            kind: "appearance",
            run: () =>
                toggleTheme(originOf(document.getElementById("themeBtn"))),
        },
        {
            icon: "page",
            text: "Open settings",
            kind: "settings",
            run: () => {
                settingsOpen.value = true;
            },
        },
    );
    for (const s of SCHEMES)
        items.push({
            icon: "page",
            text: `Scheme: ${s.name}`,
            kind: "scheme",
            run: () => setPalette(s.id),
        });
    for (const m of ["system", "light", "dark"])
        items.push({
            icon: "page",
            text: `Appearance: ${m}`,
            kind: "appearance",
            run: () =>
                setMode(m, originOf(document.getElementById("themeBtn"))),
        });
    items.push(
        {
            icon: "page",
            text: `Density: ${density.value === "compact" ? "comfortable" : "compact"}`,
            kind: "settings",
            run: () =>
                setDensity(
                    density.value === "compact" ? "comfortable" : "compact",
                ),
        },
        {
            icon: "db",
            text: "Environment: all",
            kind: "context",
            run: () => setEnv(null),
        },
    );
    for (const e of instances.value)
        items.push({
            icon: "db",
            text: `Environment: ${label(e)}`,
            kind: "context",
            run: () => setEnv(e.name),
        });
    for (const e of instances.value)
        items.push({
            icon: "db",
            text: `Edit ${label(e)}`,
            kind: e.secret_set ? "environment" : "missing token",
            run: () => openEnvByName(e.name),
        });
    for (const t of state.value?.tools || [])
        items.push({
            icon: "page",
            text: `Run ${t}`,
            kind: "playground",
            run: async () => {
                if (await navigate("playground"))
                    playground.value = { tool: t };
            },
        });
    return items;
}
export function Palette() {
    const open = paletteOpen.value,
        [query, setQuery] = useState(""),
        [selected, setSelected] = useState(0),
        input = useRef<HTMLInputElement>(null),
        words = query.trim().toLowerCase().split(/\s+/).filter(Boolean),
        items = paletteItems()
            .filter((i) =>
                words.every((w) =>
                    `${i.text} ${i.kind}`.toLowerCase().includes(w),
                ),
            )
            .slice(0, 12),
        sel = Math.min(selected, Math.max(0, items.length - 1));
    useLayoutEffect(() => {
        if (open) {
            setQuery("");
            setSelected(0);
            input.current?.focus();
        }
    }, [open]);
    useEffect(() => {
        if (open)
            document
                .getElementById(`pal${sel}`)
                ?.scrollIntoView({ block: "nearest" });
    }, [open, sel]);
    const close = () => {
            paletteOpen.value = false;
        },
        run = (i: number) => {
            const item = items[i];
            if (item) {
                close();
                void attempt(item.run);
            }
        };
    return (
        <Dialog
            id="paletteWrap"
            class="layer-dialog"
            animated
            aria-label="Jump to or do"
            open={open}
            onClose={close}
        >
            <div class="palette">
                <input
                    ref={input}
                    id="paletteInput"
                    placeholder="Type a page, an environment or an action…"
                    autoComplete="off"
                    role="combobox"
                    aria-expanded={open}
                    aria-controls="paletteList"
                    aria-activedescendant={
                        items.length ? `pal${sel}` : undefined
                    }
                    value={query}
                    onInput={(e) => {
                        setQuery(e.currentTarget.value);
                        setSelected(0);
                    }}
                    onKeyDown={(e) => {
                        if (e.key === "ArrowDown") {
                            e.preventDefault();
                            setSelected(Math.min(items.length - 1, sel + 1));
                        } else if (e.key === "ArrowUp") {
                            e.preventDefault();
                            setSelected(Math.max(0, sel - 1));
                        } else if (e.key === "Enter") {
                            e.preventDefault();
                            run(sel);
                        }
                    }}
                />
                <ul id="paletteList" role="listbox">
                    {items.length ? (
                        items.map((i, n) => (
                            <li
                                role="option"
                                id={`pal${n}`}
                                aria-selected={n === sel}
                                onClick={() => run(n)}
                            >
                                <Icon name={i.icon} />
                                <span>{i.text}</span>
                                <span class="kind">{i.kind}</span>
                            </li>
                        ))
                    ) : (
                        <li class="none" role="option" aria-disabled="true">
                            Nothing matches
                        </li>
                    )}
                </ul>
            </div>
        </Dialog>
    );
}
