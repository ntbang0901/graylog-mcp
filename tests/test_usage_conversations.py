"""The admin page's view of recorded calls: the dashboard numbers, and calls by conversation and turn."""

from __future__ import annotations

import mimetypes
import re
import time

import httpx
import pytest

from graylog_mcp import usage
from graylog_mcp import usage_conversations as conversations
from graylog_mcp.usage_store import store

A, B = "aaaaaaaaaaaa", "bbbbbbbbbbbb"
# the last half past the hour: a call minutes before stays in its hour's bucket, and it is never in the future, which
# the admin page (reading up to the real time) would not see
NOW = (time.time() - 1800) // 3600 * 3600 + 1800


def _call(ts: float, conversation: str = A, **extra) -> dict:
    entry = {"ts": ts, "conversation": conversation, "tool": "search_logs", "ok": True, "status": "ok", "ms": 100,
             "chars": 10, "instance": "main", "results": 3, **extra}  # fmt: skip
    store().append(entry)
    return entry


@pytest.fixture
def admin(tmp_path):
    from graylog_mcp.admin.app import AdminState, build_app

    state = AdminState(tmp_path, None, "tok", allowed_hosts=("testserver",))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(state)), base_url="http://testserver",
                             headers={"X-Admin-Token": "tok"})  # fmt: skip


# ----------------------------------------------------------------------------- dashboard


def test_the_dashboard_counts_environments_slow_calls_and_sizes():
    _call(NOW - 60, ms=12000, chars=900, truncated=True)
    _call(NOW - 120, ms=2000, instance="staging", ok=False, status="error")
    _call(NOW - 180, results=0)
    _call(NOW - 240, source="playground")
    store().append({"event": "started", "call_id": "c1", "ts": NOW - 30, "tool": "root_cause", "conversation": A})
    s = usage.summarize(24, 1, now=NOW)
    (search,) = s["tools"]
    assert search["slow"] == {"1000": 2, "3000": 1, "10000": 1} and search["truncated"] == 1
    assert search["chars_p95"] == 900 and search["last_ts"] == NOW - 60
    assert s["instances"] == [{"instance": "main", "calls": 2, "errors": 0, "misses": 1},
                              {"instance": "staging", "calls": 1, "errors": 1, "misses": 0}]  # fmt: skip
    # running: listed, not counted
    assert s["totals"]["calls"] == 3 and s["recent"][0]["status"] == "running" and s["total_recent"] == 4
    assert all("preview" not in e for e in s["recent"])
    assert usage.summarize(24, 1, now=NOW, source="all")["totals"]["calls"] == 4
    assert usage.summarize(24, 1, now=NOW, source="playground")["totals"]["calls"] == 1


def test_a_record_of_an_earlier_version_has_a_status():
    assert usage.status_of({"ok": True}) == "ok" and usage.status_of({"ok": False}) == "error"
    assert usage.status_of({"ok": False, "status": "cancelled"}) == "cancelled"


# ----------------------------------------------------------------------------- turns


def test_a_pause_of_more_than_ten_minutes_starts_a_turn():
    calls = [{"ts": NOW - 1000, "ms": 100}, {"ts": NOW - 900, "ms": 100}, {"ts": NOW - 900 + 11 * 60}]
    assert [len(t) for t in conversations.split_turns(calls)] == [2, 1]


def test_a_prompt_in_between_starts_a_turn_however_short_the_pause():
    calls = [{"ts": NOW - 30, "turn": "p1"}, {"ts": NOW - 20, "turn": "p1"}, {"ts": NOW - 10, "turn": "p2"}]
    assert [len(t) for t in conversations.split_turns(calls)] == [2, 1]
    long_turn = [{"ts": NOW - 3600, "turn": "p1"}, {"ts": NOW - 60, "turn": "p1"}]  # one prompt, a long wait
    assert len(conversations.split_turns(long_turn)) == 1


def test_the_list_has_each_turn_then_what_is_left_of_older_conversations():
    _call(NOW - 3000, repo="acme", client="claude-code", agent="fake-agent-1")
    _call(NOW - 2990, tool="root_cause", ok=False, status="error", agent="fake-agent-2")
    _call(NOW - 60, results=0, args={"query": "OrderNotFound"})
    _call(NOW - 50, conversation=B, instance="staging")
    store().append({"ts": NOW - 40, "tool": "count_logs", "ok": True})  # an earlier version: no conversation
    listing = conversations.conversations(168, now=NOW)
    later, first = (t for t in listing["turns"] if t["conversation"] == A)
    assert first["tools"] == ["search_logs", "root_cause"] and first["errors"] == 1 and first["agents"] == 2
    assert first["repo"] == "acme" and first["client"] == "claude-code" and first["tier"] == "full"
    assert later["misses"] == 1 and later["turn"] == str(round((NOW - 60) * 1000))
    assert any(t["conversation"].startswith("legacy-") for t in listing["turns"])
    assert [t["conversation"] for t in conversations.conversations(168, instance="staging", now=NOW)["turns"]] == [B]
    assert [t["turn"] for t in conversations.conversations(168, q="ordernotfound", now=NOW)["turns"]] == [later["turn"]]


