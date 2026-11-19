import {
    ASSESSMENT_WORD,
    LINK_WORD,
    PROVENANCE_WORD,
    REQUEST_WORD,
    bodyText,
    citesOf,
    cohortsOf,
    itemsOf,
    sharedEvidence,
} from "../investigations-model";
import { chooseEvidence } from "../investigations";
import type { InvItem, InvSnapshot } from "../scripts/types";

const ASSESSMENT_CLASS: Record<string, string> = {
    supported: "ok",
    contradicted_in_scope: "fail",
    undetermined: "warn",
};
function Cites({ item, snap }: { item: InvItem; snap: InvSnapshot }) {
    const ids = [...new Set(citesOf(item))],
        shared = sharedEvidence(snap);
    if (!ids.length) return null;
    return (
        <span class="inv-cites">
            {ids.map((id) => {
                const kind = snap.evidence.find((e) => e.id === id)?.kind ?? id;
                return (
                    <button
                        class="btn sm ghost"
                        onClick={() => void chooseEvidence(id)}
                        title={
                            shared.has(id)
                                ? `Also cited by ${shared
                                      .get(id)!
                                      .filter((x) => x !== item.id)
                                      .join(", ")}`
                                : undefined
                        }
                    >
                        {kind}
                    </button>
                );
            })}
        </span>
    );
}
export function Verdict({ a }: { a: InvItem }) {
    const word = String(a.data.assessment);
    return (
        <span class={`badge ${ASSESSMENT_CLASS[word] ?? ""}`}>
            {ASSESSMENT_WORD[word] ?? word}
        </span>
    );
}
// what the agent currently believes: each explanation with where it stands and the evidence it rests on
function Explained({ snap }: { snap: InvSnapshot }) {
    const list = itemsOf(snap, "assessment"),
        hypotheses = itemsOf(snap, "hypothesis");
    if (!list.length && !hypotheses.length)
        return (
            <p class="inv-muted">The agent has not concluded anything yet.</p>
        );
    return (
        <ul class="inv-explained">
            {list.map((a) => (
                <li
                    class={`inv-explain ${ASSESSMENT_CLASS[String(a.data.assessment)] ?? ""}`}
                >
                    <Verdict a={a} />
                    <span>{bodyText(a, "claim")}</span>
                    <Cites item={a} snap={snap} />
                </li>
            ))}
            {hypotheses.map((h) => (
                <li class="inv-explain">
                    <span class="badge">Open</span>
                    <span>{bodyText(h, "statement")}</span>
                </li>
            ))}
        </ul>
    );
}
// the claims that rest on one piece of evidence, under Supports, Contradicts or Looked at
export function CitedList({
    title,
    cls,
    items,
}: {
    title: string;
    cls: string;
    items: InvItem[];
}) {
    return (
        <div class={`inv-cited ${cls}`}>
            <h4>{title}</h4>
            <ul class="inv-bullets">
                {items.map((a) => (
                    <li>{bodyText(a, "claim")}</li>
                ))}
            </ul>
        </div>
    );
}
// how identities are linked, what was asked of the user and the counts that were compared; with `extras` the
// assessments and check results (shown on their own) are left out
export function Findings({
    snap,
    extras,
    explanations,
}: {
    snap: InvSnapshot;
    extras?: boolean;
    explanations?: boolean;
}) {
    if (explanations) return <Explained snap={snap} />;
    const assessments = extras ? [] : itemsOf(snap, "assessment"),
        results = extras ? [] : itemsOf(snap, "check_result"),
        identities = itemsOf(snap, "identity"),
        links = itemsOf(snap, "relationship"),
        requests = itemsOf(snap, "evidence_request"),
        comparisons = itemsOf(snap, "comparison"),
        name = (id: string) => {
            const i = identities.find((x) => x.id === id);
            return i ? `${i.data.type}: ${i.data.value}` : id;
        };
    if (
        ![assessments, results, identities, links, requests, comparisons].some(
            (l) => l.length,
        )
    )
        return null;
    return (
        <section class="inv-findings" aria-label="Findings">
            {!!assessments.length && (
                <>
                    <h4>Assessments</h4>
                    <ul>
                        {assessments.map((a) => (
                            <li>
                                <span
                                    class={`badge ${ASSESSMENT_CLASS[String(a.data.assessment)] ?? ""}`}
                                >
                                    {ASSESSMENT_WORD[
                                        String(a.data.assessment)
                                    ] ?? String(a.data.assessment)}
                                </span>{" "}
                                {bodyText(a, "claim")}
                                <Cites item={a} snap={snap} />
                            </li>
                        ))}
                    </ul>
                </>
            )}
            {!!results.length && (
                <>
                    <h4>Check results</h4>
                    <ul>
                        {results.map((r) => (
                            <li>
                                <span class="mono">{String(r.data.check)}</span>{" "}
                                {bodyText(r, "result")}
                                <Cites item={r} snap={snap} />
                            </li>
                        ))}
                    </ul>
                </>
            )}
            {!!identities.length && (
                <>
                    <h4>Identities</h4>
                    <ul class="inv-chips">
                        {identities.map((i) => (
                            <li
                                class="inv-chip"
                                title={`Seen in ${citesOf(i).length} evidence`}
                            >
                                <span class="inv-chip-type">
                                    {String(i.data.type)}
                                    <span class="inv-muted">
                                        {" "}
                                        · {String(i.data.namespace)}
                                    </span>
                                </span>
                                <span class="mono">{String(i.data.value)}</span>
                            </li>
                        ))}
                    </ul>
                </>
            )}
            {!!links.length && (
                <>
                    <h4>Links</h4>
                    <ul>
                        {links.map((l) => (
                            <li
                                class={`inv-link-row ${l.data.status === "observed" ? "observed" : "candidate"}`}
                            >
                                {name(String(l.data.from))} →{" "}
                                {name(String(l.data.to))}{" "}
                                <i>({String(l.data.relation)})</i>
                                <span class="badge">
                                    {LINK_WORD[String(l.data.status)] ??
                                        String(l.data.status)}
                                </span>
                                {typeof l.data.basis === "string" && (
                                    <span class="inv-muted">
                                        {" "}
                                        {l.data.basis}
                                    </span>
                                )}
                                <Cites item={l} snap={snap} />
                            </li>
                        ))}
                    </ul>
                </>
            )}
            {!!requests.length && (
                <>
                    <h4>Asked of you</h4>
                    <ul>
                        {requests.map((r) => (
                            <li>
                                <span
                                    class={`badge ${r.data.status === "requested" ? "warn" : ""}`}
                                >
                                    {REQUEST_WORD[String(r.data.status)] ??
                                        String(r.data.status)}
                                </span>{" "}
                                {bodyText(r, "question")}
                                {typeof r.data.provenance === "string" && (
                                    <span class="inv-muted">
                                        {" "}
                                        —{" "}
                                        {PROVENANCE_WORD[r.data.provenance] ??
                                            r.data.provenance}
                                    </span>
                                )}
                                {typeof r.data.answer === "string" && (
                                    <div class="inv-answer">
                                        {r.data.answer}
                                    </div>
                                )}
                            </li>
                        ))}
                    </ul>
                </>
            )}
            {comparisons.map((c) => (
                <>
                    <h4>Comparison · {String(c.data.unit)}</h4>
                    <table class="inv-table">
                        <thead>
                            <tr>
                                <th>Cohort</th>
                                <th>Count</th>
                                <th>Eligible</th>
                                <th>Rate</th>
                            </tr>
                        </thead>
                        <tbody>
                            {cohortsOf(c).map((k) => (
                                <tr>
                                    <td>{k.name}</td>
                                    <td>{k.count ?? "—"}</td>
                                    <td>{k.eligible ?? "unknown"}</td>
                                    <td>{k.rate}</td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </>
            ))}
        </section>
    );
}
