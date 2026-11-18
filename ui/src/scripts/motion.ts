// The full build: this page is served from the local machine, so its size costs nothing and it brings real springs, stagger
// and number animation. The hybrid engine still runs opacity and transform on the browser's own animations.
import { animate } from "motion";

// ------------------------------------------------------------------ motion
// Everything that moves in script goes through here, so there is one place to tune the feel and one reduced-motion check.
// CSS still does the small things (hover, press, the row expand); Motion does what needs a spring, a stagger or a finish
// callback. Motion writes through WAAPI and the CSSOM, never a style attribute, so the style-src 'self' policy holds.
export const REDUCED = {
    get matches() {
        return (
            typeof matchMedia !== "undefined" &&
            matchMedia("(prefers-reduced-motion: reduce)").matches
        );
    },
}; // evaluated lazily: importing animation helpers does no DOM work
const SOFT = { type: "spring", duration: 0.5, bounce: 0.18 } as const; // things that arrive: a toast, a dialog
const SNAP = { type: "spring", duration: 0.38, bounce: 0.12 } as const; // things that follow a click: the pill
const OUT = { duration: 0.14, ease: "easeIn" } as const; // things that leave are quick and do not bounce
const calm = () => REDUCED.matches;
// Remove the inline values an animation leaves at its end. Motion can commit that final style a moment after "finished" resolves,
// so the removal runs again shortly after.
const settle = (el: Element, ...props: string[]) => {
    const go = () =>
        props.forEach((p) => (el as HTMLElement).style.removeProperty(p));
    go();
    setTimeout(go, 100);
};

// One animation per element, each starting `step` seconds after the one before it and each cleaned up on its own.
function each(
    els: Element[],
    keyframes: Record<string, unknown>,
    options: Record<string, unknown>,
    step: number,
    clear: string[],
) {
    els.forEach((el, i) =>
        animate(
            el,
            keyframes as any,
            { ...options, delay: i * step } as any,
        ).finished.then(
            () => settle(el, ...clear),
            () => {},
        ),
    );
}

// A screen switch: the blocks of the new screen rise in one after another. Only blocks in the viewport move, so a long screen
// does not run a stagger nobody sees, and the delay is capped so the last block is never more than ~300ms late.
export function revealPage(page: HTMLElement) {
    if (calm()) return;
    const blocks = [...page.children].filter((c) => {
        const r = c.getBoundingClientRect();
        return r.height > 0 && r.top < innerHeight;
    });
    if (!blocks.length) return;
    // a finished transform would keep a containing block for sticky and fixed children (the Playground card), so it goes
    each(
        blocks,
        { opacity: [0, 1], transform: ["translateY(10px)", "translateY(0px)"] },
        { duration: 0.42, ease: [0.16, 1, 0.3, 1] },
        Math.min(0.045, 0.3 / blocks.length),
        ["transform", "opacity"],
    );
}

// Floating layers (the confirm box, the palette): the panel springs in under a fading scrim, and leaves quickly.
export function layerIn(wrap: HTMLElement) {
    if (calm()) return;
    const scrim = wrap.querySelector<HTMLElement>(".overlay"),
        panel = wrap.querySelector<HTMLElement>(".modal, .palette");
    if (scrim) animate(scrim, { opacity: [0, 1] }, { duration: 0.2 });
    if (panel)
        animate(
            panel,
            {
                opacity: [0, 1],
                transform: [
                    "translateY(-10px) scale(0.97)",
                    "translateY(0px) scale(1)",
                ],
            },
            { ...SOFT, opacity: { duration: 0.18 } },
        ).finished.then(
            () => settle(panel, "transform", "opacity"),
            () => {},
        );
}
export async function layerOut(wrap: HTMLElement) {
    const parts = [
        ...wrap.querySelectorAll<HTMLElement>(".overlay, .modal, .palette"),
    ];
    if (calm() || !parts.length) return;
    await Promise.all(
        parts.map((p) => animate(p, { opacity: 0 }, OUT).finished),
    ).catch(() => {});
    parts.forEach((p) => settle(p, "opacity"));
}

