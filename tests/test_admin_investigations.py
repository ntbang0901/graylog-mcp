"""The admin page's view of investigations: list, snapshot, evidence and the live event feed."""

from __future__ import annotations

import asyncio
import json
import re

import httpx
import pytest

from graylog_mcp import investigations
from graylog_mcp.admin.app import AdminState, build_app
from graylog_mcp.investigations import stream
from graylog_mcp.investigations.model import ANY_REPO
from graylog_mcp.server import build_server
from graylog_mcp.shared import current_repo

REPO = "acme-shop"


@pytest.fixture(autouse=True)
def _repo():
    token = current_repo.set(REPO)
    yield
    current_repo.reset(token)


async def call(server, tool: str, args: dict) -> dict:
    return json.loads((await server.call_tool(tool, args)).content[0].text)


@pytest.fixture
async def world(make_app, tmp_path):
    """An agent's server with one investigation that ran two steps, and the admin page's client over the same db."""
    app, _ = make_app("6.1.2", dataset="incident")
    server = build_server(app)
    inv = await call(
        server,
        "investigation",
        {"action": "start", "title": "Failures", "question": "Why did checkout fail?", "range": "2h"},
    )
    ids = {"investigation_id": inv["id"], "owner_fence": inv["owner_fence"]}
    await call(server, "search_logs", {"query": "*", "range": "2h", "limit": 2, **ids})
    await call(server, "aggregate", {"kind": "count", "range": "2h", **ids})
    state = AdminState(tmp_path, None, "tok", allowed_hosts=("testserver",))
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_app(state)),
        base_url="http://testserver",
        headers={"X-Admin-Token": "tok"},
    )
    async with client:
        yield server, inv, ids, client


def events_of(text: str) -> list[dict]:
    out = []
    for block in text.split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line and not line.startswith(":"))
        if lines.get("event") == "event":
            out.append(json.loads(lines["data"]))
    return out


async def test_the_list_shows_the_investigation_with_its_repository(world):
    _, inv, _, client = world
    got = (await client.get("/api/investigations")).json()
    assert got["enabled"] is True
    (row,) = got["investigations"]
    assert (row["id"], row["state"], row["repo"], row["instance"]) == (inv["id"], "running", REPO, row["instance"])
    assert (await client.get("/api/investigations", params={"state": "completed"})).json()["investigations"] == []
    assert (await client.get("/api/investigations", params={"instance": "nowhere"})).json()["investigations"] == []


async def test_the_page_reads_investigations_of_every_repository(world):
    server, _, _, client = world
    token = current_repo.set("acme-other")
    try:
        other = await call(
            server, "investigation", {"action": "start", "title": "Other", "question": "Why?", "range": "1h"}
        )
    finally:
        current_repo.reset(token)
    ids = {r["id"] for r in (await client.get("/api/investigations")).json()["investigations"]}
    assert other["id"] in ids and len(ids) == 2
    assert (await client.get(f"/api/investigations/{other['id']}")).status_code == 200


async def test_the_snapshot_carries_steps_evidence_and_a_cursor_and_never_the_fence(world):
    _, inv, ids, client = world
    snap = (await client.get(f"/api/investigations/{inv['id']}")).json()
    assert [s["operation"] for s in snap["steps"]] == ["search_logs", "count_logs"]
    assert len(snap["evidence"]) == 2 and snap["cursor"] > 0
    assert ids["owner_fence"] not in json.dumps(snap)


async def test_one_piece_of_evidence_in_full_and_a_missing_one_is_404(world):
    _, inv, _, client = world
    snap = (await client.get(f"/api/investigations/{inv['id']}")).json()
    eid = snap["evidence"][0]["id"]
    found = (await client.get(f"/api/investigations/{inv['id']}/evidence/{eid}")).json()
    assert found["kind"] == "search_logs" and found["step"]["operation"] == "search_logs"
    assert (await client.get(f"/api/investigations/{inv['id']}/evidence/nope")).status_code == 404
    assert (await client.get("/api/investigations/INV-999")).status_code == 404
    assert (await client.get("/api/investigations/INV-999/events")).status_code == 404


