# ruff: noqa: F401, F811  (the service tests' fixtures are reused)
"""Hard cases of an investigation, from start through checkpoint and resume to export: what the agent concluded must
come back as it was written, with the alternatives, the units of its counts and why it stopped."""

from __future__ import annotations

import pytest

from graylog_mcp.investigations.model import InvestigationError
from tests.test_investigations_service import REDACT, begin, clock, finish, start, svc, update


def ran(svc, inv, check=None, kind="search_logs", count=None, **evidence):
    step = begin(svc, inv, check_id=check)
    (ev,) = svc.finish_step(
        step["step"],
        state="succeeded",
        evidence=[{"kind": kind, "summary": {}, "exact_count": count, "returned": count, **evidence}],
    )
    return ev


def through_a_pause(svc, inv, text):
    """Checkpoint, then a new session resumes: the way a record has to survive."""
    svc.checkpoint(
        repo="acme-shop", investigation_id=inv["id"], owner_fence=inv["owner_fence"], text=text, redact=REDACT
    )
    return svc.resume(repo="acme-shop", investigation_id=inv["id"])


def exported(svc, inv):
    return svc.get(repo="acme-shop", investigation_id=inv["id"], view="export")["markdown"]


def test_two_deliveries_and_one_verified_effect_stay_two_deliveries(svc):
    inv = start(svc)
    d1, d2, effect = ran(svc, inv, count=1), ran(svc, inv, count=1), ran(svc, inv, kind="get_message", count=1)
    update(
        svc,
        inv,
        [
            {
                "kind": "identity",
                "id": "m1",
                "type": "message_id",
                "namespace": "broker",
                "value": "msg-7",
                "evidence": [d1, d2],
            },
            {
                "kind": "identity",
                "id": "p1",
                "type": "payment_id",
                "namespace": "ledger",
                "value": "pay-9",
                "evidence": [effect],
            },
            {
                "kind": "relationship",
                "id": "l1",
                "from": "m1",
                "to": "p1",
                "relation": "delivered twice, charged once",
                "status": "observed",
                "basis": "ledger lists one payment for msg-7",
                "evidence": [effect],
            },
            {
                "kind": "assessment",
                "id": "a1",
                "claim": "the message was delivered twice; one charge resulted",
                "assessment": "supported",
                "supporting": [d1, d2, effect],
            },
        ],
    )
    back = through_a_pause(svc, inv, "check the worker's acknowledgements next")
    md = exported(svc, back)
    assert "msg-7" in md and "pay-9" in md and "delivered twice, charged once" in md and "**observed**" in md
    assert "delivered twice; one charge" in md and "check the worker" in md


def test_two_messages_published_for_one_order_keep_both_and_the_unit(svc):
    inv = start(svc)
    a, b = ran(svc, inv, count=2), ran(svc, inv, count=1)
    update(
        svc,
        inv,
        [
            {
                "kind": "branch",
                "id": "b1",
                "question": "Was the order published twice?",
                "alternatives": ["one publish, retried by the client"],
            },
            {
                "kind": "comparison",
                "id": "m1",
                "unit": "published messages per order",
                "cohorts": [
                    {"name": "order o-1", "count": 2, "denominator": 1, "rate": 2.0},
                    {"name": "order o-2", "count": 1},
                ],
            },
            {
                "kind": "assessment",
                "id": "a1",
                "claim": "two publishes for o-1",
                "assessment": "supported",
                "supporting": [a, b],
            },
        ],
    )
    md = exported(svc, through_a_pause(svc, inv, "waiting"))
    assert "published messages per order" in md and "one publish, retried by the client" in md
    assert "| order o-2 | 1 | unknown | unknown denominator |" in md  # no rate where nobody knows the denominator


def test_missing_broker_metadata_is_an_undetermined_assessment_and_a_blocked_request(svc):
    inv = start(svc)
    update(
        svc,
        inv,
        [
            {"kind": "branch", "id": "b1", "question": "Did the broker redeliver?"},
            {
                "kind": "assessment",
                "id": "a1",
                "claim": "no delivery attempt field in these logs",
                "assessment": "undetermined",
            },
            {"kind": "evidence_request", "id": "q1", "branch": "b1", "question": "Broker delivery count for msg-7?"},
        ],
    )
    back = through_a_pause(svc, inv, "need the broker's own record")
    out = svc.get(repo="acme-shop", investigation_id=back["id"], view="briefing")
    assert [r["id"] for r in out["blocked"]] == ["q1"] and out["assessments"][0]["assessment"] == "undetermined"
    done = finish(svc, back, outcome="cause_undetermined", report={"limits": "broker metadata is not logged"})
    assert done["outcome"] == "cause_undetermined"
    assert "broker metadata is not logged" in exported(svc, back) and "requested" in exported(svc, back)


