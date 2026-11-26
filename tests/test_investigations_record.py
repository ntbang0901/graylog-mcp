"""Graylog tools run as steps of an investigation, through the real server and the Graylog emulator."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import textwrap
import time

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from graylog_mcp import investigations, recording
from graylog_mcp.investigations.model import LEASE_SECONDS
from graylog_mcp.operations import OPERATIONS
from graylog_mcp.recording import RecordContext, run_recorded
from graylog_mcp.server import build_server
from graylog_mcp.shared import current_repo
from graylog_mcp.tools import App
from tests.conftest import make_config
from tests.fake_graylog import FakeGraylog

REPO = "acme-shop"


@pytest.fixture(autouse=True)
def _repo():
    token = current_repo.set(REPO)
    yield
    current_repo.reset(token)


async def call(server, tool: str, args: dict) -> dict:
    return json.loads((await server.call_tool(tool, args)).content[0].text)


async def start(server, **kw) -> dict:
    args = {"action": "start", "title": "Failures", "question": "Why did checkout fail?", "range": "2h", **kw}
    return await call(server, "investigation", args)


def steps(snapshot: dict) -> list[dict]:
    return snapshot["steps"]


async def snapshot(server, inv: dict) -> dict:
    return await call(server, "get_investigation", {"investigation_id": inv["id"]})


@pytest.fixture
def server(make_app):
    app, fake = make_app("6.1.2", dataset="incident")
    srv = build_server(app)
    srv.fake = fake  # type: ignore[attr-defined]
    return srv


# ------------------------------------------------------------------------------------------- start


async def test_start_returns_the_fence_and_says_when_there_is_no_link(server):
    inv = await start(server)
    assert inv["state"] == "running" and inv["owner_fence"].startswith("1:")
    assert "ui" not in inv and "admin page" in inv["ui_unavailable"]
    assert inv["scope"]["from"].endswith("Z") and inv["scope"]["baseline_to"] == inv["scope"]["from"]
    assert server.fake.requests == [] or all(
        "search" not in str(r.url) for r in server.fake.requests
    )  # before any search


async def test_the_link_is_given_while_an_admin_page_runs_and_never_carries_the_token(server):
    from graylog_mcp import ui_links

    ui_links.record("127.0.0.1", 18742, "/admin/")
    try:
        inv = await start(server)
    finally:
        ui_links.forget()
    assert inv["ui"] == f"http://127.0.0.1:18742/admin/investigations/{inv['id']}"
    assert "token" not in inv["ui"]


# ------------------------------------------------------------------------------------------- steps


async def test_a_search_with_the_investigation_is_a_step_with_evidence(server):
    inv = await start(server)
    out = await call(
        server,
        "search_logs",
        {"query": "level:<=3", "range": "2h", "investigation_id": inv["id"], "owner_fence": inv["owner_fence"]},
    )
    snap = await snapshot(server, inv)
    (step,) = steps(snap)
    assert step["state"] == "succeeded" and step["operation"] == "search_logs" and step["check"] is None
    assert (
        step["source"] == "mcp" and step["requested"]["query"] == "level:<=3" and "owner_fence" not in step["requested"]
    )
    (ev_id,) = step["evidence"]
    ev = await call(
        server, "get_investigation", {"investigation_id": inv["id"], "view": "evidence", "evidence_id": ev_id}
    )
    assert ev["kind"] == "search_logs" and ev["exact_count"] == out["total"] and ev["returned"] == out["returned"]
    assert ev["excerpts"] and ev["step"]["instance"] == "main"


async def test_a_call_without_the_arguments_is_what_it_always_was(server):
    inv = await start(server)
    plain = await call(server, "search_logs", {"query": "level:<=3", "range": "2h"})
    again = await call(server, "search_logs", {"query": "level:<=3", "range": "2h"})
    assert {**plain, "range": None} == {**again, "range": None}
    assert steps(await snapshot(server, inv)) == []


async def test_the_step_is_visible_while_the_query_runs(make_app):
    gate = asyncio.Event()
    fake = FakeGraylog("6.1.2", dataset="incident")

    async def slow(request: httpx.Request) -> httpx.Response:
        if "/api/search/" in request.url.path:
            await gate.wait()
        return fake.handle(request)

    app = App.create(make_config(), transport=httpx.MockTransport(slow))
    server = build_server(app)
    inv = await start(server)
    task = asyncio.create_task(
        call(
            server,
            "search_logs",
            {"query": "*", "range": "1h", "investigation_id": inv["id"], "owner_fence": inv["owner_fence"]},
        )
    )
    async with asyncio.timeout(5):
        while True:
            snap = await snapshot(server, inv)
            if steps(snap):
                break
            await asyncio.sleep(0.01)
    assert steps(snap)[0]["state"] == "running" and steps(snap)[0]["ended"] is None
    events = investigations.service().events(repo=REPO, investigation_id=inv["id"])["events"]
    assert events[-1]["type"] == "step_started"
    gate.set()
    await task
    assert steps(await snapshot(server, inv))[0]["state"] == "succeeded"
    await app.close()


async def test_cancelling_a_call_mid_query_leaves_a_cancelled_step(make_app):
    async def hang(request: httpx.Request) -> httpx.Response:
        if "/api/search/" in request.url.path:
            await asyncio.sleep(3600)
        return FakeGraylog("6.1.2", dataset="incident").handle(request)

    app = App.create(make_config(), transport=httpx.MockTransport(hang))
    server = build_server(app)
    inv = await start(server)
    task = asyncio.create_task(
        call(
            server,
            "search_logs",
            {"query": "*", "range": "1h", "investigation_id": inv["id"], "owner_fence": inv["owner_fence"]},
        )
    )
    async with asyncio.timeout(5):
        while not steps(await snapshot(server, inv)):
            await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (step,) = steps(await snapshot(server, inv))
    assert step["state"] == "cancelled" and step["evidence"] == [] and step["ended"]
    await app.close()


async def test_investigation_cancel_stops_the_steps_this_process_runs(make_app):
    async def hang(request: httpx.Request) -> httpx.Response:
        if "/api/search/" in request.url.path:
            await asyncio.sleep(3600)
        return FakeGraylog("6.1.2", dataset="incident").handle(request)

    app = App.create(make_config(), transport=httpx.MockTransport(hang))
    server = build_server(app)
    inv = await start(server)
    task = asyncio.create_task(
        call(
            server,
            "search_logs",
            {"query": "*", "range": "1h", "investigation_id": inv["id"], "owner_fence": inv["owner_fence"]},
        )
    )
    async with asyncio.timeout(5):
        while not steps(await snapshot(server, inv)):
            await asyncio.sleep(0.01)
    out = await call(
        server, "investigation", {"action": "cancel", "investigation_id": inv["id"], "owner_fence": inv["owner_fence"]}
    )
    assert out["state"] == "cancelled" and out["running_steps"] == []
    with pytest.raises(asyncio.CancelledError):
        await task
    assert steps(await snapshot(server, inv))[0]["state"] == "cancelled"
    await app.close()


async def test_a_failure_and_an_empty_result_are_different_things(server):
    inv = await start(server)
    ids = {"investigation_id": inv["id"], "owner_fence": inv["owner_fence"]}
    empty = await call(server, "search_logs", {"query": "nosuchfield:nothing", "range": "1h", **ids})
    assert empty["returned"] == 0
    # Graylog refuses every way of searching: the search did not happen
    server.fake.forbidden = {"search/universal/absolute", "views/search/sync", "search/messages"}
    with pytest.raises(ToolError):
        await call(server, "search_logs", {"query": "*", "range": "1h", **ids})
    snap = await snapshot(server, inv)
    found, failed = steps(snap)
    assert found["state"] == "succeeded" and len(found["evidence"]) == 1
    ev = await call(
        server,
        "get_investigation",
        {"investigation_id": inv["id"], "view": "evidence", "evidence_id": found["evidence"][0]},
    )
    assert ev["exact_count"] in (0, None) and ev["coverage"], "an empty result says what it covers"
    assert ev["summary"].get("query") == "nosuchfield:nothing"  # the query as asked is kept next to what ran
    assert failed["state"] == "failed" and failed["evidence"] == [] and failed["error"]


async def test_a_call_refused_for_its_arguments_never_ran_and_is_no_step(server):
    inv = await start(server)
    ids = {"investigation_id": inv["id"], "owner_fence": inv["owner_fence"]}
    with pytest.raises(ToolError, match="needs group_by"):
        await call(server, "aggregate", {"kind": "top", "range": "1h", **ids})
    assert steps(await snapshot(server, inv)) == []


async def test_root_cause_and_scan_are_one_step_each(server):
    inv = await start(server)
    ids = {"investigation_id": inv["id"], "owner_fence": inv["owner_fence"]}
    await call(server, "root_cause", {"range": "2h", **ids})
    await call(server, "scan", {"range": "2h", **ids})
    snap = await snapshot(server, inv)
    assert [s["operation"] for s in steps(snap)] == ["root_cause", "scan"]
    assert all(len(s["evidence"]) == 1 for s in steps(snap))


async def test_a_check_is_named_by_the_agent_and_an_unknown_one_stops_the_call(server):
    inv = await start(server)
    ids = {"investigation_id": inv["id"], "owner_fence": inv["owner_fence"]}
    before = len(server.fake.requests)
    with pytest.raises(ToolError, match="check 'c1' does not exist"):
        await call(server, "search_logs", {"query": "*", "range": "1h", "check_id": "c1", **ids})
    assert len(server.fake.requests) == before, "the refused call never reached Graylog"
    await call(
        server,
        "investigation",
        {"action": "update", **ids, "items": [{"kind": "check", "id": "c1", "purpose": "see the failures"}]},
    )
    await call(server, "search_logs", {"query": "*", "range": "1h", "check_id": "c1", **ids})
    assert steps(await snapshot(server, inv))[0]["check"] == "c1"


# ------------------------------------------------------------------------------------------- who may record


async def test_only_the_owner_of_a_repository_s_investigation_can_record(server):
    inv = await start(server)
    args = {"query": "*", "range": "1h", "investigation_id": inv["id"], "owner_fence": inv["owner_fence"]}
    before = len(server.fake.requests)
    with pytest.raises(ToolError, match="owner_fence"):
        await call(server, "search_logs", {**args, "owner_fence": "1:guessed"})
    token = current_repo.set("other-repo")
    try:
        with pytest.raises(ToolError, match="no investigation"):
            await call(server, "search_logs", args)
        with pytest.raises(ToolError, match="no investigation"):
            await call(server, "get_investigation", {"investigation_id": inv["id"]})
    finally:
        current_repo.reset(token)
    assert len(server.fake.requests) == before
    assert steps(await snapshot(server, inv)) == []


async def test_a_call_for_another_instance_is_refused(make_app):
    app, _ = make_app("6.1.2", instances={"second": {"url": "https://second.test", "token_env": "TEST_GRAYLOG_TOKEN"}})
    server = build_server(app)
    inv = await start(server, instance="main")
    with pytest.raises(ToolError, match="about instance 'main'"):
        await call(
            server,
            "search_logs",
            {
                "query": "*",
                "range": "1h",
                "instance": "second",
                "investigation_id": inv["id"],
                "owner_fence": inv["owner_fence"],
            },
        )


async def test_the_playground_can_record_through_the_same_path(make_app):
    """Not wired to the page yet (steering is a later step); the choke point already supports it."""
    app, _ = make_app("6.1.2", dataset="incident")
    server = build_server(app)
    inv = await start(server)
    ctx = RecordContext(source="playground", investigation_id=inv["id"], owner_fence=inv["owner_fence"])
    await run_recorded(OPERATIONS["search_logs"], lambda: app, ctx, {"query": "*", "range": "1h"})
    (step,) = steps(await snapshot(server, inv))
    assert step["source"] == "playground" and step["state"] == "succeeded"


async def test_a_late_result_after_a_takeover_belongs_to_the_old_attempt(make_app):
    gate = asyncio.Event()
    fake = FakeGraylog("6.1.2", dataset="incident")

    async def slow(request: httpx.Request) -> httpx.Response:
        if "/api/search/" in request.url.path:
            await gate.wait()
        return fake.handle(request)

    app = App.create(make_config(), transport=httpx.MockTransport(slow))
    server = build_server(app)
    inv = await start(server)
    task = asyncio.create_task(
        call(
            server,
            "search_logs",
            {"query": "*", "range": "1h", "investigation_id": inv["id"], "owner_fence": inv["owner_fence"]},
        )
    )
    async with asyncio.timeout(5):
        while not steps(await snapshot(server, inv)):
            await asyncio.sleep(0.01)
    new = await call(server, "investigation", {"action": "resume", "investigation_id": inv["id"], "takeover": True})
    await call(
        server,
        "investigation",
        {
            "action": "update",
            "investigation_id": inv["id"],
            "owner_fence": new["owner_fence"],
            "items": [{"kind": "hypothesis", "statement": "h"}],
        },
    )
    gate.set()
    await task
    snap = await snapshot(server, inv)
    (late,) = steps(snap)
    assert late["fenced_out"] is True and late["state"] == "succeeded" and late["evidence"]
    assert late["attempt"] != snap["attempts"][-1]["id"] and len(snap["items"]) == 1 and snap["state"] == "running"
    await app.close()


# ------------------------------------------------------------------------------------------- what is stored


async def test_nothing_unredacted_reaches_the_database_the_usage_files_or_the_answer(make_app):
    from graylog_mcp.setup.service import home_dir

    app, fake = make_app("6.1.2", dataset="incident")
    secret = "fake-secret-value-9981"
    fake.messages[0]["message"] = f"login failed password={secret} for bob@example.com"
    server = build_server(app)
    inv = await start(server, question=f"Who sent password={secret}?")
    ids = {"investigation_id": inv["id"], "owner_fence": inv["owner_fence"]}
    out = await call(server, "search_logs", {"query": f"password:{secret}", "range": "6h", **ids})
    await call(server, "investigation", {"action": "checkpoint", **ids, "briefing": f"saw password={secret}"})
    snap = await snapshot(server, inv)
    raw = b"".join(p.read_bytes() for p in investigations.path().parent.glob("investigations.db*"))
    usage_files = b"".join(p.read_bytes() for p in (home_dir() / "usage").rglob("*") if p.is_file())
    del out  # the tool's own answer echoes the query it was asked, as it always did
    for text in (raw, usage_files, json.dumps(snap).encode()):
        assert secret.encode() not in text and b"bob@example.com" not in text
    owner_token = inv["owner_fence"].split(":", 1)[1].encode()
    assert owner_token not in usage_files, "the fence is a credential: it stays out of the usage previews"


async def test_the_owner_fence_is_not_written_to_the_usage_files(server):
    from graylog_mcp.setup.service import home_dir

    inv = await start(server)
    await call(
        server, "investigation", {"action": "resume", "investigation_id": inv["id"], "owner_fence": inv["owner_fence"]}
    )
    written = "".join(p.read_text(encoding="utf-8") for p in (home_dir() / "usage").rglob("*.jsonl"))
    assert "owner_fence" in written and inv["owner_fence"].split(":", 1)[1] not in written


# ------------------------------------------------------------------------------------------- recovery and load


async def test_a_step_left_running_by_a_crash_is_interrupted_on_the_next_start(make_app, monkeypatch):
    app, _ = make_app("6.1.2")
    server = build_server(app)
    inv = await start(server)
    svc = investigations.service()
    step = svc.begin_step(
        repo=REPO,
        investigation_id=inv["id"],
        owner_fence=inv["owner_fence"],
        check_id=None,
        operation="search_logs",
        tool="search_logs",
        source="mcp",
        requested={},
        instance="main",
        call_id=None,
    )
    with svc.store.tx() as con:
        con.execute("UPDATE step SET pid = 2147483646 WHERE id = ?", (step["step"],))  # that process is gone
    await investigations.recover()
    (got,) = steps(await snapshot(server, inv))
    assert got["state"] == "interrupted" and "process ended" in got["error"]


async def test_the_loop_stays_responsive_while_many_steps_commit(make_app):
    app, _ = make_app("6.1.2", dataset="incident")
    server = build_server(app)
    inv = await start(server)
    ids = {"investigation_id": inv["id"], "owner_fence": inv["owner_fence"]}
    worst, stop = 0.0, False

    async def heartbeat() -> None:
        nonlocal worst
        last = time.perf_counter()
        while not stop:
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            worst, last = max(worst, now - last), now

    beat = asyncio.create_task(heartbeat())
    await asyncio.gather(*(call(server, "search_logs", {"query": "*", "range": "1h", **ids}) for _ in range(50)))
    stop = True
    await beat
    assert len(steps(await snapshot(server, inv))) == 50
    assert worst < 1.0, f"the event loop was blocked for {worst:.2f} s"


def test_two_server_processes_write_one_investigation_without_losing_or_repeating_events(tmp_path):
    from graylog_mcp.investigations.model import Scope
    from graylog_mcp.investigations.service import Investigations
    from graylog_mcp.investigations.store import Store

    path = tmp_path / "shared.db"
    svc = Investigations(Store(path))
    scope = Scope(
        "2026-11-18T03:00:00.000Z",
        "2026-11-18T04:00:00.000Z",
        "2026-11-18T02:00:00.000Z",
        "2026-11-18T03:00:00.000Z",
        "UTC",
    )
    inv = svc.start(repo=REPO, instance="main", title="t", question="q", scope=scope, redact=lambda v: v)
    script = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        from graylog_mcp.investigations.service import Investigations
        from graylog_mcp.investigations.store import Store
        svc = Investigations(Store(Path(sys.argv[1])))
        for i in range(25):
            svc.update(repo=sys.argv[2], investigation_id=sys.argv[3], owner_fence=sys.argv[4],
                       items=[{"kind": "hypothesis", "id": f"{sys.argv[5]}{i}", "statement": "s"}],
                       scope=None, reason=None, expected_revision=None, redact=lambda v: v)
        """
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-I", "-c", script, str(path), REPO, inv["id"], inv["owner_fence"], tag],
            stderr=subprocess.PIPE,
        )
        for tag in ("a", "b")
    ]
    for proc in procs:
        assert proc.wait(timeout=60) == 0, proc.stderr.read().decode() if proc.stderr else ""
    events = svc.events(repo=REPO, investigation_id=inv["id"], limit=1000)["events"]
    assert [e["seq"] for e in events] == list(range(1, 52))  # started + 50 saved, no gap, no repeat
    assert len(svc.get(repo=REPO, investigation_id=inv["id"])["items"]) == 50


