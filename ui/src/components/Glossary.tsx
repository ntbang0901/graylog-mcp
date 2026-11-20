import { applies, openPreset, vocabulary, vocabularyError } from "../ask";
import type { ExplorerView } from "../explorer";
import { drill, knownInstance } from "../explorer-store";
import { toast } from "../store";

// The team's words under the query bar: a term narrows the view to its query, a preset opens what it runs.
export function Glossary({ v }: { v: ExplorerView }) {
    const voc = vocabulary.value,
        env = knownInstance(v.instance),
        terms = (voc?.terms || []).filter((t) => applies(t, env)),
        presets = voc?.presets || [];
    if (!voc && vocabularyError.value)
        return (
            <div class="hint mb-xs">
                The glossary could not be read: {vocabularyError.value}
            </div>
        );
    if (!terms.length && !presets.length) return null;
    return (
        <div class="chips ask-glossary" aria-label="The glossary's terms">
            {terms.map((t) => {
                const about = [
                    t.aliases.length ? `also: ${t.aliases.join(", ")}` : "",
                    t.description,
                    t.query ? `query: ${t.query}` : "no query of its own",
                ]
                    .filter(Boolean)
                    .join("\n");
                return (
                    <button
                        key={t.name}
                        type="button"
                        class="chip"
                        title={about}
                        onClick={() => {
                            if (t.query) drill(`(${t.query})`);
                            else
                                toast(
                                    `${t.name} has no query of its own: ask finds it by its keywords`,
                                    "info",
                                );
                        }}
                    >
                        {t.name}
                    </button>
                );
            })}
            {presets.length > 0 && (
                <select
                    class="sel auto-width"
                    aria-label="Open a preset"
                    value=""
                    onChange={(e) => {
                        const p = presets[Number(e.currentTarget.value)];
                        e.currentTarget.value = "";
                        if (p) void openPreset(p, v.instance);
                    }}
                >
                    <option value="">Presets ({presets.length})</option>
                    {presets.map((p, i) => (
                        <option value={String(i)} title={p.description}>
                            {p.name}
                            {typeof p.args?.query === "string"
                                ? ""
                                : ` · ${p.tool}`}
                        </option>
                    ))}
                </select>
            )}
        </div>
    );
}