def test_unequal_version_cohorts_do_not_make_a_rate(svc):
    inv = start(svc)
    with pytest.raises(InvestigationError, match="no known denominator"):
        update(
            svc,
            inv,
            [
                {
                    "kind": "comparison",
                    "id": "m1",
                    "unit": "requests",
                    "cohorts": [
                        {"name": "v1", "count": 5, "denominator": 1000, "rate": 0.005},
                        {"name": "v2", "count": 5, "rate": 1.0},
                    ],
                }
            ],
        )


def test_two_independent_causes_are_two_supported_assessments(svc):
    inv = start(svc)
    a, b = ran(svc, inv, count=4), ran(svc, inv, count=9)
    update(
        svc,
        inv,
        [
            {
                "kind": "assessment",
                "id": "a1",
                "claim": "a bad deploy of payment",
                "assessment": "supported",
                "supporting": [a],
            },
            {
                "kind": "assessment",
                "id": "a2",
                "claim": "a full disk on the queue host",
                "assessment": "supported",
                "supporting": [b],
            },
        ],
    )
    done = finish(
        svc,
        through_a_pause(svc, inv, "two causes, rank them"),
        outcome="explanation_supported",
        report={"summary": "both explain a share; neither explains all"},
    )
    md = exported(svc, done)
    assert md.count("**supported**") == 2 and "neither explains all" in md


def test_the_same_evidence_cited_by_two_branches_is_one_record(svc):
    inv = start(svc)
    ev = ran(svc, inv, count=3)
    update(
        svc,
        inv,
        [
            {"kind": "assessment", "id": "a1", "claim": "c1", "assessment": "supported", "supporting": [ev]},
            {
                "kind": "assessment",
                "id": "a2",
                "claim": "c2",
                "assessment": "contradicted_in_scope",
                "contradicting": [ev],
            },
        ],
    )
    snap = svc.get(repo="acme-shop", investigation_id=inv["id"])
    assert [e["id"] for e in snap["evidence"]] == [ev]  # cited twice, stored once


def test_a_verification_follows_a_completed_investigation_and_changes_nothing_of_it(svc):
    inv = start(svc)
    ev = ran(svc, inv, count=5)
    update(
        svc,
        inv,
        [
            {
                "kind": "assessment",
                "id": "a1",
                "claim": "the deploy broke it",
                "assessment": "supported",
                "supporting": [ev],
            }
        ],
    )
    with pytest.raises(InvestigationError, match="only a completed investigation is followed up"):
        start(svc, follows=inv["id"])
    done = finish(svc, inv, outcome="explanation_supported")
    before = svc.get(repo="acme-shop", investigation_id=inv["id"])
    check = start(svc, title="After the fix", question="Did the errors stop?", follows=inv["id"])
    assert check["follows"] == inv["id"]
    after = svc.get(repo="acme-shop", investigation_id=inv["id"])
    assert (after["outcome"], after["state"], after["revision"]) == (
        before["outcome"],
        before["state"],
        before["revision"],
    )
    assert after["followed_by"] == [check["label"]] and done["outcome"] == "explanation_supported"
    final = finish(svc, check, outcome="no_anomaly_detected", verification="recovery_verified")
    assert final["verification"] == "recovery_verified"
    assert svc.get(repo="acme-shop", investigation_id=inv["id"])["outcome"] == "explanation_supported"
    assert "Follows up" in exported(svc, check)


def test_the_briefing_stays_bounded_with_many_records(svc):
    inv = start(svc)
    update(svc, inv, [{"kind": "check", "id": f"c{i}", "purpose": "p" * 150} for i in range(40)])
    out = svc.get(repo="acme-shop", investigation_id=inv["id"], view="briefing")
    assert len(str(out)) < 9_000 and out["next_checks"]


def test_the_export_holds_nothing_the_redactor_masks(svc):
    inv = start(svc, question="Why did card 4111 1111 1111 1111 fail for fake@example.test?")
    md = exported(svc, inv)
    assert "4111 1111 1111 1111" not in md


def test_a_report_with_findings_limits_and_next_checks_exports_as_lists(svc):
    inv = start(svc)
    ran(svc, inv, count=3)
    done = finish(
        svc,
        inv,
        outcome="cause_undetermined",
        report={
            "findings": ["errors are spread over ten sources"],
            "limits": ["one environment"],
            "next_checks": ["group by route"],
        },
    )
    md = exported(svc, done)
    assert "**Findings**\n\n- errors are spread over ten sources" in md
    assert "**Limits**\n\n- one environment" in md and "**Next checks**\n\n- group by route" in md
    assert "{'findings'" not in md and "Outcome: cause_undetermined" in md