def test_a_turn_in_detail_with_its_requests_and_preview():
    _call(NOW - 3000, preview="older turn")
    _call(NOW - 60, preview='{"total":3}', spans=[{"path": "search/messages", "ms": 40, "status": 200}], seq=2)
    store().append({"event": "started", "call_id": "c9", "ts": NOW - 60, "tool": "root_cause", "conversation": A,
                    "seq": 1})  # fmt: skip
    turn = str(round((NOW - 60) * 1000))
    found = conversations.turn_detail(A, turn, now=NOW)
    assert found is not None
    (only,) = found["turns"]
    running, done = only["calls"]  # by start, then by seq
    assert running["status"] == "running" and only["running"] == 1
    assert done["preview"] == '{"total":3}' and done["spans"][0]["path"] == "search/messages"
    assert len(conversations.turn_detail(A, now=NOW)["turns"]) == 2  # type: ignore[index]
    assert conversations.turn_detail(A, "1", now=NOW) is None


def test_a_compacted_conversation_is_listed_as_cold():
    s = store()
    s.append({"event": "started", "call_id": "c1", "ts": NOW - 20 * 86400, "conversation": A, "tool": "search_logs"})
    s.append({"event": "finished", "call_id": "c1", "ts": NOW - 20 * 86400, "conversation": A, "tool": "search_logs",
              "ok": True, "status": "ok"})  # fmt: skip
    s.compact(NOW)
    (row,) = conversations.conversations(2160, now=NOW)["turns"]
    assert row["tier"] == "cold" and row["calls"] == 1 and row["turn"] is None and row["tools"] == ["search_logs"]
    assert conversations.turn_detail(A, now=NOW) == {"conversation": A, "turns": [], "cold": row, "detail_days": 15}


# ----------------------------------------------------------------------------- admin


async def test_admin_lists_and_opens_conversations(admin):
    _call(NOW - 60)
    _call(NOW - 30, conversation="playground", source="playground")
    async with admin:
        listing = (await admin.get("/api/conversations?range=24h")).json()
        assert [t["conversation"] for t in listing["turns"]] == [A]
        everything = (await admin.get("/api/conversations?range=24h&source=all")).json()
        assert {t["conversation"] for t in everything["turns"]} == {A, "playground"}
        turn = listing["turns"][0]["turn"]
        opened = (await admin.get(f"/api/conversations/{A}?turn={turn}")).json()
        assert opened["turns"][0]["calls"][0]["tool"] == "search_logs"
        assert (await admin.get("/api/conversations/zzz")).status_code == 400
        assert (await admin.get(f"/api/conversations/{A}?turn=x")).status_code == 400
        assert (await admin.get(f"/api/conversations/{B}")).status_code == 404
        assert (await admin.get("/api/conversations?range=1y")).status_code == 400
        assert (await admin.get("/api/usage?source=other")).status_code == 400
        assert (await admin.get("/api/usage?source=all")).json()["totals"]["calls"] == 2


async def test_every_view_of_the_page_has_its_own_address(admin, monkeypatch):
    # a Windows registry can map .js to text/plain; the page's own files keep their type anyway
    monkeypatch.setattr(mimetypes, "guess_type", lambda *_a, **_k: ("text/plain", None))
    async with admin:
        paths = ("/", "/activity", "/activity/conversations/aaaaaaaaaaaa?turn=1", "/activity/calls?tool=x",
                 "/logs/traces?instance=main&from=2026-01-01T10:00:00Z&to=2026-01-01T11:00:00Z&trace=t1")  # fmt: skip
        for path in paths:
            page = await admin.get(path, headers={"X-Admin-Token": ""})  # the page itself needs no token
            assert page.status_code == 200 and '<meta name="graylog-mcp-root" content=""' in page.text, path
            assert "__GRAYLOG_MCP_ROOT__" not in page.text and "./assets/" not in page.text
        (script,) = re.findall(r'src="(/assets/[^"]+\.js)"', page.text)
        loaded = await admin.get(script)
        assert loaded.status_code == 200 and loaded.headers["content-type"].startswith("text/javascript")
        (sheet,) = re.findall(r'href="(/assets/[^"]+\.css)"', page.text)
        assert (await admin.get(sheet)).headers["content-type"].startswith("text/css")
        assert (await admin.get("/assets/index.html")).status_code == 404  # only the built files
        assert (await admin.get("/assets/..%2Fapp.py")).status_code == 404
        beside = (await admin.get("/assets/../app.py")).text  # the page, never a file next to it
        assert beside.startswith("<!doctype html>") and "def build_app" not in beside
        missing = await admin.get("/api/no-such-thing")
        assert missing.status_code == 404 and missing.json() == {"error": "not found"}  # an API is not the page


async def test_an_export_is_the_redacted_conversation_as_a_file(admin):
    identity = {"call_id": "c1", "ts": NOW - 60, "conversation": A, "seq": 1}
    usage.track("search_logs", None, {"query": "password=fake-value"}, {"total": 1}, None, time.monotonic(), 30,
                identity=identity, status="ok", text='{"line": "password=fake-value"}')  # fmt: skip
    async with admin:
        response = await admin.get(f"/api/conversations/{A}/export?turn={round((NOW - 60) * 1000)}")
    assert response.status_code == 200
    assert response.headers["content-disposition"] == (
        f'attachment; filename="graylog-mcp-{A}-{round((NOW - 60) * 1000)}.json"'
    )
    body = response.json()
    assert body["turns"][0]["calls"][0]["preview"] and "fake-value" not in response.text
