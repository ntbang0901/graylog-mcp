import { density, mode, palette, settingsOpen, theme, toast } from "./store";
import { storage } from "./api";
import { REDUCED } from "./scripts/motion";
export const SCHEMES = [
    {
        id: "slate",
        name: "Slate",
        desc: "Tinted slate, teal for actions and data.",
        def: true,
    },
    {
        id: "midnight",
        name: "Midnight",
        desc: "Blue-black console, one blue accent.",
    },
    { id: "graphite", name: "Graphite", desc: "Neutral zinc, violet accent." },
    {
        id: "catppuccin",
        name: "Catppuccin",
        desc: "Pastel Latte and Mocha, mauve accent.",
    },
];
export function originOf(el: Element | null) {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
}
function transition(fn: () => void, origin?: { x: number; y: number } | null) {
    const doc = document.documentElement;
    if (
        !document.startViewTransition ||
        REDUCED.matches ||
        document.querySelector("dialog[open]:not(#settings)")
    ) {
        fn();
        return;
    }
    if (origin) doc.classList.add("vt-reveal");
    const vt = document.startViewTransition(fn);
    if (origin) {
        vt.ready
            .then(() => {
                const { x, y } = origin,
                    r = Math.hypot(
                        Math.max(x, innerWidth - x),
                        Math.max(y, innerHeight - y),
                    );
                doc.animate(
                    {
                        clipPath: [
                            `circle(0px at ${x}px ${y}px)`,
                            `circle(${r}px at ${x}px ${y}px)`,
                        ],
                    },
                    {
                        duration: 520,
                        easing: "cubic-bezier(.16,1,.3,1)",
                        pseudoElement: "::view-transition-new(root)",
                    },
                );
            })
            .catch(() => {});
        void vt.finished.finally(() => doc.classList.remove("vt-reveal"));
    }
}
export function setMode(
    value: string,
    origin?: { x: number; y: number } | null,
) {
    storage.set("gm-theme", value === "system" ? null : value);
    const next =
        value === "system"
            ? matchMedia("(prefers-color-scheme: dark)").matches
                ? "dark"
                : "light"
            : value;
    const apply = () => {
        mode.value = value;
        theme.value = next;
        const d = document.documentElement.dataset;
        d.mode = value;
        d.theme = next;
    };
    if (next !== theme.value) transition(apply, origin);
    else apply();
}
export function toggleTheme(origin?: { x: number; y: number } | null) {
    setMode(theme.value === "dark" ? "light" : "dark", origin);
}
export function setPalette(value: string) {
    if (value === palette.value) return;
    storage.set("gm-palette", value);
    transition(() => {
        palette.value = value;
        document.documentElement.dataset.palette = value;
    });
    toast(`Scheme: ${SCHEMES.find((s) => s.id === value)?.name}`);
}
export function setDensity(value: string) {
    storage.set("gm-density", value);
    density.value = value;
    document.documentElement.dataset.density = value;
}
export function openSettings() {
    settingsOpen.value = true;
}
