"""Usage recording: what the model asked, for the admin page's activity charts."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from graylog_mcp import usage
from tests.fake_graylog import FakeGraylog


def _lines() -> list[dict]:
    return [json.loads(line) for line in usage.path().read_text(encoding="utf-8").splitlines()]


async def test_tool_calls_are_recorded_with_redacted_queries(make_app):
    from graylog_mcp.server import build_server

    app, _ = make_app("6.1.2")
    server = build_server(app)
    await server.call_tool("search_logs", {"query": "*", "range": "1h"})
    await server.call_tool(
        "search_logs", {"query": 'message:"nothing-like-this" AND user:bob@example.com', "range": "1h"}
    )
    with pytest.raises(Exception, match=r"unknown|instance"):
        await server.call_tool("count_logs", {"instance": "nope"})
    found, empty, failed = _lines()
    assert found["tool"] == "search_logs" and found["ok"] and found["results"] > 0 and found["instance"] == "main"
    assert found["chars"] > 0 and found["ms"] >= 0
    assert empty["results"] == 0 and usage.is_miss(empty)
    assert "bob@example.com" not in empty["args"]["query"] and "nothing-like-this" in empty["args"]["query"]
    assert not failed["ok"] and failed["error"] and failed["instance"] == "nope" and not usage.is_miss(failed)
    usage.track("search_logs", app, {}, None, "bad query near alice@example.com", time.monotonic(), 0)
    assert "alice@example.com" not in _lines()[-1]["error"]  # an error quoting the query is redacted too


def test_summary_buckets_tools_misses_and_previous_period():
    now = time.time()
    base = {"ok": True, "ms": 100, "chars": 10, "instance": "main"}
    for e in [
        {"tool": "search_logs", "results": 0, "args": {"query": "OrderNotFound"}, "ts": now - 60},
        {"tool": "search_logs", "results": 5, "ts": now - 120, "ms": 300},
        {"tool": "root_cause", "ts": now - 3 * 3600},
        {"tool": "count_logs", "ok": False, "error": "boom", "ts": now - 5 * 3600},
        {"tool": "search_logs", "results": 2, "ts": now - 30 * 3600},  # the period before
    ]:
        usage.write({**base, **e})
    s = usage.summarize(24, 1, now=now)
    assert len(s["buckets"]) == 24 and sum(b["calls"] for b in s["buckets"]) == 4
    assert s["buckets"][-1]["calls"] == 2 and s["buckets"][-1]["misses"] == 1
    assert s["totals"] == {"calls": 4, "errors": 1, "searches": 2, "misses": 1, "hit_rate": 50}
    assert s["previous"]["calls"] == 1 and s["previous"]["hit_rate"] == 100
    search = next(t for t in s["tools"] if t["tool"] == "search_logs")
    assert search == {"tool": "search_logs", "calls": 2, "errors": 0, "misses": 1, "p50_ms": 200, "p95_ms": 300}
    assert s["tools"][0]["tool"] == "search_logs"  # most used first
    assert [m["args"]["query"] for m in s["misses"]] == ["OrderNotFound"]
    assert s["recent"][0]["ts"] > s["recent"][-1]["ts"]  # newest first
    assert len(usage.summarize(168, 6, now=now)["buckets"]) == 28


def test_file_is_trimmed_and_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(usage, "MAX_BYTES", 2_000)
    monkeypatch.setattr(usage, "KEEP_LINES", 5)
    for i in range(40):
        usage.write({"ts": time.time(), "tool": "search_logs", "n": i, "ok": True})
    kept = _lines()
    assert len(kept) <= 30 and kept[-1]["n"] == 39
    monkeypatch.setenv("GRAYLOG_MCP_USAGE", "off")
    before = len(_lines())
    usage.track("search_logs", None, {"query": "x"}, {"total": 1}, None, time.monotonic(), 1)
    assert len(_lines()) == before and usage.summarize()["enabled"] is False


def test_results_of():
    assert usage.results_of({"total": 3, "messages": [1]}) == 3
    assert usage.results_of({"total": None, "messages": [1, 2]}) == 2
    assert usage.results_of({"count": 0}) == 0
    assert usage.results_of({"timeline": []}) == 0
    assert usage.results_of("text") is None and usage.results_of({"other": 1}) is None


@pytest.fixture
def admin_client(tmp_path):
    from graylog_mcp.admin.app import AdminState, build_app

    fake = FakeGraylog("6.1.2", dataset="incident")
    cfg = tmp_path / ".graylog-mcp.toml"
    cfg.write_text(
        '[instances.main]\nurl = "https://graylog.test"\ntoken_env = "TEST_GRAYLOG_TOKEN"\n'
        '[instances.down]\nurl = "https://down.test"\ntoken_env = "TEST_GRAYLOG_TOKEN"\n',
        encoding="utf-8",
    )

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.host == "down.test":
            raise httpx.ConnectError("connection refused", request=request)
        return fake.transport.handle_request(request)

    state = AdminState(tmp_path, cfg, "tok", transport=httpx.MockTransport(transport), allowed_hosts=("testserver",))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(state)), base_url="http://testserver",
                             headers={"X-Admin-Token": "tok"})  # fmt: skip


async def test_admin_usage_and_log_stats(admin_client):
    usage.write({"ts": time.time(), "tool": "search_logs", "ok": True, "results": 0, "ms": 5})
    body = (await admin_client.get("/api/usage")).json()
    assert body["totals"]["misses"] == 1 and len(body["buckets"]) == 24
    assert len((await admin_client.get("/api/usage?range=7d")).json()["buckets"]) == 28
    stats = (await admin_client.get("/api/logstats")).json()
    envs = {e["name"]: e for e in stats["environments"]}
    main = envs["main"]
    assert main["total"] > 0 and main["errors"] > 0 and main["total"] >= main["errors"]
    assert len(main["buckets"]) >= 24 and len(main["error_buckets"]) == len(main["buckets"])
    assert "error" in envs["down"] and "total" not in envs["down"]  # one unreachable environment, others still shown
    assert (await admin_client.get("/api/usage", headers={"X-Admin-Token": "bad"})).status_code == 401