async def test_the_api_needs_the_admin_token(world):
    _, inv, _, client = world
    bare = httpx.AsyncClient(transport=client._transport, base_url="http://testserver")  # no X-Admin-Token
    async with bare:
        for path in (
            "/api/investigations",
            f"/api/investigations/{inv['id']}",
            f"/api/investigations/{inv['id']}/events",
        ):
            assert (await bare.get(path)).status_code == 401


async def test_events_replay_from_a_sequence_without_repeats(world):
    _, inv, _, client = world
    everything = events_of((await client.get(f"/api/investigations/{inv['id']}/events", params={"follow": "0"})).text)
    seqs = [e["seq"] for e in everything]
    assert seqs == sorted(set(seqs)) and {"step_started", "step_finished"} <= {e["type"] for e in everything}
    mid = seqs[len(seqs) // 2]
    rest = events_of(
        (await client.get(f"/api/investigations/{inv['id']}/events", params={"follow": "0", "after": mid})).text
    )
    assert [e["seq"] for e in rest] == [s for s in seqs if s > mid]
    via_header = events_of(
        (
            await client.get(
                f"/api/investigations/{inv['id']}/events", params={"follow": "0"}, headers={"Last-Event-ID": str(mid)}
            )
        ).text
    )
    assert [e["seq"] for e in via_header] == [e["seq"] for e in rest]
    assert (await client.get(f"/api/investigations/{inv['id']}/events", params={"after": "x"})).status_code == 400


async def test_reconnecting_after_parallel_steps_finish_out_of_order_loses_and_repeats_nothing(world):
    server, inv, ids, client = world
    first = events_of((await client.get(f"/api/investigations/{inv['id']}/events", params={"follow": "0"})).text)
    seen = first[-1]["seq"]
    await asyncio.gather(
        *[call(server, "aggregate", {"kind": "count", "range": "2h", "query": f"level:{n}", **ids}) for n in range(5)]
    )
    more = events_of(
        (await client.get(f"/api/investigations/{inv['id']}/events", params={"follow": "0", "after": seen})).text
    )
    assert [e["seq"] for e in more] == list(range(seen + 1, seen + 1 + len(more)))  # no gap, no repeat
    finished = [e for e in more if e["type"] == "step_finished"]
    assert len(finished) == 5 and len({e["ref"][1] for e in finished}) == 5


async def test_an_older_position_than_the_events_kept_asks_for_a_resync(world):
    _, inv, _, _ = world
    svc = investigations.service()
    svc.store.sweep(keep_events=0)  # nothing is kept for an ended one, so end it first
    got = [f async for f in stream.feed(inv["id"], after=0, follow=False)]
    assert got and got[-1].startswith("event: end")  # a running investigation keeps all its events
    await call(
        world[0],
        "investigation",
        {"action": "cancel", "investigation_id": inv["id"], "owner_fence": world[2]["owner_fence"]},
    )
    svc.store.sweep(keep_events=1)
    late = [f async for f in stream.feed(inv["id"], after=1, follow=False)]
    assert late == ['event: resync\ndata: {"after":1}\n\n']


async def test_the_live_feed_delivers_a_new_step_and_a_keep_alive_while_quiet(world):
    server, inv, ids, _ = world
    cursor = (await investigations.run(investigations.service().get, repo=ANY_REPO, investigation_id=inv["id"]))[
        "cursor"
    ]
    frames: list[str] = []

    async def read():
        async for f in stream.feed(inv["id"], cursor, poll=0.02, heartbeat=0.05):
            frames.append(f)

    task = asyncio.create_task(read())
    await asyncio.sleep(0.15)
    assert any(f.startswith(": keep-alive") for f in frames) and not any("event: event" in f for f in frames)
    await call(server, "aggregate", {"kind": "count", "range": "2h", "query": "level:3", **ids})
    for _ in range(100):
        if any("step_finished" in f for f in frames):
            break
        await asyncio.sleep(0.02)
    task.cancel()
    assert any("step_started" in f for f in frames) and any("step_finished" in f for f in frames)


async def test_the_feed_stops_when_the_client_has_gone(world):
    _, inv, _, _ = world
    cursor = (await investigations.run(investigations.service().get, repo=ANY_REPO, investigation_id=inv["id"]))[
        "cursor"
    ]

    async def gone() -> bool:
        return True

    assert [f async for f in stream.feed(inv["id"], cursor, gone=gone, poll=0.01)] == []


async def test_nothing_unredacted_reaches_the_page(make_app, tmp_path):
    secret = "fake-secret-value"
    app, _ = make_app("6.1.2", dataset="incident")
    server = build_server(app)
    inv = await call(
        server, "investigation", {"action": "start", "title": "T", "question": f"password={secret}?", "range": "1h"}
    )
    state = AdminState(tmp_path, None, "tok", allowed_hosts=("testserver",))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_app(state)),
        base_url="http://testserver",
        headers={"X-Admin-Token": "tok"},
    ) as client:
        texts = [
            (await client.get("/api/investigations")).text,
            (await client.get(f"/api/investigations/{inv['id']}")).text,
            (await client.get(f"/api/investigations/{inv['id']}/events", params={"follow": "0"})).text,
        ]
    assert all(secret not in t for t in texts)


