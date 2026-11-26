"""What each kind of record must say before it is kept.

The service stores what the agent concludes; these rules only refuse a record that claims more than it carries: an
assessment with nothing cited, an identity joined on a redaction placeholder, a link called observed with no evidence,
a rate over a denominator nobody knows. They never decide what is true.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from graylog_mcp.investigations.model import InvestigationError

PLACEHOLDER = re.compile(r"\[[A-Z][A-Z0-9_]*\]")  # [REDACTED], [EMAIL], [CARD] ...
RELATIONSHIP_STATUS = ("candidate", "observed")
REQUEST_STATUS = ("requested", "received", "withdrawn")
PROVENANCE = ("user_reported", "unverified", "verified")
CITE_FIELDS = ("evidence", "supporting", "contradicting")


def _cited(data: dict[str, Any], *names: str) -> list[str]:
    out: list[str] = []
    for name in names or CITE_FIELDS:
        value = data.get(name)
        if isinstance(value, list):
            out += [str(v) for v in value]
    return out


def _text(data: dict[str, Any], name: str, kind: str, ident: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value.strip():
        raise InvestigationError(f"{kind} {ident}: {name} is required")
    return value


def _exists(con: sqlite3.Connection, inv: str, kind: str, ident: Any, what: str, owner: str) -> None:
    if (
        not isinstance(ident, str)
        or not con.execute(
            "SELECT 1 FROM item WHERE investigation = ? AND kind = ? AND id = ?", (inv, kind, ident)
        ).fetchone()
    ):
        raise InvestigationError(f"{owner}: {what} {ident!r} is not a {kind} of this investigation")


def _only_succeeded(con: sqlite3.Connection, inv: str, ids: list[str], owner: str) -> None:
    """A failed, interrupted or fenced-out step is no basis for a conclusion."""
    for evidence_id in ids:
        row = con.execute(
            "SELECT s.state, s.fenced_out FROM evidence e JOIN step s ON s.id = e.step "
            "WHERE e.id = ? AND e.investigation = ?",
            (evidence_id, inv),
        ).fetchone()
        if row is None or row["state"] != "succeeded" or row["fenced_out"]:
            raise InvestigationError(f"{owner}: evidence {evidence_id!r} does not come from a step that succeeded")


def after_redaction(kind: str, body: dict[str, Any], ident: str) -> None:
    """What is stored is the redacted record: an identity whose value only became a placeholder by redaction (a card
    number, an email) cannot be a join key either."""
    if kind == "identity" and PLACEHOLDER.search(str(body.get("value", ""))):
        raise InvestigationError(
            f"identity {ident}: the value is masked by redaction; records are never joined on a placeholder"
        )


def check(con: sqlite3.Connection, inv: str, kind: str, ident: str, data: dict[str, Any]) -> dict[str, Any]:
    """Validate ``data`` for ``kind`` and return it with its defaults filled in."""
    owner = f"{kind} {ident}"
    if kind in ("assessment", "check_result", "identity", "relationship", "comparison"):
        _only_succeeded(con, inv, _cited(data), owner)
    if kind == "assessment" and data.get("assessment") in ("supported", "contradicted_in_scope"):
        needs = ("contradicting", "evidence") if data["assessment"] == "contradicted_in_scope" else CITE_FIELDS
        if not _cited(data, *needs):
            raise InvestigationError(
                f"{owner}: {data['assessment']} must cite the evidence it rests on "
                f"({' or '.join(needs)}); say undetermined when nothing was found"
            )
    elif kind == "check_result":
        _exists(con, inv, "check", data.get("check"), "check", owner)
        _text(data, "result", kind, ident)
        if not _cited(data):
            raise InvestigationError(f"{owner}: evidence is required; a step that ran is not a result")
    elif kind == "identity":
        for name in ("type", "namespace", "value"):
            _text(data, name, kind, ident)
        if PLACEHOLDER.search(data["value"]):
            raise InvestigationError(f"{owner}: the value is a redaction placeholder; records are never joined on one")
        if not _cited(data):
            raise InvestigationError(f"{owner}: evidence is required: where was this value seen")
    elif kind == "relationship":
        _text(data, "relation", kind, ident)  # how from relates to to: "published", "delivered", "retried" ...
        _exists(con, inv, "identity", data.get("from"), "from", owner)
        _exists(con, inv, "identity", data.get("to"), "to", owner)
        data = {**data, "status": data.get("status") or "candidate"}
        if data["status"] not in RELATIONSHIP_STATUS:
            raise InvestigationError(f"{owner}: status must be one of: {', '.join(RELATIONSHIP_STATUS)}")
        if data["status"] == "observed":
            _text(data, "basis", kind, ident)
            if not _cited(data):
                raise InvestigationError(
                    f"{owner}: observed needs evidence of the link itself; same key, similar text or nearby "
                    "times make it a candidate"
                )
    elif kind == "evidence_request":
        _text(data, "question", kind, ident)
        data = {**data, "status": data.get("status") or "requested"}
        if data["status"] not in REQUEST_STATUS:
            raise InvestigationError(f"{owner}: status must be one of: {', '.join(REQUEST_STATUS)}")
        if data["status"] == "received" and data.get("provenance") not in PROVENANCE:
            raise InvestigationError(f"{owner}: received needs provenance, one of: {', '.join(PROVENANCE)}")
        if data.get("branch") is not None:
            _exists(con, inv, "branch", data["branch"], "branch", owner)
    elif kind == "comparison":
        _text(data, "unit", kind, ident)
        cohorts = data.get("cohorts")
        if not isinstance(cohorts, list) or len(cohorts) < 2:
            raise InvestigationError(f"{owner}: cohorts is a list of at least two {{name, count, denominator?}}")
        for cohort in cohorts:
            if not isinstance(cohort, dict) or not cohort.get("name"):
                raise InvestigationError(f"{owner}: each cohort has a name")
            if "rate" in cohort and not isinstance(cohort.get("denominator"), int | float):
                raise InvestigationError(
                    f"{owner}: cohort {cohort['name']!r} has a rate but no known denominator; give counts, not a rate"
                )
    return data
