"""What one tool call leaves behind: its start and end, who called, the Graylog requests it sent, its result."""

from __future__ import annotations

import asyncio
import json
import threading
import time

import httpx
import pytest

from graylog_mcp import recording, usage
from graylog_mcp.client import GraylogClient, SpanLog, spans_recorded
from graylog_mcp.recording import hashed
from graylog_mcp.server import build_server
from graylog_mcp.setup.service import home_dir
from graylog_mcp.shared import current_repo
from graylog_mcp.tools import App
from graylog_mcp.usage_store import store
from tests.conftest import make_config
from tests.fake_graylog import FakeGraylog
from tests.test_shared import _call as http_call
from tests.test_shared import _free_port


@pytest.fixture(autouse=True)
def _fresh_conversations(monkeypatch):
    # the tests share one process, hence one fallback conversation: its call count starts again in each test
    monkeypatch.setattr(recording, "_seq", type(recording._seq)())


def _calls() -> list[dict]:
    return store().read(0, detail=True)


def _raw() -> list[dict]:
    """The lines on disk, before started and finished are joined."""
    files = sorted((home_dir() / "usage").rglob("*.jsonl"))
    return [json.loads(line) for f in files for line in f.read_text(encoding="utf-8").splitlines()]


async def test_a_call_is_written_when_it_starts_and_when_it_ends(make_app):
    app, _ = make_app("6.1.2")
    server = build_server(app)
    await server.call_tool("search_logs", {"query": "password=fake-value AND user:bob@example.com", "range": "1h"})
    await server.call_tool("search_logs", {"query": "*", "range": "1h"})
    started, finished = _raw()[:2]
    assert started["event"] == "started" and finished["event"] == "finished"
    assert started["call_id"] == finished["call_id"] and started["seq"] == 1
    first, second = _calls()
    assert first["status"] == "ok" and first["source"] == "mcp" and first["tool_name"] == "search_logs"
    assert first["conversation"] == second["conversation"] and (first["seq"], second["seq"]) == (1, 2)
    assert first["spans"] and all(s["instance"] == "main" and s["attempts"] == 1 for s in first["spans"])
    assert any("password" in s.get("query", "") for s in first["spans"])
    assert second["preview"].startswith("{") and len(second["preview"]) <= usage.PREVIEW_CHARS
    on_disk = (home_dir() / "usage").rglob("*.jsonl")
    written = "".join(p.read_text(encoding="utf-8") for p in on_disk)
    assert "fake-value" not in written and "bob@example.com" not in written  # args, spans and preview alike


async def test_what_the_client_sends_with_the_call_is_recorded(make_app):
    from mcp.client import Client

    app, _ = make_app("6.1.2")
    server = build_server(app)
    async with Client(server) as client:
        listed = await client.list_tools()
        await client.call_tool(
            "search_logs", {"query": "*", "range": "1h"}, meta={"claudecode/toolUseId": "toolu_fake1"}
        )
    assert all("ctx" not in t.input_schema.get("properties", {}) for t in listed.tools)
    (call,) = _calls()
    assert call["tool_call_id"] == hashed("toolu_fake1") and "toolu_fake1" not in json.dumps(_raw())
    assert call["client"].startswith("mcp") and call["status"] == "ok"


async def test_a_codex_session_is_the_conversation(make_app):
    from mcp.client import Client

    app, _ = make_app("6.1.2")
    meta = {"x-codex-turn-metadata": {"session_id": "fake-session", "turn_id": "fake-turn"}}
    async with Client(build_server(app)) as client:
        await client.call_tool("search_logs", {"query": "*", "range": "1h"}, meta=meta)
    (call,) = _calls()
    assert call["conversation"] == hashed("fake-session") and call["turn"] == hashed("fake-turn")


async def test_a_cancelled_call_is_recorded_as_cancelled():
    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(3600)  # Graylog that never answers
        raise AssertionError

    app = App.create(make_config(), transport=httpx.MockTransport(hang))
    task = asyncio.create_task(build_server(app).call_tool("search_logs", {"query": "*", "range": "1h"}))
    async with asyncio.timeout(5):
        while not any(c.get("status") == "running" for c in _calls()):
            await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (call,) = _calls()
    assert call["status"] == "cancelled" and call["ok"] is False and "preview" not in call
    assert any(s["status"] == "cancelled" for s in call["spans"])
    await app.close()


