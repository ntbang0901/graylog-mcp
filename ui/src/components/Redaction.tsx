import { useEffect, useRef, useState } from "preact/hooks";
import { api, errorText } from "../api";
import {
    currentPage,
    dirty,
    loadState,
    navigate,
    state,
    toast,
} from "../store";
import { Icon, Page, plural, splitList } from "./UI";
import { Marks, maskRegex } from "./Marks";
interface Pattern {
    name: string;
    pattern: string;
    replacement: string;
}
interface Draft {
    profile: string;
    packs: string[];
    vn_cmnd: boolean;
    allow: string;
    sensitive: string;
    exclude: string;
    patterns: Pattern[];
}
const empty: Draft = {
    profile: "basic",
    packs: [],
    vn_cmnd: false,
    allow: "",
    sensitive: "",
    exclude: "",
    patterns: [],
};
function settings(d: Draft) {
    return {
        packs: d.packs,
        ...(d.vn_cmnd ? { vn_cmnd: true } : {}),
        allow: d.allow
            .split("\n")
            .map((x) => x.trim())
            .filter(Boolean),
        sensitive_fields: splitList(d.sensitive),
        exclude_fields: splitList(d.exclude),
        patterns: d.patterns
            .filter((p) => p.pattern)
            .map((p) => ({
                ...p,
                ...(!p.name ? { name: undefined } : {}),
                ...(!p.replacement ? { replacement: undefined } : {}),
            })),
        ...(d.profile !== "basic" ? { profile: d.profile } : {}),
    };
}
const sample = `user=alice@example.com password=hunter2 card 4111 1111 1111 1111
call 0912345678 or +84 912 345 678, cccd 001099012345, order 1234567890123
Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijk
jdbc:postgresql://admin:s3cret@db:5432/orders`;
export function Redaction() {
    const [draft, setDraft] = useState<Draft>(empty),
        [input, setInput] = useState(sample),
        [masked, setMasked] = useState(""),
        [hits, setHits] = useState<string[]>([]),
        [error, setError] = useState(""),
        [open, setOpen] = useState<Record<string, boolean>>({
            profile: true,
            packs: true,
            patterns: false,
            allow: false,
            fields: false,
        }),
        active = currentPage.value === "redaction" && !!state.value;
    const previewed = useRef(false);
    const reset = () => {
        const r = state.value?.data.redaction || {};
        setDraft({
            profile: r.profile || "basic",
            packs: r.packs || [],
            vn_cmnd: !!r.vn_cmnd,
            allow: (r.allow || []).join("\n"),
            sensitive: (r.sensitive_fields || []).join(", "),
            exclude: (r.exclude_fields || []).join(", "),
            patterns: (r.patterns || []).map((p: Partial<Pattern>) => ({
                name: p.name || "",
                pattern: p.pattern || "",
                replacement: p.replacement || "",
            })),
        });
        setOpen({
            profile: true,
            packs: true,
            patterns: !!r.patterns?.length,
            allow: !!r.allow?.length,
            fields: !!(r.sensitive_fields?.length || r.exclude_fields?.length),
        });
    };
    useEffect(() => {
        if (active && !dirty.value) reset();
    }, [state.value, active]);
    useEffect(() => {
        if (!active) {
            previewed.current = false;
            return;
        }
        let live = true;
        const timer = setTimeout(
            () => {
                void api<{ masked: string; hits: string[] }>(
                    "/api/redact",
                    "POST",
                    { text: input, redaction: settings(draft) },
                )
                    .then((r) => {
                        if (live) {
                            previewed.current = true;
                            setMasked(r.masked);
                            setHits(r.hits);
                            setError("");
                        }
                    })
                    .catch((e) => {
                        if (live) setError(errorText(e));
                    });
            },
            previewed.current ? 250 : 0,
        );
        return () => {
            live = false;
            clearTimeout(timer);
        };
    }, [input, draft, active]);
    const change = (patch: Partial<Draft>) => {
        const next = { ...draft, ...patch };
        setDraft(next);
        dirty.value = {
            what: "redaction rules",
            save: async () => {
                await api("/api/settings", "POST", {
                    redaction: settings(next),
                });
                dirty.value = null;
                toast("Redaction rules saved");
                await loadState();
            },
            discard: reset,
        };
    };
    const counts = settings(draft),
        extra = draft.patterns.map((p) => p.replacement).filter(Boolean),
        n = (masked.match(maskRegex(extra)) || []).length;
    const accordion = (
        id: string,
        title: string,
        meta: string,
        children: preact.ComponentChildren,
    ) => (
        <details
            class="acc"
            id={`acc-${id}`}
            open={open[id]}
            onToggle={(e) => {
                const value = e.currentTarget.open;
                setOpen((p) => (p[id] === value ? p : { ...p, [id]: value }));
            }}
        >
            <summary>
                {title}
                <span class="acc-meta" id={`accm-${id}`}>
                    {meta}
                </span>
            </summary>
            <div class="acc-body">{children}</div>
        </details>
    );
    return (
        <Page
            id="redaction"
            tools={
                <button class="btn ghost sm" onClick={() => navigate("config")}>
                    Raw file
                </button>
            }
        >
            <div class="grid2 start">
                <div class="card">
                    <div class="inline-msg">
                        Always on: sensitive field names, emails, Bearer/Basic
                        credentials, JWTs, key=value secrets, credentials in
                        URLs, private keys and card numbers (Luhn).
                    </div>
                    {accordion(
                        "profile",
                        "Profile",
                        draft.profile,
                        <div class="field">
                            <select
                                id="r_profile"
                                aria-label="Redaction profile"
                                value={draft.profile}
                                onChange={(e) =>
                                    change({ profile: e.currentTarget.value })
                                }
                            >
                                {Object.keys(state.value?.profiles || {}).map(
                                    (p) => (
                                        <option value={p}>{p}</option>
                                    ),
                                )}
                            </select>
                            <span class="hint" id="r_profile_hint">
                                {state.value?.profiles[draft.profile] || ""}
                            </span>
                        </div>,
                    )}
                    {accordion(
                        "packs",
                        "Country packs",
                        draft.packs.length ? draft.packs.join(", ") : "none",
                        <>
                            <div class="muted small">
                                Added on top of the profile.
                            </div>
                            <div class="chips" id="packs">
                                {state.value?.packs.map((p) => (
                                    <label class="chip">
                                        <input
                                            type="checkbox"
                                            checked={draft.packs.includes(p)}
                                            onChange={(e) =>
                                                change({
                                                    packs: e.currentTarget
                                                        .checked
                                                        ? [...draft.packs, p]
                                                        : draft.packs.filter(
                                                              (x) => x !== p,
                                                          ),
                                                })
                                            }
                                        />
                                        {p}
                                    </label>
                                ))}
                            </div>
                            <label class="check small">
                                <input
                                    type="checkbox"
                                    id="r_vn_cmnd"
                                    checked={draft.vn_cmnd}
                                    onChange={(e) =>
                                        change({
                                            vn_cmnd: e.currentTarget.checked,
                                        })
                                    }
                                />
                                vn: also mask bare 9-digit numbers (old CMND,
                                noisy)
                            </label>
                        </>,
                    )}
                    {accordion(
                        "patterns",
                        "Custom patterns",
                        counts.patterns.length
                            ? plural(counts.patterns.length, "pattern")
                            : "none",
                        <>
                            <div id="patterns">
                                {!draft.patterns.length ? (
                                    <div class="inline-msg" id="noPatterns">
                                        None. Add one for your own identifiers,
                                        e.g. order numbers.
                                    </div>
                                ) : (
                                    draft.patterns.map((p, i) => (
                                        <div class="row pattern-row">
                                            <div class="field pn-field">
                                                <input
                                                    class="pn"
                                                    value={p.name}
                                                    placeholder="name"
                                                    aria-label="Pattern name"
                                                    onInput={(e) =>
                                                        change({
                                                            patterns:
                                                                draft.patterns.map(
                                                                    (x, j) =>
                                                                        j === i
                                                                            ? {
                                                                                  ...x,
                                                                                  name: e
                                                                                      .currentTarget
                                                                                      .value,
                                                                              }
                                                                            : x,
                                                                ),
                                                        })
                                                    }
                                                />
                                            </div>
                                            <div class="field pp-field">
                                                <input
                                                    class="pp mono"
                                                    value={p.pattern}
                                                    placeholder="ORD-[0-9]{8}"
                                                    aria-label="Regex"
                                                    onInput={(e) =>
                                                        change({
                                                            patterns:
                                                                draft.patterns.map(
                                                                    (x, j) =>
                                                                        j === i
                                                                            ? {
                                                                                  ...x,
                                                                                  pattern:
                                                                                      e
                                                                                          .currentTarget
                                                                                          .value,
                                                                              }
                                                                            : x,
                                                                ),
                                                        })
                                                    }
                                                />
                                            </div>
                                            <div class="field pr-field">
                                                <input
                                                    class="pr"
                                                    value={p.replacement}
                                                    placeholder="[ORDER]"
                                                    aria-label="Replacement"
                                                    onInput={(e) =>
                                                        change({
                                                            patterns:
                                                                draft.patterns.map(
                                                                    (x, j) =>
                                                                        j === i
                                                                            ? {
                                                                                  ...x,
                                                                                  replacement:
                                                                                      e
                                                                                          .currentTarget
                                                                                          .value,
                                                                              }
                                                                            : x,
                                                                ),
                                                        })
                                                    }
                                                />
                                            </div>
                                            <button
                                                class="btn sm ghost icon danger"
                                                aria-label="Remove pattern"
                                                title="Remove"
                                                onClick={() =>
                                                    change({
                                                        patterns:
                                                            draft.patterns.filter(
                                                                (_, j) =>
                                                                    i !== j,
                                                            ),
                                                    })
                                                }
                                            >
                                                <Icon name="trash" />
                                            </button>
                                        </div>
                                    ))
                                )}
                            </div>
                            <div>
                                <button
                                    class="btn sm ghost"
                                    id="addPattern"
                                    onClick={() => {
                                        change({
                                            patterns: [
                                                ...draft.patterns,
                                                {
                                                    name: "",
                                                    pattern: "",
                                                    replacement: "",
                                                },
                                            ],
                                        });
                                        setTimeout(
                                            () =>
                                                document
                                                    .querySelector<HTMLInputElement>(
                                                        "#patterns .pattern-row:last-child .pn",
                                                    )
                                                    ?.focus(),
                                            0,
                                        );
                                    }}
                                >
                                    <Icon name="plus" />
                                    Add pattern
                                </button>
                            </div>
                        </>,
                    )}
                    {accordion(
                        "allow",
                        "Never mask",
                        counts.allow.length
                            ? plural(counts.allow.length, "rule")
                            : "none",
                        <div class="field">
                            <textarea
                                id="r_allow"
                                rows={3}
                                placeholder={"^noreply@example\\.com$"}
                                aria-label="Values never masked"
                                value={draft.allow}
                                onInput={(e) =>
                                    change({ allow: e.currentTarget.value })
                                }
                            />
                            <span class="hint">
                                One regex per line; a value fully matching one
                                is kept.
                            </span>
                        </div>,
                    )}
                    {accordion(
                        "fields",
                        "Field names",
                        counts.sensitive_fields.length +
                            counts.exclude_fields.length
                            ? `${counts.sensitive_fields.length} added · ${counts.exclude_fields.length} kept`
                            : "none",
                        <div class="grid2">
                            <div class="field">
                                <label class="label" for="r_sensitive">
                                    Extra sensitive field names
                                </label>
                                <input
                                    id="r_sensitive"
                                    placeholder="national_id, otp_code"
                                    value={draft.sensitive}
                                    onInput={(e) =>
                                        change({
                                            sensitive: e.currentTarget.value,
                                        })
                                    }
                                />
                            </div>
                            <div class="field">
                                <label class="label" for="r_exclude">
                                    Field names never masked
                                </label>
                                <input
                                    id="r_exclude"
                                    placeholder="token_count"
                                    value={draft.exclude}
                                    onInput={(e) =>
                                        change({
                                            exclude: e.currentTarget.value,
                                        })
                                    }
                                />
                            </div>
                        </div>,
                    )}
                </div>
                <div class="well sticky-side">
                    <div class="card-head">
                        <h2>Try it</h2>
                        <span class="spacer" />
                        <span
                            class="mono small muted"
                            id="r_hits"
                            aria-live="polite"
                            title={
                                hits.length
                                    ? `Rules that changed the text: ${hits.join(", ")}`
                                    : ""
                            }
                        >
                            {error ||
                                (n
                                    ? `${n} masked · ${plural(hits.length, "rule")}`
                                    : "nothing masked")}
                        </span>
                    </div>
                    <label class="glabel" for="r_input">
                        Before: what the log says
                    </label>
                    <textarea
                        id="r_input"
                        rows={6}
                        class="mt-xs"
                        value={input}
                        onInput={(e) => setInput(e.currentTarget.value)}
                    />
                    <div class="glabel mt-sm mb-xs">
                        After: what the model sees
                    </div>
                    <pre id="r_output" aria-label="Masked text">
                        <Marks text={masked} extra={extra} />
                    </pre>
                </div>
            </div>
        </Page>
    );
}