def test_lease_constant_is_ten_minutes():
    assert LEASE_SECONDS == 600 and recording.RecordContext().owner_fence is None


# ------------------------------------------------------------------------------------------- every recorded tool


@pytest.mark.parametrize(
    ("tool", "args", "operation"),
    [
        ("search_logs", {"query": "*", "range": "1h"}, "search_logs"),
        ("aggregate", {"kind": "count", "range": "1h"}, "count_logs"),
        ("aggregate", {"kind": "top", "group_by": "source", "range": "1h"}, "top_values"),
        ("aggregate", {"kind": "histogram", "range": "1h"}, "log_histogram"),
        ("aggregate", {"kind": "errors", "range": "1h"}, "error_summary"),
        ("compare_periods", {"window": "1h"}, "compare_periods"),
        ("trace_request", {"trace_id": "fake-trace", "range": "1h"}, "trace_request"),
        ("scan", {"range": "1h"}, "scan"),
        ("root_cause", {"range": "1h"}, "root_cause"),
        ("root_cause", {"view": "changes", "range": "1h"}, "detect_changes"),
        ("root_cause", {"view": "service_map", "range": "1h"}, "service_map"),
    ],
)
async def test_every_recorded_tool_takes_the_arguments_and_leaves_one_step(server, tool, args, operation):
    inv = await start(server)
    await call(server, tool, {**args, "investigation_id": inv["id"], "owner_fence": inv["owner_fence"]})
    (step,) = steps(await snapshot(server, inv))
    assert step["operation"] == operation and step["tool"] == tool and step["state"] == "succeeded"
    assert len(step["evidence"]) == 1