async def test_a_refused_call_has_its_own_status(make_app):
    app, _ = make_app("6.1.2")
    with pytest.raises(Exception, match="does not take interval"):
        await build_server(app).call_tool("aggregate", {"kind": "count", "query": "level:3", "interval": "5m"})
    (call,) = _calls()
    assert call["status"] == "refused" and not call["ok"] and call["spans"] == []


async def test_root_cause_records_one_span_per_request(make_app):
    app, _ = make_app("6.1.2", dataset="incident")
    await build_server(app).call_tool("root_cause", {"range": "1h"})
    (call,) = _calls()
    assert call["requests"] > 1 and len(call["spans"]) == call["requests"]
    assert all(s["start_ms"] >= 0 and s["ms"] >= 0 and s["wait_ms"] >= 0 for s in call["spans"])


async def test_previews_can_be_left_out(make_app, monkeypatch):
    monkeypatch.setenv("GRAYLOG_MCP_USAGE", "calls")
    app, _ = make_app("6.1.2")
    await build_server(app).call_tool("search_logs", {"query": "*", "range": "1h"})
    (call,) = _calls()
    assert "preview" not in call and call["spans"]


async def _spans_of(handler, retries: int = 1) -> tuple[list[dict], Exception | None]:
    spans = SpanLog()
    token = spans_recorded.set(spans)
    cfg = make_config(instances={"main": {"url": "https://graylog.test", "token_env": "TEST_GRAYLOG_TOKEN",
                                          "retries": retries}}).instance(None)  # fmt: skip
    client = GraylogClient(cfg, httpx.MockTransport(handler))
    error = None
    try:
        await client.get("search/universal/relative", {"query": "level:3", "range": 60})
    except Exception as exc:
        error = exc
    finally:
        spans_recorded.reset(token)
        await client.aclose()
    return spans.items, error


async def test_a_retried_request_is_one_span_with_its_attempts(monkeypatch):
    monkeypatch.setattr("graylog_mcp.client.RETRY_BASE_SECONDS", 0.001)
    answers = iter([httpx.Response(503), httpx.Response(200, json={"total_results": 0})])
    (span,), error = await _spans_of(lambda request: next(answers))
    assert error is None and span["attempts"] == 2 and span["status"] == 200
    assert span["method"] == "GET" and span["path"] == "search/universal/relative" and span["query"] == "level:3"


async def test_a_timeout_is_the_status_of_its_span():
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    (span,), error = await _spans_of(slow, retries=0)
    assert span["status"] == "timeout" and "timeout" in str(error)


def test_the_instance_is_recorded_under_its_own_name(make_app):
    app, _ = make_app("6.1.2")
    usage.track("search_logs", app, {"instance": "MAIN"}, None, None, time.monotonic(), 0)
    assert _calls()[-1]["instance"] == "main"


def test_a_windows_folder_is_recorded_by_its_name():
    token = current_repo.set("C:\\work\\acme-api")
    try:
        usage.track("search_logs", None, {}, None, None, time.monotonic(), 0)
    finally:
        current_repo.reset(token)
    assert _calls()[-1]["repo"] == "acme-api"


def test_each_http_session_is_a_conversation():
    import uvicorn

    from graylog_mcp.__main__ import build_http_app

    fake = FakeGraylog("6.1.2")
    app = build_http_app(make_config(), "127.0.0.1", "/mcp", allow_no_auth=False, transport=fake.transport)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        url = f"http://127.0.0.1:{port}/mcp"
        for _ in range(2):  # two clients, one after the other
            asyncio.run(http_call(url, {}, "search_logs", {"query": "*", "range": "1h"}))
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    first, second = _calls()
    assert first["http_session"] != second["http_session"] and first["conversation"] != second["conversation"]
    assert first["seq"] == second["seq"] == 1
