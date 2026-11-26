import { useEffect, useState } from "preact/hooks";
import type { Fields, ToolField } from "../scripts/types";
import { splitList } from "./UI";

type Parsed = { ok: true; value: unknown } | { ok: false };
interface Kind {
    format: (v: unknown) => string;
    parse: (s: string) => Parsed;
}
const blank = (s: string) => s.trim() === "";
// an empty input leaves the argument out, so the tool's own default applies
const KINDS: Record<string, Kind> = {
    string: {
        format: (v) => (v == null ? "" : String(v)),
        parse: (s) => ({ ok: true, value: s === "" ? undefined : s }),
    },
    integer: {
        format: (v) => (v == null ? "" : String(v)),
        parse: (s) =>
            blank(s)
                ? { ok: true, value: undefined }
                : /^-?\d+$/.test(s.trim())
                  ? { ok: true, value: Number(s) }
                  : { ok: false },
    },
    number: {
        format: (v) => (v == null ? "" : String(v)),
        parse: (s) =>
            blank(s)
                ? { ok: true, value: undefined }
                : Number.isFinite(Number(s))
                  ? { ok: true, value: Number(s) }
                  : { ok: false },
    },
    list: {
        format: (v) =>
            Array.isArray(v) ? v.join(", ") : v == null ? "" : String(v),
        parse: (s) => {
            const items = splitList(s);
            return { ok: true, value: items.length ? items : undefined };
        },
    },
    json: {
        format: (v) => (v === undefined ? "" : JSON.stringify(v, null, 2)),
        parse: (s) => {
            if (blank(s)) return { ok: true, value: undefined };
            try {
                return { ok: true, value: JSON.parse(s) };
            } catch {
                return { ok: false };
            }
        },
    },
};
const same = (a: unknown, b: unknown) =>
    JSON.stringify(a) === JSON.stringify(b);

// The text as typed stays while it does not parse ("1." on its way to "1.5"); a change of the arguments from
// elsewhere (the JSON, a re-run) replaces it.
function Typed({
    f,
    id,
    value,
    onChange,
}: {
    f: ToolField;
    id: string;
    value: unknown;
    onChange: (v: unknown) => void;
}) {
    const kind = KINDS[f.control] || KINDS.json,
        [text, setText] = useState(() => kind.format(value)),
        [bad, setBad] = useState(false);
    useEffect(() => {
        const p = kind.parse(text);
        if (!p.ok || !same(p.value, value)) {
            setText(kind.format(value));
            setBad(false);
        }
    }, [JSON.stringify(value)]);
    const input = (s: string) => {
        setText(s);
        const p = kind.parse(s);
        setBad(!p.ok);
        if (p.ok) onChange(p.value);
    };
    const hint =
        f.default !== undefined && f.default !== null
            ? typeof f.default === "object"
                ? JSON.stringify(f.default)
                : String(f.default)
            : f.control === "list"
              ? "comma-separated"
              : "";
    if (f.control === "json")
        return (
            <textarea
                id={id}
                rows={3}
                spellcheck={false}
                class={bad ? "invalid" : undefined}
                aria-invalid={bad}
                placeholder={hint || "JSON"}
                value={text}
                onInput={(e) => input(e.currentTarget.value)}
            />
        );
    const common = {
        id,
        autoComplete: "off",
        spellcheck: false,
        class: bad ? "invalid" : undefined,
        "aria-invalid": bad,
        placeholder: hint,
        value: text,
        onInput: (e: { currentTarget: HTMLInputElement }) =>
            input(e.currentTarget.value),
    };
    return f.control === "integer" || f.control === "number" ? (
        <input
            type="number"
            step={f.control === "integer" ? 1 : "any"}
            {...common}
        />
    ) : (
        <input type="text" {...common} />
    );
}

// The Playground's arguments as a form, one field per argument of the tool. The JSON text stays the arguments:
// every change here is written back into it.
export function ToolForm({
    fields,
    args,
    onChange,
}: {
    fields: ToolField[];
    args: Fields;
    onChange: (next: Fields) => void;
}) {
    // the environment has its own select above
    const shown = fields.filter((f) => f.name !== "instance");
    if (!shown.length)
        return <div class="hint">This tool takes no arguments.</div>;
    const set = (name: string, value: unknown) => {
        const next = { ...args };
        if (value === undefined) delete next[name];
        else next[name] = value;
        onChange(next);
    };
    return (
        <div class="pg-form" id="p_form">
            {shown.map((f) => {
                const id = `p_f_${f.name}`,
                    value = args[f.name];
                return (
                    <div class="field" key={f.name}>
                        {f.control === "boolean" ? (
                            <label class="check" for={id}>
                                <input
                                    id={id}
                                    type="checkbox"
                                    checked={Boolean(value ?? f.default)}
                                    onChange={(e) =>
                                        set(f.name, e.currentTarget.checked)
                                    }
                                />
                                <span class="mono">{f.name}</span>
                            </label>
                        ) : (
                            <label
                                class={`label mono${f.required ? " req" : ""}`}
                                for={id}
                            >
                                {f.name}
                            </label>
                        )}
                        {f.control === "choice" ? (
                            <select
                                id={id}
                                value={value == null ? "" : String(value)}
                                onChange={(e) => {
                                    const s = e.currentTarget.value;
                                    set(
                                        f.name,
                                        s === ""
                                            ? undefined
                                            : (f.choices || []).find(
                                                  (c) => String(c) === s,
                                              ),
                                    );
                                }}
                            >
                                {(!f.required || value == null) && (
                                    <option value="">
                                        {f.default != null
                                            ? `default (${String(f.default)})`
                                            : ""}
                                    </option>
                                )}
                                {(f.choices || []).map((c) => (
                                    <option value={String(c)}>
                                        {String(c)}
                                    </option>
                                ))}
                            </select>
                        ) : f.control !== "boolean" ? (
                            <Typed
                                f={f}
                                id={id}
                                value={value}
                                onChange={(v) => set(f.name, v)}
                            />
                        ) : null}
                        {f.description && (
                            <div class="hint">{f.description}</div>
                        )}
                    </div>
                );
            })}
        </div>
    );
}
