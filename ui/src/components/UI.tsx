import type { ComponentChildren, JSX } from "preact";
import { useEffect, useLayoutEffect, useRef, useState } from "preact/hooks";
import {
    currentEnv,
    state,
    loadErrors,
    currentPage,
    context,
    label,
    pages,
    range,
    setEnv,
    attempt,
} from "../store";
import {
    REDUCED,
    movePill,
    revealPage,
    layerIn,
    layerOut,
} from "../scripts/motion";
export type IconName =
    | "ok"
    | "fail"
    | "info"
    | "warn"
    | "edit"
    | "trash"
    | "plus"
    | "key"
    | "page"
    | "bolt"
    | "sun"
    | "moon"
    | "search"
    | "db"
    | "lines";
export function Icon({
    name = "page",
    small = false,
}: {
    name?: IconName;
    small?: boolean;
}) {
    const icons = {
        ok: (
            <svg class="i" viewBox="0 0 24 24" aria-hidden="true">
                <path d="M5 12l5 5 9-10" />
            </svg>
        ),
        fail: (
            <svg class="i" viewBox="0 0 24 24" aria-hidden="true">
                <circle cx="12" cy="12" r="9" />
                <path d="M15 9l-6 6M9 9l6 6" />
            </svg>
        ),
        info: (
            <svg class="i" viewBox="0 0 24 24" aria-hidden="true">
                <circle cx="12" cy="12" r="9" />
                <path d="M12 11v5M12 8h.01" />
            </svg>
        ),
        warn: (
            <svg class="i" viewBox="0 0 24 24" aria-hidden="true">
                <path d="M12 4l9 16H3l9-16z" />
                <path d="M12 10v4M12 17h.01" />
            </svg>
        ),
        edit: (
            <svg class="i" viewBox="0 0 24 24" aria-hidden="true">
                <path d="M4 20h4L19 9l-4-4L4 16v4z" />
            </svg>
        ),
        trash: (
            <svg class="i" viewBox="0 0 24 24" aria-hidden="true">
                <path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13" />
            </svg>
        ),
        plus: (
            <svg class="i" viewBox="0 0 24 24" aria-hidden="true">
                <path d="M12 5v14M5 12h14" />
            </svg>
        ),
        key: (
            <svg class="i" viewBox="0 0 24 24" aria-hidden="true">
                <circle cx="8" cy="15" r="4" />
                <path d="M11 12l9-9M17 6l3 3" />
            </svg>
        ),
        page: (
            <svg class="i" viewBox="0 0 24 24" aria-hidden="true">
                <path d="M9 6l6 6-6 6" />
            </svg>
        ),
        bolt: (
            <svg class="i" viewBox="0 0 24 24" aria-hidden="true">
                <path d="M13 3L4 14h7l-1 7 9-11h-7l1-7z" />
            </svg>
        ),
        sun: (
            <svg class="i" viewBox="0 0 24 24" aria-hidden="true">
                <circle cx="12" cy="12" r="4" />
                <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
            </svg>
        ),
        moon: (
            <svg class="i" viewBox="0 0 24 24" aria-hidden="true">
                <path d="M20 15A8 8 0 019 4a8 8 0 1011 11z" />
            </svg>
        ),
        search: (
            <svg class="i" viewBox="0 0 24 24" aria-hidden="true">
                <circle cx="11" cy="11" r="7" />
                <path d="M21 21l-4.3-4.3" />
            </svg>
        ),
        db: (
            <svg class="i" viewBox="0 0 24 24" aria-hidden="true">
                <rect x="3" y="4" width="18" height="6" rx="1.5" />
                <rect x="3" y="14" width="18" height="6" rx="1.5" />
            </svg>
        ),
        lines: (
            <svg class="i" viewBox="0 0 24 24" aria-hidden="true">
                <path d="M4 6h16M4 12h10M4 18h13" />
            </svg>
        ),
    };
    return small ? <span class="icon-small">{icons[name]}</span> : icons[name];
}
export function AsyncButton({
    run,
    children,
    ...props
}: Omit<JSX.IntrinsicElements["button"], "onClick"> & {
    run: () => unknown;
    children?: ComponentChildren;
}) {
    const [busy, setBusy] = useState(false);
    return (
        <button
            {...props}
            disabled={busy || props.disabled}
            class={`${props.class || "btn"}${busy ? " loading" : ""}`}
            onClick={async () => {
                setBusy(true);
                try {
                    await attempt(run);
                } finally {
                    setBusy(false);
                }
            }}
        >
            {children}
        </button>
    );
}
export function Page({
    id,
    tools,
    children,
}: {
    id: keyof typeof pages;
    tools?: ComponentChildren;
    children?: ComponentChildren;
}) {
    const ref = useRef<HTMLElement>(null),
        first = useRef(true),
        active = currentPage.value === id && !!state.value;
    useLayoutEffect(() => {
        if (active && ref.current) {
            if (!first.current) {
                // Signal subscribers commit separately; wait for the previous page to hide.
                const frame = requestAnimationFrame(() => {
                    if (ref.current) revealPage(ref.current);
                });
                return () => cancelAnimationFrame(frame);
            }
            first.current = false;
        } else if (currentPage.value !== id) {
            first.current = false;
        }
    }, [active]);
    return (
        <section
            ref={ref}
            class={`page${active ? " active" : ""}`}
            id={id}
            data-title={pages[id][0]}
            data-sub={pages[id][1]}
        >
            <div class="screen-head">
                <div>
                    <h1>{pages[id][0]}</h1>
                    <div class="sub">{pages[id][1]}</div>
                </div>
                {tools && <div class="page-tools tools">{tools}</div>}
            </div>
            {children}
        </section>
    );
}
export function Segment({
    id,
    label,
    options,
    value,
    onChange,
}: {
    id?: string;
    label: string;
    options: readonly (readonly [string, string])[];
    value: string;
    onChange: (v: string) => void;
}) {
    const ref = useRef<HTMLDivElement>(null),
        pill = useRef<HTMLSpanElement>(null),
        ready = useRef(false);
    useLayoutEffect(() => {
        const sync = () => {
            const el = ref.current,
                p = pill.current,
                b = el?.querySelector<HTMLButtonElement>("[aria-pressed=true]");
            if (!el || !p || !b || !el.offsetParent) return;
            if (!ready.current || REDUCED.matches) {
                p.style.width = b.offsetWidth + "px";
                p.style.height = b.offsetHeight + "px";
                p.style.transform = `translate(${b.offsetLeft}px, ${b.offsetTop}px)`;
                ready.current = true;
            } else
                movePill(
                    p,
                    b.offsetLeft,
                    b.offsetTop,
                    b.offsetWidth,
                    b.offsetHeight,
                );
        };
        sync();
        const ro = new ResizeObserver(sync);
        if (ref.current) ro.observe(ref.current);
        return () => ro.disconnect();
    }, [value, currentPage.value]);
    return (
        <div ref={ref} class="seg" id={id} role="group" aria-label={label}>
            <span class="pill" aria-hidden="true" ref={pill} />
            {options.map(([key, text]) => (
                <button
                    key={key}
                    type="button"
                    aria-pressed={key === value}
                    onClick={() => onChange(key)}
                >
                    {text}
                </button>
            ))}
        </div>
    );
}
export function Range() {
    return (
        <Segment
            label="Time range"
            value={range.value}
            options={[
                ["24h", "24 hours"],
                ["7d", "7 days"],
            ]}
            onChange={(v) => {
                range.value = v;
            }}
        />
    );
}
export function ContextChip({ id = "ctxChip" }: { id?: string }) {
    return (
        <span class="ctxchip" id={id} hidden={!currentEnv.value}>
            <span>{context.value ? `env: ${label(context.value)}` : ""}</span>
            <button
                type="button"
                aria-label="Show every environment"
                onClick={() => setEnv(null)}
            >
                <svg class="i xs" viewBox="0 0 24 24" aria-hidden="true">
                    <path d="M6 6l12 12M18 6L6 18" />
                </svg>
            </button>
        </span>
    );
}
export function Refresh({ run, id }: { run: () => unknown; id?: string }) {
    return (
        <AsyncButton id={id} class="btn sm ghost" run={run}>
            <svg class="i" viewBox="0 0 24 24" aria-hidden="true">
                <path d="M21 12a9 9 0 11-3-6.7L21 8" />
                <path d="M21 3v5h-5" />
            </svg>
            Refresh
        </AsyncButton>
    );
}
export function Dialog({
    open,
    onClose,
    children,
    animated = false,
    ...props
}: Omit<JSX.IntrinsicElements["dialog"], "open"> & {
    open: boolean;
    onClose: () => void;
    children?: ComponentChildren;
    animated?: boolean;
    "data-noscrim"?: boolean;
}) {
    const generation = useRef(0),
        ref = useRef<HTMLDialogElement>(null),
        opener = useRef<HTMLElement | null>(null);
    useLayoutEffect(() => {
        const d = ref.current,
            id = ++generation.current;
        if (!d) return;
        if (open) {
            if (!d.open) {
                opener.current = document.activeElement as HTMLElement;
                d.showModal();
            }
            if (animated) layerIn(d);
        } else if (d.open) {
            if (animated)
                void layerOut(d).then(() => {
                    if (generation.current === id) d.close();
                });
            else d.close();
        }
    }, [open]);
    return (
        <dialog
            {...props}
            ref={ref}
            onCancel={(e) => {
                e.preventDefault();
                onClose();
            }}
            onClose={() => {
                onClose();
                if (opener.current?.isConnected) opener.current.focus();
            }}
            onClick={(e) => {
                if (e.target === e.currentTarget && !props["data-noscrim"])
                    onClose();
            }}
        >
            {children}
        </dialog>
    );
}
export function Close({ onClick }: { onClick: () => void }) {
    return (
        <button class="btn ghost" onClick={onClick}>
            Close <span class="kbd">Esc</span>
        </button>
    );
}
export function Tabs({
    id,
    label,
    options,
    value,
    onChange,
}: {
    id: string;
    label: string;
    options: readonly (readonly [string, ComponentChildren])[];
    value: string;
    onChange: (v: string) => void;
}) {
    const ref = useRef<HTMLDivElement>(null);
    return (
        <div
            class={`tabs${id === "envTabs" ? " dtabs" : id === "c_tabs" ? " ctabs" : ""}`}
            id={id}
            ref={ref}
            role="tablist"
            aria-label={label}
            onKeyDown={(e) => {
                const at = options.findIndex(([key]) => key === value);
                const next: Record<string, number> = {
                    ArrowRight: (at + 1) % options.length,
                    ArrowLeft: (at + options.length - 1) % options.length,
                    Home: 0,
                    End: options.length - 1,
                };
                if (next[e.key] === undefined) return;
                e.preventDefault();
                onChange(options[next[e.key]][0]);
                ref.current
                    ?.querySelectorAll<HTMLButtonElement>("button")
                    [next[e.key]].focus();
            }}
        >
            {options.map(([key, text]) => (
                <button
                    key={key}
                    role="tab"
                    id={`${id}-${key}`}
                    aria-controls={`${id}-pane-${key}`}
                    aria-selected={value === key}
                    tabIndex={value === key ? 0 : -1}
                    onClick={() => onChange(key)}
                >
                    {text}
                </button>
            ))}
        </div>
    );
}
export function Panel({
    tabs,
    id,
    value,
    children,
    className = "tabpanel",
}: {
    tabs: string;
    id: string;
    value: string;
    children?: ComponentChildren;
    className?: string;
}) {
    return (
        <div
            class={className}
            id={`${tabs}-pane-${id}`}
            role="tabpanel"
            aria-labelledby={`${tabs}-${id}`}
            hidden={value !== id}
        >
            {children}
        </div>
    );
}
export function StateTag({ cls }: { cls: string }) {
    return (
        <span class={`badge ${cls === "busy" ? "" : cls}`}>
            {{
                ok: "healthy",
                warn: "warning",
                fail: "failing",
                busy: loadErrors.value.doctor ? "not checked" : "checking…",
            }[cls] || "not checked"}
        </span>
    );
}
export function Skeleton({ short = false }: { short?: boolean }) {
    return <div class={`skeleton${short ? " short" : ""}`} />;
}
export function JsonText({ value }: { value: unknown }) {
    return (
        <>
            {JSON.stringify(value ?? {}, null, 2)
                .split(/("(?:[^"\\]|\\.)*"\s*:)/g)
                .map((s, i) => (i % 2 ? <span class="jk">{s}</span> : s))}
        </>
    );
}
export const num = (n: unknown) => Number(n || 0).toLocaleString();
export const short = (n: number) =>
    n >= 1e6
        ? `${+(n / 1e6).toFixed(1)}M`
        : n >= 1e4
          ? `${Math.round(n / 1e3)}k`
          : n >= 1e3
            ? `${+(n / 1e3).toFixed(1)}k`
            : String(+n.toFixed(1));
export const plural = (n: number, word: string, many = word + "s") =>
    `${n} ${n === 1 ? word : many}`;
export const show = (v: unknown) =>
    Array.isArray(v) ? v.join(", ") : v == null ? "" : String(v);
export const splitList = (v: string) =>
    v
        .split(/[,\n]/)
        .map((x) => x.trim())
        .filter(Boolean);
export const ENV_NAME = /^[A-Za-z_][A-Za-z0-9_]*$/;
export function modKey() {
    return /Mac|iPhone|iPad/.test(navigator.platform || "") ? "⌘" : "Ctrl";
}
export const timeAgo = (ts: number) => {
    const s = Math.max(0, Date.now() / 1000 - ts);
    return s < 60
        ? "just now"
        : s < 3600
          ? `${Math.floor(s / 60)}m ago`
          : s < 86400
            ? `${Math.floor(s / 3600)}h ago`
            : `${Math.floor(s / 86400)}d ago`;
};
export function whenShort(ts: number) {
    const d = new Date(ts * 1000),
        t = d.toLocaleTimeString([], { hour12: false });
    return d.toDateString() === new Date().toDateString()
        ? t
        : `${d.toLocaleDateString([], { weekday: "short" })} ${t.slice(0, 5)}`;
}
