# ruff: noqa: F401, F811  (the service tests' fixtures are reused)
"""What a record must carry before it is kept: citations, identities, candidate links, requests, comparisons."""

from __future__ import annotations

import pytest

from graylog_mcp.investigations.model import InvestigationError
from tests.test_investigations_service import begin, clock, start, svc, update


def seen(svc, inv, kind="search_logs", state="succeeded"):
    """A step that ended, and its evidence id when it succeeded."""
    step = begin(svc, inv)
    got = svc.finish_step(
        step["step"], state=state, evidence=[{"kind": kind, "summary": {}}] if state == "succeeded" else None
    )
    return step["step"], (got[0] if got else None)


def rejects(svc, inv, item, message):
    before = svc.get(repo="acme-shop", investigation_id=inv["id"])["items"]
    with pytest.raises(InvestigationError, match=message):
        update(svc, inv, [item])
    assert svc.get(repo="acme-shop", investigation_id=inv["id"])["items"] == before  # nothing of it was kept


# ------------------------------------------------------------------------------------------- assessments


def test_supported_or_contradicted_must_cite_what_it_rests_on(svc):
    inv = start(svc)
    _, ev = seen(svc, inv)
    rejects(svc, inv, {"kind": "assessment", "claim": "c", "assessment": "supported"}, "must cite the evidence")
    rejects(
        svc,
        inv,
        {"kind": "assessment", "claim": "c", "assessment": "contradicted_in_scope", "supporting": [ev]},
        "contradicting or evidence",
    )
    update(
        svc,
        inv,
        [
            {"kind": "assessment", "id": "a1", "claim": "c", "assessment": "supported", "supporting": [ev]},
            {
                "kind": "assessment",
                "id": "a2",
                "claim": "c",
                "assessment": "contradicted_in_scope",
                "contradicting": [ev],
            },
            {"kind": "assessment", "id": "a3", "claim": "nothing found", "assessment": "undetermined"},
        ],
    )
    assert {i["id"] for i in svc.get(repo="acme-shop", investigation_id=inv["id"])["items"]} == {"a1", "a2", "a3"}


def test_a_step_that_did_not_succeed_leaves_nothing_to_cite(svc):
    inv = start(svc)
    _, none = seen(svc, inv, state="failed")
    assert none is None
    rejects(
        svc,
        inv,
        {"kind": "assessment", "claim": "c", "assessment": "contradicted_in_scope", "contradicting": ["evd_missing"]},
        "is not saved in this investigation",
    )


# ------------------------------------------------------------------------------------------- check results


def test_a_check_result_is_not_made_by_a_step_running(svc):
    inv = start(svc)
    _, ev = seen(svc, inv)
    update(svc, inv, [{"kind": "check", "id": "c1", "purpose": "p"}])
    base = {"kind": "check_result", "check": "c1", "result": "no errors before 14:00"}
    with pytest.raises(InvestigationError, match="a step that ran is not a result"):
        update(svc, inv, [base])
    with pytest.raises(InvestigationError, match="not a check of this investigation"):
        update(svc, inv, [{**base, "check": "c9", "evidence": [ev]}])
    with pytest.raises(InvestigationError, match="result is required"):
        update(svc, inv, [{"kind": "check_result", "check": "c1", "evidence": [ev]}])
    update(svc, inv, [{**base, "id": "r1", "evidence": [ev]}])


# ------------------------------------------------------------------------------------------- identities


def test_an_identity_is_typed_namespaced_and_has_a_source(svc):
    inv = start(svc)
    _, ev = seen(svc, inv)
    ok = {
        "kind": "identity",
        "id": "i1",
        "type": "order_id",
        "namespace": "checkout",
        "value": "o-1042",
        "evidence": [ev],
    }
    for missing in ("type", "namespace", "value"):
        rejects(svc, inv, {k: v for k, v in ok.items() if k != missing}, f"{missing} is required")
    rejects(svc, inv, {k: v for k, v in ok.items() if k != "evidence"}, "evidence is required")
    update(svc, inv, [ok])


