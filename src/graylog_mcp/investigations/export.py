"""An investigation as Markdown for a person to read, keep or paste into a ticket.

Built from the stored (already redacted) records: the question, the scope and its revisions, each branch with its
checks, what was concluded and from which evidence, how identities are linked (candidate or observed), what was
asked of the user, the counts that were compared, and how it ended. Evidence is cited by id and kind; log excerpts
are not repeated here.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from graylog_mcp.investigations import briefing

ASSESSMENT = {
    "supported": "supported",
    "contradicted_in_scope": "contradicted in scope",
    "undetermined": "undetermined",
}


def _line(text: Any) -> str:
    return " ".join(str(text).split())


def _cell(text: Any) -> str:
    return _line(text).replace("|", "\\|")


def markdown(con: sqlite3.Connection, row: sqlite3.Row, scope: dict[str, Any]) -> str:
    snap = briefing.snapshot(con, row, scope)
    items = {k: [i for i in snap["items"] if i["kind"] == k] for k in {i["kind"] for i in snap["items"]}}
    evidence = {e["id"]: e for e in snap["evidence"]}
    steps = {s["id"]: s for s in snap["steps"]}
    out: list[str] = [f"# {row['title']} ({row['label']})", "", f"**Question:** {_line(row['question'])}", ""]
    state = row["state"] + (f", {row['outcome']}" if row["outcome"] else "")
    out.append(f"**State:** {state} · **Environment:** {row['instance']} · **Verification:** {row['verification']}")
    if row["follows"]:
        earlier = con.execute("SELECT label FROM investigation WHERE id = ?", (row["follows"],)).fetchone()
        out.append(f"**Follows up:** {earlier['label'] if earlier else row['follows']}")
    out += ["", "## Scope", ""]
    for r in snap["scope_revisions"]:
        why = f" — {_line(r['reason'])}" if r["reason"] else ""
        out.append(f"- Revision {r['rev']}: {r['from']} → {r['to']}{why}")
    out.append(f"- Query: `{scope['query'] or '*'}` · timezone {scope['timezone']}")

    def cites(i: dict[str, Any]) -> str:
        ids = [c for f in ("evidence", "supporting", "contradicting") for c in i["data"].get(f, [])]
        return " (" + ", ".join(f"{c} {evidence[c]['kind']}" if c in evidence else c for c in ids) + ")" if ids else ""

    checks = items.get("check", [])
    for b in items.get("branch", []):
        out += ["", f"## Branch: {_line(b['data'].get('question', b['id']))}", ""]
        if b["data"].get("alternatives"):
            out.append("Alternatives: " + "; ".join(_line(a) for a in b["data"]["alternatives"]))
        for c in (c for c in checks if b["id"] in (c["data"].get("branches") or [])):
            out.append(f"- Check `{c['id']}`: {_line(c['data'].get('purpose', ''))}")
            for s in (s for s in snap["steps"] if s["check"] == c["id"]):
                out.append(
                    f"  - {s['operation']} → {s['state']}" + (f" ({', '.join(s['evidence'])})" if s["evidence"] else "")
                )
    if items.get("assessment"):
        out += ["", "## Assessments", ""]
        for a in items["assessment"]:
            word = ASSESSMENT.get(str(a["data"].get("assessment")), "")
            out.append(f"- **{word}** — {_line(a['data'].get('claim', ''))}{cites(a)}")
    for kind, title in (("check_result", "Check results"), ("hypothesis", "Hypotheses")):
        if items.get(kind):
            out += ["", f"## {title}", ""]
            for i in items[kind]:
                text = i["data"].get("result") or i["data"].get("statement") or ""
                out.append(f"- {_line(text)}{cites(i)}")
    if items.get("identity"):
        out += ["", "## Identities", "", "| id | type | namespace | value |", "|---|---|---|---|"]
        for i in items["identity"]:
            d = i["data"]
            out.append(
                f"| {i['id']} | {_cell(d.get('type'))} | {_cell(d.get('namespace'))} | {_cell(d.get('value'))} |"
            )
    if items.get("relationship"):
        out += ["", "## Links", ""]
        for i in items["relationship"]:
            d = i["data"]
            basis = f" — {_line(d['basis'])}" if d.get("basis") else ""
            link = f"{d.get('from')} → {d.get('to')} ({_line(d.get('relation', ''))})"
            out.append(f"- {link}: **{d.get('status')}**{basis}{cites(i)}")
    if items.get("evidence_request"):
        out += ["", "## Asked of the user", ""]
        for i in items["evidence_request"]:
            d = i["data"]
            how = f", {d['provenance']}" if d.get("provenance") else ""
            answer = f" — {_line(d['answer'])}" if d.get("answer") else ""
            out.append(f"- {_line(d.get('question', ''))} **{d.get('status')}{how}**{answer}")
    for i in items.get("comparison", []):
        d = i["data"]
        out += ["", f"## Comparison {i['id']} (unit: {_line(d.get('unit', ''))})", ""]
        out += ["| cohort | count | eligible | rate |", "|---|---|---|---|"]
        for c in d.get("cohorts", []):
            known = isinstance(c.get("denominator"), int | float)
            if not known:
                rate, eligible = "unknown denominator", "unknown"
            else:
                eligible = str(c["denominator"])
                rate = f"{c['rate']:.1%}" if isinstance(c.get("rate"), int | float) else ""
            out.append(f"| {_cell(c.get('name'))} | {c.get('count', '')} | {eligible} | {rate} |")
    out += ["", "## Evidence", "", "| id | kind | matched | shown | |", "|---|---|---|---|---|"]
    for e in snap["evidence"]:
        flags = (("sampled", e["sampled"]), ("cut", e["truncated"]), ("excerpts expired", e["retention"] == "expired"))
        note = ", ".join(name for name, on in flags if on)
        state = steps.get(e["step"], {}).get("state", "")
        exact = "n/a" if e["exact_count"] is None else e["exact_count"]
        shown = "n/a" if e["returned"] is None else e["returned"]
        out.append(f"| {e['id']} | {e['kind']} ({state}) | {exact} | {shown} | {note} |")
    if snap["attempts"]:
        out += ["", "## Attempts", ""]
        for a in snap["attempts"]:
            end = f" — {a['end_reason']}" if a["end_reason"] else ""
            out.append(f"- Attempt {a['n']}: {a['started']}{end}")
            if a["checkpoint"]:
                out.append(f"  - Checkpoint: {_line(a['checkpoint'])}")
    for r in snap["reports"]:
        out += ["", "## Report", ""]
        body = r.get("report")
        if isinstance(body, dict):
            for key, title in (("findings", "Findings"), ("limits", "Limits"), ("next_checks", "Next checks")):
                value = body.get(key)
                for_key = value if isinstance(value, list) else [value] if isinstance(value, str) else []
                if for_key:
                    out += [f"**{title}**", ""] + [f"- {_line(v)}" for v in for_key] + [""]
            if isinstance(body.get("summary"), str):
                out += [_line(body["summary"]), ""]
        else:
            out += [str(body or r.get("summary") or ""), ""]
        if r.get("outcome"):
            out.append(f"Outcome: {r['outcome']}")
    return "\n".join(out).rstrip() + "\n"
