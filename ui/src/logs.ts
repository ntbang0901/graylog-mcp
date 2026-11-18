import { stats, breakdown, logsView, logsRevision } from "./store";
import type { LogsView } from "./scripts/types";
export type { LogsView } from "./scripts/types";
export { logsView, logsRevision } from "./store";
export const LEVEL_NAME = [
    "emerg",
    "alert",
    "crit",
    "error",
    "warn",
    "notice",
    "info",
    "debug",
];
const LEVEL_WORD: Record<string, number> = {
    emerg: 0,
    emergency: 0,
    fatal: 2,
    alert: 1,
    crit: 2,
    critical: 2,
    error: 3,
    err: 3,
    warn: 4,
    warning: 4,
    notice: 5,
    info: 6,
    debug: 7,
    trace: 7,
};
export function levelOf(v: unknown) {
    const s = String(v ?? "").trim(),
        n = /^\d$/.test(s) ? Number(s) : LEVEL_WORD[s.toLowerCase()];
    if (n === undefined || n > 7)
        return { n: null, word: s.toLowerCase() || "-", cls: "" };
    return {
        n,
        word: LEVEL_NAME[n],
        cls: n <= 3 ? "fail" : n === 4 ? "warn" : "",
    };
}
export const phrase = (v: unknown) =>
    `"${String(v).replace(/[\\"]/g, (c) => `\\${c}`)}"`;
export function logsBase(name: string) {
    return {
        errorQuery: stats.value?.environments.find((e) => e.name === name)
            ?.error_query,
        column:
            breakdown.value?.instance === name
                ? breakdown.value.service_field
                : undefined,
    };
}
export function openLogs(view: LogsView) {
    logsView.value = { ...logsBase(view.instance), ...view };
    logsRevision.value++;
}