def test_a_redaction_placeholder_is_never_an_identity(svc):
    inv = start(svc)
    _, ev = seen(svc, inv)
    base = {"kind": "identity", "type": "user", "namespace": "auth", "evidence": [ev]}
    rejects(svc, inv, {**base, "value": "[EMAIL]"}, "redaction placeholder")
    rejects(svc, inv, {**base, "value": "user-[REDACTED]"}, "redaction placeholder")
    rejects(svc, inv, {**base, "value": "password=fake-secret-12345"}, "masked by redaction")  # became one on redacting


# ------------------------------------------------------------------------------------------- relationships


def _identities(svc, inv, ev):
    update(
        svc,
        inv,
        [
            {"kind": "identity", "id": n, "type": "order_id", "namespace": "x", "value": v, "evidence": [ev]}
            for n, v in (("i1", "o-1"), ("i2", "o-1b"))
        ],
    )


def test_a_link_is_a_candidate_until_it_is_observed_with_evidence(svc):
    inv = start(svc)
    _, ev = seen(svc, inv)
    _identities(svc, inv, ev)
    link = {"kind": "relationship", "id": "l1", "from": "i1", "to": "i2", "relation": "published"}
    rejects(svc, inv, {k: v for k, v in link.items() if k != "relation"}, "relation is required")
    rejects(svc, inv, {**link, "to": "i9"}, "is not a identity of this investigation")
    rejects(svc, inv, {**link, "status": "probable"}, "status must be one of")
    rejects(svc, inv, {**link, "status": "observed", "basis": "same order id"}, "observed needs evidence")
    update(svc, inv, [link])  # time order or a shared key alone: stays a candidate
    snap = svc.get(repo="acme-shop", investigation_id=inv["id"])
    (kept,) = [i for i in snap["items"] if i["id"] == "l1"]
    assert kept["data"]["status"] == "candidate"
    with pytest.raises(InvestigationError, match="basis is required"):  # promoting says why
        update(svc, inv, [{**link, "revision": 1, "status": "observed", "evidence": [ev]}])
    update(
        svc,
        inv,
        [{**link, "revision": 1, "status": "observed", "basis": "producer id equals consumer id", "evidence": [ev]}],
    )


# ------------------------------------------------------------------------------------------- requests


def test_an_evidence_request_keeps_who_said_so(svc):
    inv = start(svc)
    update(svc, inv, [{"kind": "branch", "id": "b1", "question": "q"}])
    ask = {"kind": "evidence_request", "id": "q1", "question": "Did the broker redeliver?", "branch": "b1"}
    rejects_nothing = update(svc, inv, [ask])
    assert rejects_nothing
    snap = svc.get(repo="acme-shop", investigation_id=inv["id"])
    (kept,) = [i for i in snap["items"] if i["id"] == "q1"]
    assert kept["data"]["status"] == "requested"
    with pytest.raises(InvestigationError, match="received needs provenance"):
        update(svc, inv, [{**ask, "revision": 1, "status": "received"}])
    with pytest.raises(InvestigationError, match="is not a branch"):
        update(svc, inv, [{**ask, "revision": 1, "branch": "b9"}])
    update(svc, inv, [{**ask, "revision": 1, "status": "received", "provenance": "user_reported", "answer": "twice"}])


# ------------------------------------------------------------------------------------------- comparisons


def test_a_rate_needs_a_known_denominator(svc):
    inv = start(svc)
    cohorts = [{"name": "v1", "count": 3, "denominator": 100, "rate": 0.03}, {"name": "v2", "count": 9}]
    base = {"kind": "comparison", "id": "m1", "unit": "requests", "cohorts": cohorts}
    update(svc, inv, [base])
    rejects_rate = {**base, "id": "m2", "cohorts": [*cohorts[:1], {"name": "v2", "count": 9, "rate": 0.5}]}
    with pytest.raises(InvestigationError, match="no known denominator"):
        update(svc, inv, [rejects_rate])
    with pytest.raises(InvestigationError, match="at least two"):
        update(svc, inv, [{**base, "id": "m3", "cohorts": cohorts[:1]}])
    with pytest.raises(InvestigationError, match="unit is required"):
        update(svc, inv, [{"kind": "comparison", "id": "m4", "cohorts": cohorts}])
