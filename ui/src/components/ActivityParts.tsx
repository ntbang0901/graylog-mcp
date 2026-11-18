// What the Activity tabs show the same way: a call's status and what it asked for.
import { rerun, toast } from "../store";
import { STATUS_CLASS, STATUS_WHY } from "../activity";
import type { Call } from "../scripts/types";
import { callWhat } from "./Overview";
import { num } from "./UI";

export const fmtMs = (ms: number) =>
    ms >= 10000 ? `${Math.round(ms / 1000)}s` : `${num(ms)}ms`;
export function askText(c: Call) {
    const w = callWhat(c),
        a = c.args || {},
        p = c.params || {};
    return (
        (w !== "-" ? w : p.range ? `last ${p.range}` : "-") +
        (a.group_by && w !== a.group_by ? ` · by ${a.group_by}` : "")
    );
}
// a modified click opens a link in a new tab or window, as the browser does
export const plainClick = (e: MouseEvent) =>
    !(e.ctrlKey || e.metaKey || e.shiftKey || e.altKey || e.button);
export function playCall(c: Call) {
    toast(
        `Playground pre-filled: ${c.tool}${c.instance ? ` on ${c.instance}` : ""}`,
        "info",
    );
    return rerun(c);
}
export function StatusBadge({ status }: { status: string }) {
    return (
        <span
            class={`badge ${STATUS_CLASS[status] || "fail"}`}
            title={STATUS_WHY[status] || ""}
        >
            {status}
        </span>
    );
}
