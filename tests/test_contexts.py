"""A human's monitoring selections survive the UI-to-agent handoff without drifting or leaking."""

from __future__ import annotations

import copy
import json
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import httpx
import pytest
from pydantic import ValidationError

from graylog_mcp import contexts
from graylog_mcp.admin.app import AdminState, build_app
from graylog_mcp.server import build_server
from graylog_mcp.setup.service import home_dir
from graylog_mcp.shaping import dumps
from graylog_mcp.tools import ToolInputError


def draft(**changes):
    return {
        "title": "Checkout failures",
        "question": "Find why checkout failed and suggest a fix.",
        "items": [
            {
                "kind": "log",
                "title": "Payment timeout",
                "scope": {
                    "instance": "main",
                    "query": "service:payment",
                    "streams": ["stream-1"],
                    "from_time": "2026-11-26T14:05:00+07:00",
                    "to_time": "2026-11-26T14:20:00+07:00",
                    "timezone": "Asia/Ho_Chi_Minh",
                    "ref": "graylog_1/message-1",
                },
                "captured_at": "2026-11-26T07:21:00Z",
                "data": {"message": {"ref": "graylog_1/message-1", "message": "Timeout", "trace_id": "req-a"}},
                "limits": ["One selected message, not every matching log."],
            }
        ],
        **changes,
    }


@pytest.fixture
async def page(make_app, tmp_path):
    app, fake = make_app()
    path = tmp_path / ".graylog-mcp.toml"
    path.write_text('[instances.main]\nurl="https://graylog.test"\ntoken_env="TEST_GRAYLOG_TOKEN"\n')
    state = AdminState(tmp_path, path, "tok", transport=fake.transport, allowed_hosts=("testserver",))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_app(state)),
        base_url="http://testserver",
        headers={"X-Admin-Token": "tok"},
    ) as client:
        yield client, app, fake


async def test_ui_save_to_mcp_brief_and_evidence_without_graylog_requests(page):
    client, app, fake = page
    response = await client.post("/api/contexts", json=draft())
    assert response.status_code == 201, response.text
    saved = response.json()
    assert saved["id"].startswith("CTX-") and saved["id"] in saved["prompt"]
    assert "get_context" in saved["prompt"] and "question" in saved["prompt"]
    server = build_server(app)
    brief = json.loads((await server.call_tool("get_context", {"context_id": saved["id"]})).content[0].text)
    assert brief["question"] == draft()["question"] and brief["item_count"] == 1
    assert brief["items"][0]["id"] == "I-1" and "data" not in brief["items"][0]
    item = json.loads(
        (await server.call_tool("get_context", {"context_id": saved["id"], "item_id": "I-1"})).content[0].text
    )
    assert item["scope"]["query"] == "service:payment"
    assert item["scope"]["from_time"] == "2026-11-26T14:05:00+07:00"
    assert item["scope"]["streams"] == ["stream-1"]
    assert item["data"]["message"]["trace_id"] == "req-a"
    assert fake.requests == []


async def test_saved_snapshot_is_immutable_and_recent_can_be_reopened(page):
    client, _, _ = page
    first = (await client.post("/api/contexts", json=draft())).json()
    changed = draft(question="Investigate the newer failure")
    changed["items"][0]["scope"]["query"] = "service:orders"
    second = (await client.post("/api/contexts", json=changed)).json()
    assert second["id"] != first["id"]
    reopened = (await client.get(f"/api/contexts/{first['id']}")).json()
    assert reopened == first
    rows = (await client.get("/api/contexts")).json()["contexts"]
    assert [r["id"] for r in rows] == [second["id"], first["id"]]
    assert rows[0]["item_count"] == 1 and "data" not in rows[0]


async def test_redaction_covers_notes_nested_fields_queries_and_database(page):
    client, _, _ = page
    payload = draft(title="Failure for alice@example.com", question="Investigate password=hunter2")
    payload["items"][0]["scope"]["query"] = 'message:"alice@example.com"'
    payload["items"][0]["data"] = {
        "nested": {"password": "raw-secret", "user": "alice@example.com", "message": "card 4111 1111 1111 1111"}
    }
    response = await client.post("/api/contexts", json=payload)
    assert response.status_code == 201
    with sqlite3.connect(home_dir() / "contexts.db") as con:
        stored = con.execute("SELECT body FROM context").fetchone()[0]
    for text in (response.text, stored, (await client.get("/api/contexts")).text):
        assert all(secret not in text for secret in ("alice@example.com", "hunter2", "raw-secret", "4111 1111"))
    if sys.platform != "win32":  # Windows has no owner-only mode bits
        assert (home_dir() / "contexts.db").stat().st_mode & 0o777 == 0o600


async def test_admin_auth_and_missing_records(page):
    client, _, _ = page
    for method, path in (("GET", "/api/contexts"), ("POST", "/api/contexts"), ("GET", "/api/contexts/CTX-NONE")):
        async with httpx.AsyncClient(transport=client._transport, base_url="http://testserver") as bare:
            assert (await bare.request(method, path, json=draft())).status_code == 401
    assert (await client.get("/api/contexts/CTX-NONE")).status_code == 404


