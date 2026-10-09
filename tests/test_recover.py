"""Searches that find nothing: safe rewrites, counted suggestions, and fixes learned from use."""

from __future__ import annotations

import pytest

from graylog_mcp import recover, tools


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    recover._fields.clear()
    recover.LEARNED.pending.clear()
    # keyword fields are case-sensitive in Graylog (the emulator folds case, like the analyzed message field)
    from tests import fake_graylog

    plain = fake_graylog._eq

    def keyword_eq(actual, expected, phrase=False):
        if phrase or isinstance(actual, (list, int, float)):
            return plain(actual, expected, phrase)
        return actual is not None and str(actual) == expected

    monkeypatch.setattr(fake_graylog, "_eq", keyword_eq)
    yield


def test_parsing():
    q = 'service:payment AND message:"payment failed" OR level:[3 TO 4] NOT OrderNotFound "a quoted phrase"'
    assert [(t.field, t.bare_value) for t in recover.terms(q)] == [
        ("service", "payment"), ("message", "payment failed"), ("level", "[3 TO 4]")]  # fmt: skip
    assert [w for _, _, w in recover.bare_words(q)] == ["OrderNotFound"]  # not operators, values or phrases
    phrase = '"timeout at host:db1" AND service:x'
    assert [(t.field, t.bare_value) for t in recover.terms(phrase)] == [("service", "x")]  # host:db1 is phrase text
    assert recover.Learned.weight(None) == 0.5
    assert recover.Learned.weight({"offered": 2, "used": 2}) == 0.75 and recover.Learned.trusted(
        {"offered": 2, "used": 2}
    )
    assert not recover.Learned.trusted({"offered": 1, "used": 1})  # one use is not enough
    assert not recover.Learned.trusted({"offered": 6, "used": 2})  # used but mostly ignored


async def test_impossible_queries_are_fixed_before_they_run(make_app):
    app, _ = make_app("6.1.2")
    out = await tools.search_logs(app, query="level:ERROR", range="1h")
    assert out["rewritten"]["from"] == "level:ERROR" and out["rewritten"]["ran"] == "level:3"
    assert "syslog numbers" in out["rewritten"]["why"][0] and out["messages"]
    out = await tools.count_logs(app, query="Service:payment AND _exists_:Trace_id", range="1h")
    assert out["rewritten"]["ran"] == "service:payment AND _exists_:trace_id" and out["count"] > 0
    out = await tools.count_logs(app, query="service:payment", range="1h")
    assert "rewritten" not in out  # nothing to fix: the query runs as written


async def test_empty_search_returns_counted_suggestions(make_app):
    app, _ = make_app("6.1.2")
    out = await tools.count_logs(app, query="service:Payment", range="1h")
    assert out["count"] == 0
    best = out["suggestions"][0]
    assert best["query"] == "service:payment" and best["count"] > 0 and "lower case" in best["why"]
    assert "'service:payment'" in out["hint"] and str(best["count"]) in out["hint"]
    out = await tools.search_logs(app, query="app:payment", range="1h")
    assert not out["messages"] and out["suggestions"][0]["query"] == "service:payment"  # a known synonym
    out = await tools.count_logs(app, query='message:"payment failed"', range="1m")  # it happened 4 minutes ago
    wider = next(s for s in out["suggestions"] if s.get("range"))
    assert wider["range"] == "1h" and wider["count"] == 1 and "range='1h'" in out["hint"]
    out = await tools.count_logs(app, query="service:nothing_like_this", range="1h")
    assert out["count"] == 0 and "suggestions" not in out  # nothing found anything: the plain hint


async def test_used_suggestions_become_automatic(make_app):
    app, _ = make_app("6.1.2")
    for _ in range(recover.AUTO_MIN_USES):
        miss = await tools.count_logs(app, query="service:Payment", range="1h")
        assert miss["count"] == 0 and "rewritten" not in miss
        await tools.count_logs(app, query=miss["suggestions"][0]["query"], range="1h")  # the model runs it
    rule = next(r for r in recover.LEARNED.listing() if r["kind"] == "case")
    assert rule["used"] == 2 and rule["offered"] == 2 and rule["auto"] and rule["instance"] == "main"
    out = await tools.count_logs(app, query="service:Payment", range="1h")
    assert out["count"] > 0 and out["rewritten"]["ran"] == "service:payment"
    assert "learned: used 2 of 2 times" in out["rewritten"]["why"][0] and "suggestions" not in out
    page = await tools.search_logs(app, query="service:Payment", range="1h")
    assert page["messages"] and page["rewritten"]["ran"] == "service:payment"
    assert recover.LEARNED.forget(rule["key"]) and not recover.LEARNED.forget(rule["key"])
    out = await tools.count_logs(app, query="service:Payment", range="1h")
    assert out["count"] == 0 and out["suggestions"]  # forgotten: back to a suggestion


async def test_ignored_suggestions_do_not_become_automatic(make_app):
    app, _ = make_app("6.1.2")
    for _ in range(4):
        await tools.count_logs(app, query="service:Payment", range="1h")  # offered, never run
    await tools.count_logs(app, query="service:payment", range="1h")  # run once
    rule = next(r for r in recover.LEARNED.listing() if r["kind"] == "case")
    assert rule["offered"] == 4 and rule["used"] == 1 and not rule["auto"]


async def test_learning_can_be_turned_off(make_app, monkeypatch):
    monkeypatch.setenv("GRAYLOG_MCP_LEARN", "off")
    app, _ = make_app("6.1.2")
    out = await tools.count_logs(app, query="service:Payment", range="1h")
    assert out["suggestions"]  # suggestions still come
    await tools.count_logs(app, query="service:payment", range="1h")
    assert not recover.LEARNED.path().exists()
    out = await tools.count_logs(app, query="level:ERROR", range="1h")
    assert out["rewritten"]["ran"] == "level:3"  # deterministic fixes stay


async def test_admin_lists_and_forgets_rules(make_app, tmp_path):
    import httpx

    from graylog_mcp.admin.app import AdminState, build_app

    app, _ = make_app("6.1.2")
    await tools.count_logs(app, query="service:Payment", range="1h")
    state = AdminState(tmp_path, None, "tok", allowed_hosts=("testserver",))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(state)), base_url="http://testserver",
                                 headers={"X-Admin-Token": "tok"}) as client:  # fmt: skip
        body = (await client.get("/api/learned")).json()
        assert body["enabled"] and body["auto_weight"] == recover.AUTO_WEIGHT
        rule = body["rules"][0]
        assert rule["offered"] == 1 and rule["used"] == 0 and rule["weight"] == pytest.approx(1 / 3, abs=0.01)
        assert (await client.post("/api/learned/forget", json={"key": rule["key"]})).json() == {"ok": True}
        assert (await client.post("/api/learned/forget", json={"key": rule["key"]})).status_code == 400
        assert (await client.get("/api/learned")).json()["rules"] == []