// A toast slides in from the edge it sits on and leaves the same way; the ones below close the gap instead of jumping.
export function toastIn(el: HTMLElement) {
    if (calm()) return;
    animate(
        el,
        {
            opacity: [0, 1],
            transform: [
                "translateX(24px) scale(0.96)",
                "translateX(0px) scale(1)",
            ],
        },
        { ...SOFT, opacity: { duration: 0.2 } },
    ).finished.then(
        () => settle(el, "transform", "opacity"),
        () => {},
    );
}
export async function toastOut(el: HTMLElement) {
    if (!el.isConnected) return;
    if (!calm()) {
        const h = el.offsetHeight,
            gap = parseFloat(getComputedStyle(el.parentElement!).rowGap) || 0;
        await animate(
            el,
            { opacity: 0, transform: "translateX(24px)" },
            OUT,
        ).finished.catch(() => {});
        await animate(
            el,
            { height: [h, 0], marginBottom: [0, -gap], paddingBlock: 0 },
            { duration: 0.2, ease: [0.16, 1, 0.3, 1] },
        ).finished.catch(() => {});
    }
    // The owning component removes the toast after the exit completes.
}

// The segmented-control pill follows the pressed button with a spring (a little overshoot reads as weight).
export function movePill(
    pill: HTMLElement,
    x: number,
    y: number,
    w: number,
    h: number,
) {
    animate(
        pill,
        {
            transform: `translate(${x}px, ${y}px)`,
            width: `${w}px`,
            height: `${h}px`,
        },
        SNAP,
    );
}

// A figure runs from 0 to its value, ease-out. Returns a handle so a newer render can stop it.
export function countTo(
    to: number,
    onUpdate: (v: number) => void,
    onDone: () => void,
) {
    return animate(0, to, {
        duration: 0.6,
        ease: [0.16, 1, 0.3, 1],
        onUpdate,
        onComplete: onDone,
    });
}

// A small acknowledgement for a choice that was just made (a colour scheme card, a copied value).
export function bump(el: Element) {
    if (!calm())
        animate(
            el,
            { transform: ["scale(0.94)", "scale(1)"] },
            { type: "spring", duration: 0.45, bounce: 0.4 },
        ).finished.then(
            () => settle(el, "transform"),
            () => {},
        );
}

// Charts draw themselves in once per data set: the line is traced, the area under it fades in, bars grow from the axis.
export function drawChart(el: HTMLElement) {
    if (calm()) return;
    const line = el.querySelector<SVGPathElement>(".line"),
        area = el.querySelector<SVGPathElement>(".area"),
        bars = [...el.querySelectorAll<SVGElement>(".bar")];
    if (line)
        animate(
            line,
            { pathLength: [0, 1] },
            { duration: 0.9, ease: [0.16, 1, 0.3, 1] },
        ).finished.then(
            () =>
                ["pathLength", "stroke-dasharray", "stroke-dashoffset"].forEach(
                    (a) => line.removeAttribute(a),
                ),
            () => {},
        );
    if (area)
        animate(
            area,
            { opacity: [0, 1] },
            { duration: 0.7, delay: 0.25 },
        ).finished.then(
            () => settle(area, "opacity"),
            () => {},
        );
    each(
        bars,
        { transform: ["scaleY(0)", "scaleY(1)"] },
        { duration: 0.55, ease: [0.16, 1, 0.3, 1] },
        Math.min(0.02, 0.4 / (bars.length || 1)),
        ["transform"],
    );
}
// Horizontal bars grow from the left, top to bottom.
export function growBars(el: HTMLElement) {
    if (calm()) return;
    const fills = [...el.querySelectorAll<HTMLElement>(".track > span")];
    each(
        fills,
        { transform: ["scaleX(0)", "scaleX(1)"] },
        { duration: 0.6, ease: [0.16, 1, 0.3, 1] },
        0.05,
        ["transform"],
    );
}
// The first call rows of the Activity list rise in one after another; the rest are below the fold or not worth the wait.
export function revealRows(rows: Element[]) {
    if (calm()) return;
    const first = rows
        .filter((r) => r.getBoundingClientRect().top < innerHeight)
        .slice(0, 14);
    each(
        first,
        { opacity: [0, 1], transform: ["translateY(8px)", "translateY(0px)"] },
        { duration: 0.4, ease: [0.16, 1, 0.3, 1] },
        0.03,
        ["transform", "opacity"],
    );
}