@pytest.mark.parametrize(
    "change",
    [
        {"title": "   "},
        {"question": ""},
        {"items": []},
        {"items": draft()["items"] * 21},
        {"unexpected": "value"},
    ],
)
async def test_invalid_drafts_are_refused_without_writing(page, change):
    client, _, _ = page
    assert (await client.post("/api/contexts", json=draft(**change))).status_code == 400
    assert (await client.get("/api/contexts")).json()["contexts"] == []


async def test_invalid_times_json_unknown_environments_and_large_bodies(page):
    client, _, _ = page
    for start, end in (
        ("2026-11-26T14:05:00", "2026-11-26T14:20:00"),
        ("2026-11-26T14:20:00Z", "2026-11-26T14:05:00Z"),
    ):
        payload = draft()
        payload["items"][0]["scope"].update(from_time=start, to_time=end)
        assert (await client.post("/api/contexts", json=payload)).status_code == 400
    payload = draft()
    payload["items"][0]["scope"]["instance"] = "not-configured"
    assert (await client.post("/api/contexts", json=payload)).status_code == 400
    assert (
        await client.post("/api/contexts", content="{invalid", headers={"Content-Type": "application/json"})
    ).status_code == 400
    assert (await client.post("/api/contexts", json=[])).status_code == 400
    assert (
        await client.post(
            "/api/contexts", content="x" * (contexts.MAX_BODY_BYTES + 1), headers={"Content-Type": "application/json"}
        )
    ).status_code == 413


def test_same_named_environment_on_another_server_cannot_read_context(make_app):
    app, _ = make_app()
    saved = contexts.save(app, draft())
    other, _ = make_app(instances={"main": {"url": "https://other.test", "token_env": "TEST_GRAYLOG_TOKEN"}})
    with pytest.raises(ToolInputError, match="not available"):
        contexts.load(other, saved["id"])
    assert contexts.recent(other) == []


def test_context_ids_survive_redaction_and_case_normalization(make_app, monkeypatch):
    app, _ = make_app()

    class FixedId:
        hex = "091234567890abcdef"

    monkeypatch.setattr(contexts.uuid, "uuid4", FixedId)
    saved = contexts.save(app, draft())
    assert contexts.load(app, saved["id"].lower())["id"] == saved["id"]
    assert contexts.recent(app)[0]["id"] == saved["id"]


async def test_large_items_and_briefs_respect_low_output_cap_and_paginate(make_app):
    app, _ = make_app(limits={"max_output_chars": 2000})
    payload = draft(title="title " * 20, question="question " * 200)
    payload["items"] *= 20
    for item in payload["items"]:
        item["data"] = {"message": '\x01"' * 2000}
        item["scope"]["query"] = "q" * 4000
    saved = contexts.save(app, payload)
    offset, ids = 0, []
    for _ in range(21):
        brief = await contexts.get_context(app, saved["id"], offset=offset)
        assert len(dumps(brief)) <= 2000
        ids.extend(i["id"] for i in brief["items"])
        if brief["next_offset"] is None:
            break
        assert brief["next_offset"] > offset
        offset = brief["next_offset"]
    assert ids == [f"I-{i}" for i in range(1, 21)]
    evidence = await contexts.get_context(app, saved["id"], item_id="I-1")
    assert len(dumps(evidence)) <= 2000 and evidence["truncated"]
    assert evidence["scope_truncated"] and "query" not in evidence["scope"]
    assert evidence["scope"]["query_preview"]
    with pytest.raises(ToolInputError, match="no item"):
        await contexts.get_context(app, saved["id"], item_id="I-999")


def test_context_limit_and_concurrent_process_style_saves(make_app, monkeypatch):
    app, _ = make_app()
    monkeypatch.setattr(contexts, "MAX_CONTEXTS", 3)
    with ThreadPoolExecutor(max_workers=4) as pool:
        snapshots = list(pool.map(lambda _: contexts.save(app, draft()), range(8)))
    rows = contexts.recent(app)
    assert len(rows) == 3 and len({s["id"] for s in snapshots}) == 8
    for row in rows:
        assert contexts.load(app, row["id"])["items"][0]["data"]["message"]["message"] == "Timeout"


def _first_use(app, barrier, i):
    barrier.wait()
    return contexts.recent(app) if i % 2 else contexts.save(app, draft())


def test_first_saves_and_reads_of_a_new_database_do_not_collide(make_app, monkeypatch, tmp_path):
    # Each round starts with no contexts.db. Switching a new file to WAL is refused at once with "database is
    # locked" when another connection opens it at the same moment, whatever the busy timeout says; the opens must
    # be serialised across threads and processes.
    app, _ = make_app()
    for round_ in range(40):
        monkeypatch.setenv("GRAYLOG_MCP_HOME", str(tmp_path / f"home-{round_}"))
        barrier = threading.Barrier(8)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(partial(_first_use, app, barrier), range(8)))  # a worker's exception is raised here


def test_input_mutation_cannot_change_a_saved_snapshot(make_app):
    app, _ = make_app()
    payload = draft()
    original = copy.deepcopy(payload)
    saved = contexts.save(app, payload)
    assert payload == original
    payload["items"][0]["data"]["message"]["message"] = "Changed"
    assert contexts.load(app, saved["id"])["items"][0]["data"]["message"]["message"] == "Timeout"
    with pytest.raises(ValidationError):
        contexts.save(app, draft(question=""))
