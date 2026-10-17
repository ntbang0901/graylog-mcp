"""Query repair before a search and the diagnosis of a search that matched nothing."""

from __future__ import annotations

import pytest

from graylog_mcp import tools
from graylog_mcp.smartsearch import field_refs, resolve_field, split_and
from tests.test_tools_contract import assert_clean, assert_read_only


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
    assert split_and(query) == clauses


def test_field_refs_skip_quotes_and_ranges():
    q = 'message:"GET http://x" AND took_ms:[1 TO 2] AND (Level:3 OR -src:a)'
    assert [name for _, _, name in field_refs(q)] == ["message", "took_ms", "Level", "src"]


KNOWN = {"level", "http_status", "exception_class", "logger_name", "service", "gl2_source_input", "message"}
ALIASES = {"exception": "exception_class", "logger": "logger_name"}


@pytest.mark.parametrize(
    ("name", "target", "similar"),
    [
        ("level", None, []),
        ("Level", "level", []),
        ("status", "http_status", []),
        ("exception", "exception_class", []),
        ("htpp_status", "http_status", []),
        ("nothing_like_it", None, []),
        ("_exists_", None, []),
    ],
)
def test_resolve_field(name, target, similar):
    assert resolve_field(name, KNOWN, ALIASES) == (target, similar)


def test_ambiguous_field_is_not_rewritten():
    target, similar = resolve_field("status", KNOWN | {"status_code"}, ALIASES)
    assert target is None and set(similar) == {"http_status", "status_code"}


async def test_field_names_are_fixed_and_reported(make_app):
    app, fake = make_app("6.1.2")
    res = await tools.count_logs(app, query="Service:gateway AND status:200", range="2h")
    assert res["query_used"] == "service:gateway AND http_status:200"
    assert [r["to"] for r in res["rewrites"]] == ["service:", "http_status:"]
    assert res["count"] == await _count(app, "service:gateway AND http_status:200") > 0
    assert_read_only(fake)


async def test_lower_case_operators(make_app):
    app, _ = make_app("5.2.4")
    res = await tools.count_logs(app, query="level:3 and service:payment", range="2h")
    assert res["query_used"] == "level:3 AND service:payment"
    assert res["count"] == await _count(app, "level:3 AND service:payment") > 0


async def test_bare_id_searches_trace_fields(make_app, version):
    app, _ = make_app(version)
    res = await tools.search_logs(app, query="req-7f3a9c", range="1h", dedup_lines=False)
    assert res["total"] == 6 and res["rewrites"][0]["why"].startswith("looks like an id")
    assert_clean(res)
    raw = await tools.search_logs(app, query="req-7f3a9c", range="1h", exact=True)
    assert raw["messages"] == [] and "rewrites" not in raw


async def test_exact_keeps_the_query(make_app):
    app, _ = make_app("6.1.2")
    res = await tools.count_logs(app, query="Service:payment", range="2h", exact=True)
    assert "query_used" not in res and res["count"] == 0


async def test_diagnosis_names_the_clause_and_its_values(make_app, version):
    app, fake = make_app(version)
    res = await tools.search_logs(app, query="service:paymnt AND level:3", range="2h")
    diag = res["diagnosis"]
    assert diag["matches_nothing"] == ["service:paymnt"]
    assert "payment" in diag["values"][0]["did_you_mean"]
    assert "did you mean payment" in res["hint"]
    assert_clean(res)
    assert_read_only(fake)


async def test_diagnosis_finds_matches_outside_the_range(make_app):
    app, _ = make_app("6.1.2")
    res = await tools.count_logs(app, query='"deadlock detected"', range="5m")
    assert res["count"] == 0
    outside = res["diagnosis"]["outside_range"]
    assert outside["range"] == "24h" and outside["count"] == 6 and outside["last_seen"]
    assert "widen the range" in res["hint"]


async def test_diagnosis_of_an_unknown_field(make_app):
    app, _ = make_app("6.1.2")
    res = await tools.count_logs(app, query="nosuchfield:1 AND level:3", range="2h")
    assert res["diagnosis"]["fields"][0]["problem"] == "no field 'nosuchfield' in these logs"
    assert res["diagnosis"]["clauses"][1] == {"clause": "level:3", "count": await _count(app, "level:3")}


async def test_diagnosis_when_clauses_only_fail_together(make_app):
    app, _ = make_app("6.1.2")
    res = await tools.count_logs(app, query="service:gateway AND level:3", range="2h")
    assert res["count"] == 0
    assert "no message matches all of them together" in res["diagnosis"]["note"]


async def test_sensitive_values_are_not_suggested(make_app):
    app, _ = make_app("6.1.2")
    res = await tools.count_logs(app, query="password:hunter AND level:3", range="2h")
    assert_clean(res)


async def _count(app, query: str) -> int:
    return (await tools.count_logs(app, query=query, range="2h", exact=True))["count"]


async def test_numbers_are_not_guessed_and_dead_clauses_are_looked_up_further_back(make_app):
    app, _ = make_app("6.1.2")
    res = await tools.count_logs(app, query='"deadlock detected" and status:500', range="5m")
    assert res["query_used"] == '"deadlock detected" AND http_status:500'
    diag = res["diagnosis"]
    assert "did_you_mean" not in diag["values"][0] and diag["values"][0]["values_in_range"] == ["200"]
    assert diag["clauses_outside_range"] == {"range": "24h", "clauses": [{"clause": '"deadlock detected"', "count": 6}]}


async def test_no_diagnosis_when_paging_past_the_end(make_app):
    app, _ = make_app("6.1.2")
    res = await tools.search_logs(app, query="level:3", range="2h", offset=10_000)
    assert res["messages"] == [] and "diagnosis" not in res


async def test_exists_names_a_field_too(make_app):
    app, _ = make_app("6.1.2")
    res = await tools.count_logs(app, query="_exists_:Trace_ID", range="2h")
    assert res["query_used"] == "_exists_:trace_id" and res["count"] == 6
