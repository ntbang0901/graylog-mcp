"""Searches that find nothing: safe rewrites, counted suggestions, and fixes learned from use."""

from __future__ import annotations

import pytest

from graylog_mcp import recover, tools
from tests.test_tools_contract import assert_clean, assert_read_only


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


# --------------------------------------------------------------------------- configured names, operators, ids


@pytest.mark.parametrize(
    ("query", "clauses"),
    [
        ("a AND (b AND c) AND NOT d", ["a", "b", "c", "NOT d"]),
        ("(x:1) AND (y:2)", ["x:1", "y:2"]),
        ('"a AND b" AND c', ['"a AND b"', "c"]),
        ("a OR b AND c", ["a OR b AND c"]),
        ("(a OR b) AND c", ["a OR b", "c"]),
        ("ssh login", ["ssh login"]),
    ],
)
def test_split_and(query, clauses):
    assert recover.split_and(query) == clauses


def test_operators_ids_and_values():
    assert recover.lower_operators('"x and y" and status:500 or a') == '"x and y" AND status:500 OR a'
    assert recover.lower_operators("and then") is None  # not between two terms
    assert recover.bare_id("req-7f3a9c") == "req-7f3a9c" and recover.bare_id('"0123456789abcdef0"')
    assert recover.bare_id("timeout") is None and recover.bare_id("user123") is None
    assert recover.close_values("paymnt", ["gateway", "payment"]) == ["payment"]
    assert recover.close_values("500", ["200"]) == []  # numbers are never guessed


def test_known_field_from_config_or_the_one_field_ending_in_the_word():
    info = dict.fromkeys(["level", "http_status", "exception_class", "service"])
    configured = {"exception": "exception_class"}
    assert recover._known_field("status", info, "main", {}, configured) == "http_status"
    assert recover._known_field("exception", info, "main", {}, configured) == "exception_class"
    assert recover._known_field("status", {**info, "response_status": None}, "main", {}, configured) is None
    assert recover._known_field("nothing", info, "main", {}, configured) is None


async def test_configured_and_unique_fields_are_fixed_before_they_run(make_app):
    app, _ = make_app("6.1.2")
    out = await tools.count_logs(app, query="Service:gateway AND status:200", range="2h")
    assert out["rewritten"]["ran"] == "service:gateway AND http_status:200" and out["count"] > 0
    out = await tools.count_logs(app, query='exception:"java.net.SocketTimeoutException"', range="1h")
    assert out["rewritten"]["ran"].startswith("exception_class:") and out["count"] == 25


async def test_lower_case_operators_and_bare_ids_are_suggested(make_app):
    app, _ = make_app("6.1.2")
    out = await tools.count_logs(app, query="level:3 and service:payment", range="2h")
    assert out["count"] == 0 and "rewritten" not in out  # it could have matched: suggested, never applied
    assert out["suggestions"][0]["query"] == "level:3 AND service:payment" and out["suggestions"][0]["count"] > 0
    out = await tools.search_logs(app, query="req-7f3a9c", range="1h")
    best = out["suggestions"][0]
    assert best["count"] == 6 and "trace_id:" in best["query"] and "looks like an id" in best["why"]


async def test_wider_range_says_when_the_last_match_was(make_app):
    app, _ = make_app("6.1.2")
    out = await tools.count_logs(app, query='"deadlock detected"', range="5m")
    wider = next(s for s in out["suggestions"] if s.get("range"))
    assert wider["count"] > 0 and wider["last_seen"]


async def test_diagnosis_names_the_clause_and_its_values(make_app, version):
    app, fake = make_app(version)
    out = await tools.search_logs(app, query="service:paymnt AND level:3", range="2h")
    diag = out["diagnosis"]
    assert diag["matches_nothing"] == ["service:paymnt"]
    assert diag["clauses"][1] == {"clause": "level:3", "count": 32}
    assert diag["values"][0]["did_you_mean"] == ["payment"] and "did you mean payment" in out["hint"]
    assert_clean(out)
    assert_read_only(fake)


async def test_diagnosis_when_clauses_only_fail_together_or_further_back(make_app):
    app, _ = make_app("6.1.2")
    out = await tools.count_logs(app, query="service:gateway AND level:3", range="2h")
    assert "no message matches all of them together" in out["diagnosis"]["note"]
    out = await tools.count_logs(app, query='"deadlock detected" and status:500', range="5m")
    diag = out["diagnosis"]
    assert out["rewritten"]["ran"] == '"deadlock detected" and http_status:500'
    assert diag["values"][0] == {"field": "http_status", "value": "500", "values_in_range": ["200"]}
    further = diag["clauses_further_back"]
    assert further["range"] == "1h" and further["clauses"][0]["clause"] == '"deadlock detected"'


async def test_diagnosis_masks_values_and_skips_pages_past_the_end(make_app):
    app, _ = make_app("6.1.2")
    out = await tools.count_logs(app, query="password:hunter AND level:3", range="2h")
    assert_clean(out)
    out = await tools.search_logs(app, query="level:3", range="2h", offset=10_000)
    assert out["messages"] == [] and "diagnosis" not in out