async def test_with_investigations_off_the_page_gets_an_empty_answer(world, monkeypatch):
    _, inv, _, client = world
    monkeypatch.setenv("GRAYLOG_MCP_INVESTIGATIONS", "off")
    got = (await client.get("/api/investigations")).json()
    assert got == {"enabled": False, "source": "env", "investigations": []}
    assert (await client.get(f"/api/investigations/{inv['id']}")).json()["enabled"] is False


async def test_the_export_is_markdown_for_download(world):
    _, inv, _, client = world
    got = await client.get(f"/api/investigations/{inv['id']}/export")
    assert got.status_code == 200 and got.headers["content-type"].startswith("text/markdown")
    assert f"{inv['id']}.md" in got.headers["content-disposition"]
    assert got.text.startswith("# Failures (INV-1)") and "search_logs" in got.text
    assert (await client.get("/api/investigations/INV-999/export")).status_code == 404


async def test_a_traced_request_keeps_the_per_service_series_the_page_draws(world):
    server, inv, ids, client = world
    await call(server, "trace_request", {"trace_id": "co-100", "range": "6h", **ids})
    snap = (await client.get(f"/api/investigations/{inv['id']}")).json()
    row = next(e for e in snap["evidence"] if e["kind"] == "trace_request")
    found = (await client.get(f"/api/investigations/{inv['id']}/evidence/{row['id']}")).json()
    (trace,) = [c for c in found["summary"]["charts"] if c["type"] == "trace"]
    names = [s["service"] for s in trace["services"]]
    assert names[0] == "gateway" and {"payment", "bank-adapter"} <= set(names)
    assert [s["first_ms"] for s in trace["services"]] == sorted(s["first_ms"] for s in trace["services"])
    assert trace["duration_ms"] == max(s["last_ms"] for s in trace["services"])
    assert all(o["level"] in ("info", "error") for s in trace["services"] for o in s["observations"])
    assert "t" in found["excerpts"][0]  # the offset the page shows beside each line


async def test_step_times_keep_milliseconds_so_calls_inside_one_second_stay_in_order(world):
    server, inv, ids, client = world
    for _ in range(3):
        await call(server, "aggregate", {"kind": "count", "range": "2h", **ids})
    snap = (await client.get(f"/api/investigations/{inv['id']}")).json()
    stamp = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
    steps = snap["steps"]
    assert len(steps) == 5 and all(stamp.match(s["started"]) and stamp.match(s["ended"]) for s in steps)
    starts = [s["started"] for s in steps]
    assert starts == sorted(starts) and len(set(starts)) == len(starts)  # no two began at the same instant
    assert stamp.match(snap["created"]) and stamp.match(snap["updated"])
