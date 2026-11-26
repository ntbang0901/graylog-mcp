"""Investigation records: lifecycle, ownership, revisions, retries, what is stored."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import pytest

from graylog_mcp.config import RedactionConfig
from graylog_mcp.investigations import briefing
from graylog_mcp.investigations import store as store_module
from graylog_mcp.investigations.model import (
    LEASE_SECONDS,
    Conflict,
    InvestigationError,
    NotFound,
    NotOwner,
    Scope,
)
from graylog_mcp.investigations.service import Investigations
from graylog_mcp.investigations.store import Store
from graylog_mcp.redact import Redactor

SCOPE = Scope(
    from_ts="2026-11-18T03:00:00.000Z",
    to_ts="2026-11-18T04:00:00.000Z",
    base_from="2026-11-18T02:00:00.000Z",
    base_to="2026-11-18T03:00:00.000Z",
    timezone="Asia/Ho_Chi_Minh",
    query="level:<=3",
    streams=("checkout",),
)
REDACT = Redactor(RedactionConfig(packs=("vn",))).value


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def svc(tmp_path, clock) -> Investigations:
    return Investigations(Store(tmp_path / "investigations.db"), clock)


def start(svc: Investigations, repo: str | None = "acme-shop", **kw):
    args = {"repo": repo, "instance": "main", "title": "Double charge", "question": "Why two charges?"}
    return svc.start(scope=SCOPE, redact=REDACT, **{**args, **kw})


# ------------------------------------------------------------------------------------------- start


def test_start_fixes_the_scope_in_absolute_times_and_hands_back_the_owner_fence(svc):
    out = start(svc)
    assert out["id"].startswith("inv_") and out["label"] == "INV-1" and out["state"] == "running"
    assert out["owner_fence"].startswith("1:")
    assert out["scope"]["rev"] == 1 and out["scope"]["from"] == "2026-11-18T03:00:00.000Z"
    assert out["scope"]["streams"] == ["checkout"] and out["scope"]["timezone"] == "Asia/Ho_Chi_Minh"
    assert start(svc)["label"] == "INV-2"  # a per-machine display label, the key stays opaque


def test_the_token_is_not_stored(svc, tmp_path):
    out = start(svc)
    token = out["owner_fence"].split(":", 1)[1]
    assert token.encode() not in (tmp_path / "investigations.db").read_bytes()


def test_start_retried_with_the_same_operation_id_is_the_same_investigation_with_a_new_fence(svc):
    first = start(svc, operation_id="op-1")
    again = start(svc, operation_id="op-1")
    assert again["id"] == first["id"] and again["deduplicated"] is True
    assert again["owner_fence"] != first["owner_fence"]
    with pytest.raises(NotOwner):  # the answer that was lost carried a token that no longer works
        svc.update(
            repo="acme-shop",
            investigation_id=first["id"],
            owner_fence=first["owner_fence"],
            items=[{"kind": "branch", "id": "b1", "question": "q"}],
            scope=None,
            reason=None,
            expected_revision=None,
            redact=REDACT,
        )
    assert len(svc.list(repo="acme-shop")) == 1


def test_another_repository_cannot_see_or_use_an_investigation(svc):
    out = start(svc)
    for repo in ("other", None):
        with pytest.raises(NotFound):
            svc.get(repo=repo, investigation_id=out["id"])
        assert svc.list(repo=repo) == []
    assert svc.get(repo="acme-shop", investigation_id="inv-1")["id"] == out["id"]  # by label too


# ------------------------------------------------------------------------------------------- lifecycle


def update(svc, inv, items=None, **kw):
    args = {
        "repo": "acme-shop",
        "investigation_id": inv["id"],
        "owner_fence": inv["owner_fence"],
        "items": items,
        "scope": None,
        "reason": None,
        "expected_revision": None,
        "redact": REDACT,
    }
    return svc.update(**{**args, **kw})


def finish(svc, inv, **kw):
    args = {
        "repo": "acme-shop",
        "investigation_id": inv["id"],
        "owner_fence": inv["owner_fence"],
        "outcome": "cause_undetermined",
        "report": {"limits": "no trace ids"},
        "verification": None,
        "redact": REDACT,
    }
    return svc.finish(**{**args, **kw})


def test_checkpoint_then_resume_then_finish(svc):
    inv = start(svc)
    cp = svc.checkpoint(
        repo="acme-shop",
        investigation_id=inv["id"],
        owner_fence=inv["owner_fence"],
        text="Next: worker logs",
        redact=REDACT,
    )
    assert cp["state"] == "awaiting_input"
    with pytest.raises(InvestigationError, match="awaiting_input, not running"):  # the attempt ended with it
        update(svc, inv, [{"kind": "branch", "id": "b1", "question": "q"}])
    back = svc.resume(repo="acme-shop", investigation_id=inv["id"])
    assert back["state"] == "running" and back["briefing"]["checkpoint"] == "Next: worker logs"
    assert back["owner_fence"].startswith("2:")
    done = finish(svc, back)
    assert done["state"] == "completed" and done["outcome"] == "cause_undetermined"


def test_forbidden_transitions_say_what_state_it_is_in(svc):
    inv = start(svc)
    svc.cancel(repo="acme-shop", investigation_id=inv["id"], owner_fence=inv["owner_fence"], redact=REDACT)
    with pytest.raises(InvestigationError, match="was cancelled"):
        svc.resume(repo="acme-shop", investigation_id=inv["id"])
    with pytest.raises(InvestigationError, match="cancelled, not running"):
        update(svc, inv, [{"kind": "branch", "id": "b1", "question": "q"}])
    with pytest.raises(InvestigationError, match="cancelled, not running"):
        finish(svc, inv)


def test_finish_needs_an_outcome_a_report_and_settled_steps(svc):
    inv = start(svc)
    with pytest.raises(InvestigationError, match="outcome must be one of"):
        finish(svc, inv, outcome="solved")
    with pytest.raises(InvestigationError, match="report is required"):
        finish(svc, inv, report=None)
    svc.begin_step(
        repo="acme-shop",
        investigation_id=inv["id"],
        owner_fence=inv["owner_fence"],
        check_id=None,
        operation="search_logs",
        tool="search_logs",
        source="mcp",
        requested={},
        instance="main",
        call_id="c1",
    )
    with pytest.raises(InvestigationError, match="still running"):
        finish(svc, inv)


def test_a_finished_investigation_keeps_its_report_when_it_is_resumed_and_finished_again(svc):
    inv = start(svc)
    finish(svc, inv, outcome="explanation_supported", report={"summary": "first"})
    again = svc.resume(repo="acme-shop", investigation_id=inv["id"])
    finish(svc, again, outcome="cause_undetermined", report={"summary": "second"})
    snap = svc.get(repo="acme-shop", investigation_id=inv["id"])
    assert [r["report"]["summary"] for r in snap["reports"]] == ["first", "second"]
    assert [a["n"] for a in snap["attempts"]] == [1, 2]


# ------------------------------------------------------------------------------------------- ownership


def test_a_second_session_cannot_take_a_live_investigation_without_saying_so(svc):
    inv = start(svc)
    with pytest.raises(NotOwner, match="takeover"):
        svc.resume(repo="acme-shop", investigation_id=inv["id"])
    taken = svc.resume(repo="acme-shop", investigation_id=inv["id"], takeover=True)
    assert taken["owner_fence"].startswith("2:")
    with pytest.raises(NotOwner, match="no longer valid"):  # the first owner is fenced out
        update(svc, inv, [{"kind": "branch", "id": "b1", "question": "q"}])
    update(svc, taken, [{"kind": "branch", "id": "b1", "question": "q"}])


def test_the_owner_can_resume_its_own_investigation_without_takeover(svc):
    inv = start(svc)
    again = svc.resume(repo="acme-shop", investigation_id=inv["id"], owner_fence=inv["owner_fence"])
    assert again["state"] == "running" and again["owner_fence"].startswith("2:")


def test_a_lease_that_ran_out_interrupts_the_investigation_and_its_running_steps(svc, clock):
    inv = start(svc)
    step = svc.begin_step(
        repo="acme-shop",
        investigation_id=inv["id"],
        owner_fence=inv["owner_fence"],
        check_id=None,
        operation="search_logs",
        tool="search_logs",
        source="mcp",
        requested={},
        instance="main",
        call_id="c1",
    )
    clock.advance(LEASE_SECONDS + 1)
    snap = svc.get(repo="acme-shop", investigation_id=inv["id"])
    assert snap["state"] == "interrupted"
    assert snap["steps"][0]["state"] == "interrupted" and "lease_expired" in snap["steps"][0]["error"]
    # the step's late answer cannot turn it into a success
    assert svc.finish_step(step["step"], state="succeeded") == []
    assert svc.get(repo="acme-shop", investigation_id=inv["id"])["steps"][0]["state"] == "interrupted"
    # anyone may take an interrupted investigation over without takeover, and the old owner is out
    back = svc.resume(repo="acme-shop", investigation_id=inv["id"])
    assert back["state"] == "running"
    with pytest.raises(NotOwner):
        update(svc, inv, [{"kind": "branch", "id": "b1", "question": "q"}])


def test_owned_calls_renew_the_lease(svc, clock):
    inv = start(svc)
    for _ in range(3):
        clock.advance(LEASE_SECONDS - 10)
        update(svc, inv, [{"kind": "hypothesis", "statement": "retry storm"}])
    assert svc.get(repo="acme-shop", investigation_id=inv["id"])["state"] == "running"


def test_a_malformed_fence_is_refused_with_the_way_out(svc):
    inv = start(svc)
    for bad in (None, "", "x", "1:", "abc:def"):
        with pytest.raises(NotOwner, match="owner_fence"):
            update(svc, {**inv, "owner_fence": bad}, [{"kind": "branch", "question": "q"}])
    with pytest.raises(NotOwner):
        update(svc, {**inv, "owner_fence": "1:wrong-token"}, [{"kind": "branch", "question": "q"}])


# ------------------------------------------------------------------------------------------- records


def test_records_are_revisioned_and_changing_one_needs_the_revision_it_was_read_at(svc):
    inv = start(svc)
    saved = update(svc, inv, [{"kind": "branch", "id": "b1", "question": "Did the gateway retry?"}])
    assert saved["items"] == [{"kind": "branch", "id": "b1", "revision": 1}]
    with pytest.raises(Conflict, match="pass revision=1"):
        update(svc, inv, [{"kind": "branch", "id": "b1", "question": "changed"}])
    with pytest.raises(Conflict):
        update(svc, inv, [{"kind": "branch", "id": "b1", "revision": 7, "question": "changed"}])
    update(svc, inv, [{"kind": "branch", "id": "b1", "revision": 1, "question": "Did the gateway retry twice?"}])
    snap = svc.get(repo="acme-shop", investigation_id=inv["id"])
    (branch,) = snap["items"]
    assert branch["revision"] == 2 and branch["data"]["question"].endswith("twice?")
    with svc.store.read() as con:
        assert con.execute("SELECT COUNT(*) FROM item_history WHERE id = 'b1'").fetchone()[0] == 2


def test_expected_revision_guards_the_investigation(svc):
    inv = start(svc)
    first = update(svc, inv, [{"kind": "branch", "id": "b1", "question": "q"}], expected_revision=1)
    assert first["revision"] == 2
    with pytest.raises(Conflict, match="revision 2, not 1"):
        update(svc, inv, [{"kind": "branch", "id": "b2", "question": "q"}], expected_revision=1)
    snap = svc.get(repo="acme-shop", investigation_id=inv["id"])
    assert [i["id"] for i in snap["items"]] == ["b1"]  # nothing of the refused update was written


def test_an_update_is_all_or_nothing(svc):
    inv = start(svc)
    with pytest.raises(InvestigationError, match="purpose is required"):
        update(svc, inv, [{"kind": "branch", "id": "b1", "question": "q"}, {"kind": "check", "id": "c1"}])
    assert svc.get(repo="acme-shop", investigation_id=inv["id"])["items"] == []


def test_a_retried_update_with_the_same_operation_id_changes_nothing_twice(svc):
    inv = start(svc)
    first = update(svc, inv, [{"kind": "hypothesis", "statement": "retry storm"}], operation_id="u1")
    again = update(svc, inv, [{"kind": "hypothesis", "statement": "retry storm"}], operation_id="u1")
    assert again["deduplicated"] is True and again["items"] == first["items"]
    assert len(svc.get(repo="acme-shop", investigation_id=inv["id"])["items"]) == 1
    with pytest.raises(InvestigationError, match="already used for update"):
        svc.cancel(
            repo="acme-shop",
            investigation_id=inv["id"],
            owner_fence=inv["owner_fence"],
            redact=REDACT,
            operation_id="u1",
        )


def test_a_scope_change_is_a_new_revision_with_a_reason_and_the_old_one_stays(svc):
    inv = start(svc)
    wider = Scope(**{**SCOPE.__dict__, "from_ts": "2026-11-18T02:30:00.000Z"})
    with pytest.raises(InvestigationError, match="reason"):
        update(svc, inv, scope=wider)
    out = update(svc, inv, scope=wider, reason="the first charge was earlier")
    assert out["scope_rev"] == 2
    snap = svc.get(repo="acme-shop", investigation_id=inv["id"])
    assert snap["scope"]["rev"] == 2 and snap["scope"]["from"].startswith("2026-11-18T02:30")
    assert [r["rev"] for r in snap["scope_revisions"]] == [1, 2]
    assert snap["scope_revisions"][1]["reason"] == "the first charge was earlier"


def test_assessments_name_evidence_that_exists_here_and_one_of_the_three_outcomes(svc):
    inv = start(svc)
    with pytest.raises(InvestigationError, match="evidence 'ev_nope' is not saved"):
        update(svc, inv, [{"kind": "assessment", "claim": "c", "assessment": "supported", "evidence": ["ev_nope"]}])
    with pytest.raises(InvestigationError, match="assessment must be one of"):
        update(svc, inv, [{"kind": "assessment", "claim": "c", "assessment": "proven"}])
    with pytest.raises(InvestigationError, match="item kind must be one of"):
        update(svc, inv, [{"kind": "verdict"}])
    with pytest.raises(InvestigationError, match="does not exist in this investigation"):
        update(svc, inv, [{"kind": "check", "id": "c1", "purpose": "p", "branches": ["b9"]}])


def test_ids_chosen_by_the_agent_are_plain_tokens(svc):
    inv = start(svc)
    for bad in ("../x", "a b", "", "é", "x" * 65):
        with pytest.raises(InvestigationError, match="id"):
            update(svc, inv, [{"kind": "branch", "id": bad, "question": "q"}])


def test_what_the_agent_writes_is_redacted_before_it_is_stored(svc, tmp_path):
    inv = start(svc, question="Why did password=fake-pass-123 appear?")
    update(svc, inv, [{"kind": "hypothesis", "statement": "token: password=fake-pass-456 was logged"}])
    svc.checkpoint(
        repo="acme-shop",
        investigation_id=inv["id"],
        owner_fence=inv["owner_fence"],
        text="password=fake-pass-789",
        redact=REDACT,
    )
    svc.store.sweep()
    raw = b"".join(p.read_bytes() for p in tmp_path.glob("investigations.db*"))
    assert b"fake-pass-" not in raw and b"[REDACTED]" in raw


# ------------------------------------------------------------------------------------------- steps and evidence


def begin(svc, inv, **kw):
    args = {
        "repo": "acme-shop",
        "investigation_id": inv["id"],
        "owner_fence": inv["owner_fence"],
        "check_id": None,
        "operation": "search_logs",
        "tool": "search_logs",
        "source": "mcp",
        "requested": {"query": "level:3"},
        "instance": "main",
        "call_id": "call-1",
    }
    return svc.begin_step(**{**args, **kw})


def test_a_step_is_written_running_before_it_ends_and_its_evidence_is_one_transaction(svc):
    inv = start(svc)
    step = begin(svc, inv)
    mid = svc.get(repo="acme-shop", investigation_id=inv["id"])
    assert mid["steps"][0]["state"] == "running" and mid["steps"][0]["check"] is None
    assert [e["type"] for e in svc.events(repo="acme-shop", investigation_id=inv["id"])["events"]][-1] == "step_started"
    (ev,) = svc.finish_step(
        step["step"],
        state="succeeded",
        executed={"query": "level:3"},
        evidence=[
            {"kind": "search_logs", "summary": {"total": 12}, "excerpts": [{"message": "boom"}], "exact_count": 12}
        ],
    )
    snap = svc.get(repo="acme-shop", investigation_id=inv["id"])
    assert snap["steps"][0]["state"] == "succeeded" and snap["steps"][0]["evidence"] == [ev]
    full = svc.get(repo="acme-shop", investigation_id=inv["id"], view="evidence", evidence_id=ev)
    assert (
        full["exact_count"] == 12 and full["excerpts"] == [{"message": "boom"}] and full["step"]["state"] == "succeeded"
    )


def test_a_step_needs_the_owner_the_instance_and_an_existing_check(svc):
    inv = start(svc)
    with pytest.raises(NotOwner):
        begin(svc, {**inv, "owner_fence": "9:nope"})
    with pytest.raises(InvestigationError, match="about instance 'main'"):
        begin(svc, inv, instance="other")
    with pytest.raises(InvestigationError, match="check 'c9' does not exist"):
        begin(svc, inv, check_id="c9")
    update(svc, inv, [{"kind": "check", "id": "c9", "purpose": "see the retry"}])
    assert begin(svc, inv, check_id="c9")["step"]


def test_a_late_result_of_a_fenced_out_attempt_stays_with_its_own_attempt(svc):
    inv = start(svc)
    step = begin(svc, inv)
    new = svc.resume(repo="acme-shop", investigation_id=inv["id"], takeover=True)
    update(svc, new, [{"kind": "assessment", "claim": "c", "assessment": "undetermined"}])
    (ev,) = svc.finish_step(step["step"], state="succeeded", evidence=[{"kind": "search_logs", "summary": {}}])
    snap = svc.get(repo="acme-shop", investigation_id=inv["id"])
    late = next(s for s in snap["steps"] if s["id"] == step["step"])
    assert late["fenced_out"] is True and late["attempt"] == step["attempt"] != snap["attempts"][-1]["id"]
    assert late["evidence"] == [ev]
    assert len(snap["items"]) == 1 and snap["state"] == "running"  # the new attempt's record is untouched


def test_a_checkpoint_waits_for_the_steps_that_are_running(svc):
    inv = start(svc)
    step = begin(svc, inv)
    cp = svc.checkpoint(
        repo="acme-shop", investigation_id=inv["id"], owner_fence=inv["owner_fence"], text="x", redact=REDACT
    )
    assert cp["state"] == "running"
    svc.finish_step(step["step"], state="succeeded")
    assert svc.get(repo="acme-shop", investigation_id=inv["id"])["state"] == "awaiting_input"


def test_a_step_cannot_end_in_a_state_it_did_not_report(svc):
    inv = start(svc)
    step = begin(svc, inv)
    with pytest.raises(ValueError, match="succeeded, failed or cancelled"):
        svc.finish_step(step["step"], state="interrupted")


def test_events_are_numbered_and_replay_from_a_cursor(svc):
    inv = start(svc)
    update(svc, inv, [{"kind": "branch", "id": "b1", "question": "q"}])
    allof = svc.events(repo="acme-shop", investigation_id=inv["id"])["events"]
    assert [e["seq"] for e in allof] == list(range(1, len(allof) + 1))
    assert [e["type"] for e in allof] == ["started", "item_saved"]
    snap = svc.get(repo="acme-shop", investigation_id=inv["id"])
    assert snap["cursor"] == allof[-1]["seq"]
    assert svc.events(repo="acme-shop", investigation_id=inv["id"], after=snap["cursor"])["events"] == []


def test_a_replay_older_than_the_retained_events_asks_for_a_resync(svc):
    inv = start(svc)
    for i in range(6):
        update(svc, inv, [{"kind": "hypothesis", "id": f"h{i}", "statement": "s"}])
    finish(svc, inv)
    svc.store.sweep(keep_events=3)
    out = svc.events(repo="acme-shop", investigation_id=inv["id"], after=1)
    assert out["resync"] is True
    assert svc.events(repo="acme-shop", investigation_id=inv["id"], after=0)["resync"] is False


def test_the_briefing_is_bounded_and_carries_no_log_text(svc):
    inv = start(svc)
    for i in range(80):
        update(svc, inv, [{"kind": "hypothesis", "id": f"h{i}", "statement": "x" * 150}])
    step = begin(svc, inv)
    svc.finish_step(
        step["step"],
        state="succeeded",
        evidence=[{"kind": "search_logs", "summary": {}, "excerpts": [{"message": "LOGLINE-TEXT"}]}],
    )
    got = svc.get(repo="acme-shop", investigation_id=inv["id"], view="briefing")
    assert len(str(got)) <= briefing.BRIEFING_CHARS + 200 and "LOGLINE-TEXT" not in str(got)
    assert got["records"] and got["recent_steps"][0]["operation"] == "search_logs"


def test_recover_interrupts_a_step_whose_process_is_gone(svc, monkeypatch):
    inv = start(svc)
    step = begin(svc, inv)
    with svc.store.tx() as con:
        con.execute("UPDATE step SET pid = 2147483646 WHERE id = ?", (step["step"],))  # no such process
    assert svc.recover() == 1
    snap = svc.get(repo="acme-shop", investigation_id=inv["id"])
    assert snap["steps"][0]["state"] == "interrupted" and snap["state"] == "running"  # the agent may still be alive
    assert svc.recover() == 0


# ------------------------------------------------------------------------------------------- the store


def test_a_file_sqlite_cannot_read_is_moved_aside_not_deleted(tmp_path):
    path = tmp_path / "investigations.db"
    path.write_bytes(b"this is not a database" * 100)
    svc = Investigations(Store(path))
    assert svc.list(repo=None) == []
    (aside,) = tmp_path.glob("investigations.db.corrupt-*")
    assert aside.read_bytes().startswith(b"this is not a database")
    assert start(svc)["label"] == "INV-1"


def test_a_newer_schema_is_refused_and_left_alone(tmp_path):
    path = tmp_path / "investigations.db"
    con = sqlite3.connect(path)
    con.execute("PRAGMA user_version = 99")
    con.execute("CREATE TABLE keep (x)")
    con.commit()
    con.close()
    with pytest.raises(InvestigationError, match="newer graylog-mcp"):
        Investigations(Store(path)).list(repo=None)
    assert not list(tmp_path.glob("*.corrupt-*"))
    con = sqlite3.connect(path)
    assert con.execute("SELECT name FROM sqlite_master WHERE name = 'keep'").fetchone()


def test_a_transaction_that_fails_leaves_nothing_behind(svc):
    with pytest.raises(RuntimeError), svc.store.tx() as con:
        con.execute(
            "INSERT INTO investigation (id, num, label, title, question, instance, state, created, updated) "
            "VALUES ('x', 1, 'INV-1', 't', 'q', 'main', 'running', 0, 0)"
        )
        raise RuntimeError("the process was interrupted here")
    assert svc.list(repo=None) == []


def test_the_oldest_ended_investigation_loses_its_excerpts_first_and_keeps_its_summary(svc, clock):
    old, new = start(svc), None
    step = begin(svc, old)
    (ev_old,) = svc.finish_step(
        step["step"],
        state="succeeded",
        evidence=[{"kind": "search_logs", "summary": {"total": 5}, "excerpts": [{"message": "x" * 500}]}],
    )
    finish(svc, old)
    clock.advance(60)
    new = start(svc)
    step = begin(svc, new)
    (ev_new,) = svc.finish_step(
        step["step"],
        state="succeeded",
        evidence=[{"kind": "search_logs", "summary": {"total": 6}, "excerpts": [{"message": "y" * 500}]}],
    )
    finish(svc, new)
    svc.store.sweep(limit=700)
    gone = svc.get(repo="acme-shop", investigation_id=old["id"], view="evidence", evidence_id=ev_old)
    kept = svc.get(repo="acme-shop", investigation_id=new["id"], view="evidence", evidence_id=ev_new)
    assert gone["excerpts"] == [] and gone["excerpts_expired"] is True and gone["summary"] == {"total": 5}
    assert kept["excerpts"] and kept["excerpts_expired"] is False


def _first_open(path, barrier, _):
    barrier.wait()
    with Store(path).read() as con:
        return con.execute("SELECT count(*) FROM investigation").fetchone()[0]


def test_first_opens_of_a_new_store_by_several_processes_keep_the_file(tmp_path):
    # Each thread stands for a process with its own Store. Unless opening runs under wal_lock, two of them read
    # version 0 together; the second then tries to create the tables again, and the healthy file is moved aside as
    # unreadable. The WAL switch is refused as locked as well.
    for round_ in range(30):
        path = tmp_path / f"round-{round_}" / "investigations.db"
        barrier = threading.Barrier(8)
        with ThreadPoolExecutor(max_workers=8) as pool:
            assert list(pool.map(partial(_first_open, path, barrier), range(8))) == [0] * 8
        assert not list(path.parent.glob("*.corrupt-*"))


def test_a_second_process_cannot_move_aside_the_file_that_replaces_an_unreadable_one(tmp_path, monkeypatch):
    path = tmp_path / "investigations.db"
    found = b"not a database " * 200
    path.write_bytes(found)
    second = threading.Thread(target=_first_open, args=(path, threading.Barrier(1), 1))

    def open_second_before_the_move(*_):  # the first opener warns after it fails to read, before it moves the file
        if second.ident is None:
            second.start()
            second.join(0.5)  # it has to wait for the first; without that it would finish here

    monkeypatch.setattr(store_module.log, "warning", open_second_before_the_move)
    assert _first_open(path, threading.Barrier(1), 0) == 0
    second.join(10)
    assert not second.is_alive()
    aside = [p.read_bytes() for p in tmp_path.glob("*.corrupt-*") if not p.name.endswith(("-wal", "-shm"))]
    assert aside == [found]