async def test_get_message_and_run_preset_are_steps_too(make_app):
    app, _ = make_app(
        "6.1.2",
        dataset="incident",
        presets={"errors-now": {"tool": "error_summary", "args": {"range": "1h"}, "description": "errors"}},
    )
    server = build_server(app)
    inv = await start(server)
    ids = {"investigation_id": inv["id"], "owner_fence": inv["owner_fence"]}
    found = await call(server, "search_logs", {"query": "*", "range": "2h", "limit": 1, **ids})
    await call(server, "get_message", {"ref": found["messages"][0]["ref"], **ids})
    await call(server, "run_preset", {"name": "errors-now", **ids})
    snap = await snapshot(server, inv)
    assert [s["operation"] for s in steps(snap)] == ["search_logs", "get_message", "run_preset"]
    preset_evidence = await call(
        server,
        "get_investigation",
        {"investigation_id": inv["id"], "view": "evidence", "evidence_id": steps(snap)[2]["evidence"][0]},
    )
    assert preset_evidence["kind"] == "error_summary"  # typed by what the preset ran


async def test_a_call_that_names_no_instance_runs_on_the_investigation_s(make_app):
    app, fake = make_app(
        "6.1.2", instances={"second": {"url": "https://second.test", "token_env": "TEST_GRAYLOG_TOKEN"}}
    )
    server = build_server(app)
    inv = await start(server, instance="second")
    await call(
        server,
        "search_logs",
        {"query": "*", "range": "1h", "investigation_id": inv["id"], "owner_fence": inv["owner_fence"]},
    )
    (step,) = steps(await snapshot(server, inv))
    assert step["executed"]["instance"] == "second"
    assert fake.requests[-1].url.host == "second.test"
