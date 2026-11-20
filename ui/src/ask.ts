// The ask box's shared part: the config's vocabulary (glossary terms, codes, presets), where a preset opens, and
// how a reading is said back. The answers themselves stay with the box on the Logs screen.
import { signal } from "@preact/signals";
import { api, errorText } from "./api";
import { navigate, playground, state } from "./store";
import { openInLogs } from "./explorer-store";
import type { AskResult, AskView, Instance, Vocabulary } from "./scripts/types";

export const vocabulary = signal<Vocabulary | null>(null);
export const vocabularyError = signal("");
// the state it was read for: a saved config is a new state, and may have new terms
let readFor: unknown = null;
export async function loadVocabulary() {
    const s = state.value;
    if (!s || readFor === s) return;
    readFor = s;
    try {
        const v = await api<Vocabulary>("/api/ask/vocabulary");
        if (readFor !== s) return;
        vocabulary.value = v;
        vocabularyError.value = "";
    } catch (e) {
        if (readFor !== s) return;
        readFor = null;
        vocabularyError.value = errorText(e);
    }
}
export type Term = Vocabulary["terms"][number];
export type Preset = Vocabulary["presets"][number];

// as config.py _applies: a term may be limited to instances, groups or environments
export function applies(t: Term, e: Instance | null | undefined) {
    if (!t.instances.length || !e) return true;
    const names = new Set(
        [e.name, e.group || "", e.environment || ""]
            .filter(Boolean)
            .map((n) => n.toLowerCase()),
    );
    return t.instances.some((i) => names.has(i.toLowerCase()));
}

// a preset with a query is a view of the Logs screen; any other runs its tool in the Playground
export async function openPreset(p: Preset, instance: string | null) {
    const a = p.args || {},
        where = (typeof a.instance === "string" && a.instance) || instance;
    if (typeof a.query === "string" && where) {
        await openInLogs({
            instance: where,
            query: a.query,
            range: typeof a.range === "string" ? a.range : undefined,
            from: typeof a.from_time === "string" ? a.from_time : undefined,
            to: typeof a.to_time === "string" ? a.to_time : undefined,
        });
        return;
    }
    if (!(await navigate("playground"))) return;
    playground.value = {
        tool: p.tool,
        instance: where || undefined,
        args: { ...(state.value?.tool_templates[p.tool] || {}), ...a },
    };
}

const at = (iso: string) => {
    const d = new Date(iso.replace(" ", "T"));
    return isNaN(+d)
        ? iso
        : d.toLocaleString([], {
              month: "short",
              day: "numeric",
              hour: "2-digit",
              minute: "2-digit",
          });
};
export function whenText(v: Pick<AskView, "range" | "from" | "to">) {
    if (v.from && v.to) return `${at(v.from)} to ${at(v.to)}`;
    return `last ${v.range || "24h"}`;
}
export const ERROR_WORDS: Record<string, string> = {
    any: "any line",
    technical: "technical errors",
    business: "business errors",
    both: "technical and business errors",
};
// compile_filters' notes are keys of ask's own sentences (ask/render.py)
const NOTES: Record<string, string> = {
    no_business_errors:
        "This environment defines no business errors (business_error_query, or an HTTP status field), so only technical errors are counted.",
};
export const noteText = (key: string) => NOTES[key] || key;
export function costLine(cost: AskResult["cost"] | undefined) {
    if (!cost) return "";
    const g = cost.graylog_requests;
    return `${g} Graylog request${g === 1 ? "" : "s"}, ${cost.provider_requests} to model providers`;
}
