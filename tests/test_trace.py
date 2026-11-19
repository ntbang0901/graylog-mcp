"""trace_request: error kinds, long traces, empty traces and business codes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import ANY

import pytest

from graylog_mcp import recover, tools, trace, ui_links
from graylog_mcp.client import requests_sent
from graylog_mcp.config import ConfigError
from graylog_mcp.shaping import Budget, dumps
from tests.conftest import make_config


def _add(fake, seconds_ago=5, **fields):
    ts = datetime.now(UTC) - timedelta(seconds=seconds_ago)
    base = {
        "_id": f"id-{len(fake.messages)}",
        "index": "graylog_3",
        "timestamp": ts.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "source": "api",
        "level": 6,
        "message": "m",
    }
    fake.messages.append({**base, **fields})


async def test_business_errors_are_marked_apart_from_technical_ones(make_app):
    app, _ = make_app(investigation={"business_error_query": 'message:"queued"'})
    res = await trace.trace_request(app, "req-7f3a9c", range="1h")
    kinds = {m["message"]: m.get("error") for m in res["timeline"]}
    assert kinds["payment failed"] == "technical"
    assert next(v for k, v in kinds.items() if "queued" in k) == "business"
    assert res["first_error"]["service"] == "payment"
    assert res["first_business_error"]["service"] == "notify"
    notify = next(s for s in res["services"] if s["service"] == "notify")
    assert (notify["errors"], notify["business_errors"]) == (0, 1)
    assert res["error_queries"]["business"] == 'message:"queued"'


async def test_without_a_business_definition_the_result_has_no_business_keys(make_app):
    app, fake = make_app()
    for m in fake.messages:  # logs without an HTTP status field have no default business error
        m.pop("http_status", None)
    res = await trace.trace_request(app, "req-7f3a9c", range="1h")
    assert "first_business_error" not in res and "business" not in res["error_queries"]
    assert all("business_errors" not in s for s in res["services"])


async def test_a_refused_error_query_does_not_fail_the_trace(make_app):
    app, _ = make_app(investigation={"business_error_query": "message:(queued"})
    res = await trace.trace_request(app, "req-7f3a9c", range="1h")
    assert res["first_error"]["service"] == "payment" and "error query" in res["note"]


def _long_trace(fake, lines=300, error_at=250):
    fake.messages = []
    for i in range(lines):
        _add(
            fake,
            seconds_ago=600 - i,
            trace_id="long-1",
            service="gateway" if i < 150 else "payment",
            level=3 if i == error_at else 6,
            message=f"step {i}",
        )


@pytest.mark.parametrize("message_api", ["auto", "scripting"])
async def test_an_error_beyond_the_limit_is_still_shown(make_app, message_api):
    main = {"url": "https://graylog.test", "token_env": "TEST_GRAYLOG_TOKEN", "message_api": message_api}
    app, fake = make_app("6.1.2", instances={"main": main})
    _long_trace(fake)
    res = await trace.trace_request(app, "long-1", range="1h", limit=50)
    assert res["first_error"]["message"] == "step 250"
    shown = [m["message"] for m in res["timeline"]]
    assert shown[:50] == [f"step {i}" for i in range(50)] and shown[-1] == "step 299"
    assert "step 250" in shown and shown == sorted(shown, key=lambda s: int(s.split()[1]))
    assert "every error line" in res["note"]
    if message_api == "auto":
        assert res["hidden"] == 300 - len(shown)


def test_keep_by_priority_keeps_errors_and_step_edges_first():
    entries = [{"service": "a" if i < 10 else "b", "message": "x" * 100, "n": i} for i in range(20)]
    entries[7]["error"] = "technical"
    entries[15]["error"] = "business"
    kept = trace.keep_by_priority(entries, Budget(960, reserve=0))  # room for the six priority lines, not a seventh
    numbers = [e["n"] for e in kept]
    assert {7, 15, 0, 9, 10, 19} <= set(numbers) and numbers == sorted(numbers) and len(numbers) < 20


def test_keep_by_priority_keeps_everything_that_fits():
    entries = [{"service": "a", "message": "x", "n": i} for i in range(5)]
    assert trace.keep_by_priority(entries, Budget(10_000)) == entries


async def test_an_empty_trace_says_where_the_id_was_last_seen(make_app, monkeypatch):
    monkeypatch.setattr(recover.LEARNED, "offer", lambda *a, **k: pytest.fail("trace must not teach recover"))
    app, _ = make_app()
    res = await trace.trace_request(app, "req-7f3a9c", range="1m")
    assert res["timeline"] == []
    assert res["suggestions"] == [{"range": "1h", "count": 6, "last_seen": ANY, "why": "found further back"}]
    assert "range='1h'" in res["hint"]


async def test_an_id_found_nowhere_keeps_the_plain_hint(make_app):
    app, _ = make_app()
    res = await trace.trace_request(app, "no-such-id", range="1m")
    assert "suggestions" not in res and "trace_fields" in res["hint"]


def _order(fake):
    fake.messages = []
    _add(fake, 300, trace_id="req-aaa111", service="gateway", level=6, message="POST /orders ORD-1001 received")
    _add(fake, 299, trace_id="req-aaa111", service="payment", level=3, message="charge refused")
    _add(fake, 200, trace_id="req-bbb222", service="gateway", level=6, message="GET /orders/ORD-1001 200")
    _add(fake, 100, service="batch", level=6, message="nightly export of ORD-1001")


async def test_a_business_code_leads_to_its_traces(make_app):
    app, fake = make_app()
    _order(fake)
    await tools.count_logs(app, query="*", range="1h")  # version detection is not part of the count below
    before = len(fake.requests)
    res = await trace.trace_request(app, "ORD-1001", range="1h")
    assert res["strategy"] == "business_code" and res["shown_trace"] == "req-aaa111"
    assert [t["trace_id"] for t in res["traces"]] == ["req-aaa111", "req-bbb222"]
    assert res["traces"][0]["errors"] == 1 and res["traces"][0]["services"] == ["gateway", "payment"]
    assert [m["message"] for m in res["timeline"]] == ["POST /orders ORD-1001 received", "charge refused"]
    assert res["first_error"]["message"] == "charge refused" and res["total"] == 2
    assert [m["message"] for m in res["unlinked"]] == ["nightly export of ORD-1001"]
    assert len(fake.requests) - before <= 9


async def test_a_code_without_any_trace_id_stays_a_full_text_search(make_app):
    app, fake = make_app()
    fake.messages = []
    _add(fake, 100, service="batch", level=6, message="nightly export of ORD-1001")
    res = await trace.trace_request(app, "ORD-1001", range="1h")
    assert res["strategy"] == "full_text" and "traces" not in res and len(res["timeline"]) == 1


async def test_more_traces_than_the_cap_are_counted_not_listed(make_app):
    app, fake = make_app()
    fake.messages = []
    for i in range(13):
        _add(fake, 400 - i, trace_id=f"req-{i:03d}", service="gateway", level=6, message="ORD-1001 seen")
    res = await trace.trace_request(app, "ORD-1001", range="1h")
    assert len(res["traces"]) == 10 and res["traces_omitted"] == 3


@pytest.mark.parametrize("code", ["ORD/2026:15", 'say "hi"', "a AND (b"])
async def test_a_code_with_query_syntax_is_searched_as_text(make_app, code):
    app, fake = make_app()
    fake.messages = []
    _add(fake, 100, trace_id="req-aaa111", service="gateway", level=6, message=f"order {code} received")
    res = await trace.trace_request(app, code, range="1h")
    assert res["strategy"] == "business_code" and res["shown_trace"] == "req-aaa111"


async def test_a_trace_id_the_redactor_changes_is_shown_masked(make_app):
    app, fake = make_app()
    ui_links.record("127.0.0.1", 18742, "/admin/")
    fake.messages = []
    _add(fake, 100, trace_id="0912345678", service="gateway", level=6, message="ORD-1001 received")
    res = await trace.trace_request(app, "ORD-1001", range="1h")
    assert "0912345678" not in dumps(res) and res["strategy"] == "business_code"
    assert res["ui"].startswith("http://127.0.0.1:18742/admin/logs/traces?") and "trace=" not in res["ui"]


async def test_a_trace_id_from_a_sensitive_field_stays_out_of_the_link(make_app):
    app, fake = make_app(investigation={"trace_fields": ["session_id"]})
    ui_links.record("127.0.0.1", 18742, "/admin/")
    fake.messages = []
    _add(fake, 100, session_id="abcd-ef01-2345", service="gateway", message="ORD-1001 received")
    res = await trace.trace_request(app, "ORD-1001", range="1h")
    assert res["shown_trace"] == "[REDACTED]" and "abcd-ef01-2345" not in dumps(res)


async def test_a_request_logged_under_two_trace_fields_is_followed_whole(make_app):
    app, fake = make_app()
    fake.messages = []
    _add(fake, 300, trace_id="abc-1", service="gateway", message="ORD-1001 received")
    _add(fake, 299, traceId="abc-1", service="payment", level=3, message="charge refused")
    _add(fake, 298, request_id="abc-1", service="notify", message="ORD-1001 mail queued")
    res = await trace.trace_request(app, "ORD-1001", range="1h")
    assert [m["message"] for m in res["timeline"]] == ["ORD-1001 received", "charge refused", "ORD-1001 mail queued"]
    assert res["first_error"]["message"] == "charge refused" and len(res["traces"]) == 1 and "unlinked" not in res


async def test_every_error_line_within_the_limit_is_counted(make_app):
    app, fake = make_app()
    fake.messages = []
    for i in range(80):
        _add(fake, 600 - i, trace_id="T-err", service="gateway", level=3, message=f"failure {i}")
    res = await trace.trace_request(app, "T-err", range="1h", limit=200)
    assert res["services"][0]["errors"] == 80 and all(m["error"] == "technical" for m in res["timeline"])


async def test_a_cut_trace_with_more_errors_than_it_reads_does_not_claim_every_one(make_app):
    app, fake = make_app()
    fake.messages = []
    for i in range(300):
        _add(fake, 600 - i, trace_id="T-err", service="gateway", level=3, message=f"failure {i}")
    res = await trace.trace_request(app, "T-err", range="1h", limit=50)
    assert "every error line" not in res["note"] and "earliest 50 error lines" in res["note"]


async def test_a_trace_the_summary_could_not_read_is_still_listed(make_app):
    app, fake = make_app(limits={"max_limit": 60, "default_limit": 50})
    fake.messages = []
    for t in range(3):
        for i in range(40):
            _add(
                fake,
                900 - t * 100 - i,
                trace_id=f"req-{t}",
                service="gateway",
                message="ORD-1001" if i == 0 else "work",
            )
    res = await trace.trace_request(app, "ORD-1001", range="1h")
    assert [t["trace_id"] for t in res["traces"]] == ["req-0", "req-1", "req-2"]
    assert "messages" in res["traces"][0] and "messages" not in res["traces"][2]


async def test_a_code_search_cut_at_the_limit_says_more_requests_may_exist(make_app):
    app, fake = make_app()
    fake.messages = []
    for i in range(30):
        _add(fake, 400 - i, trace_id=f"req-{i:03d}", service="gateway", message="ORD-1001 seen")
    res = await trace.trace_request(app, "ORD-1001", range="1h", limit=20)
    assert len(res["traces"]) == 10 and res["traces_omitted"] == 10 and "more requests" in res["note"]


async def test_an_empty_absolute_range_suggests_absolute_times(make_app):
    app, _ = make_app()
    now = datetime.now(UTC)
    start, end = (now - timedelta(seconds=120)).isoformat(), (now - timedelta(seconds=60)).isoformat()
    res = await trace.trace_request(app, "req-7f3a9c", range=None, from_time=start, to_time=end)
    (found,) = res["suggestions"]
    assert "range" not in found and found["count"] == 6 and {"from_time", "to_time"} <= set(found)
    assert "from_time=" in res["hint"] and "range=" not in res["hint"]


@pytest.fixture
def used_requests():
    """Pretend the tool call already sent this many Graylog requests. Set inside the test's own task, whose
    context ends with it: nothing to undo."""

    def use(count):
        sent = [count]
        requests_sent.set(sent)
        return sent

    return use


async def test_a_trace_under_the_request_cap_is_unchanged(make_app, used_requests):
    app, _ = make_app(investigation={"business_error_query": 'message:"queued"'})
    sent = used_requests(0)
    res = await trace.trace_request(app, "req-7f3a9c", range="1h")
    assert res["first_business_error"]["service"] == "notify" and "note" not in res and sent[0] <= 9
    assert tools.requests_left(app) == 20 - sent[0] and tools.requests_used(app) == f"{sent[0]}/20"


async def test_out_of_requests_optional_work_is_dropped_and_named(make_app, used_requests):
    app, _ = make_app(investigation={"business_error_query": 'message:"queued"'})
    used_requests(18)
    res = await trace.trace_request(app, "req-7f3a9c", range="1h")
    assert len(res["timeline"]) == 6 and res["first_error"]["message"] == "payment failed"
    assert all(m.get("error") != "business" for m in res["timeline"]) and "first_business_error" not in res
    assert "business errors not marked" in res["note"] and "/20 Graylog requests used" in res["note"]


async def test_out_of_requests_a_long_trace_keeps_its_first_lines(make_app, used_requests):
    app, fake = make_app()
    _long_trace(fake)
    used_requests(19)
    res = await trace.trace_request(app, "long-1", range="1h", limit=50)
    shown = [m["message"] for m in res["timeline"]]
    assert shown[:50] == [f"step {i}" for i in range(50)] and "step 299" not in shown
    assert "last line not read" in res["note"] and res["hidden"] == 300 - len(shown)


async def test_out_of_requests_an_empty_trace_does_not_look_further_back(make_app, used_requests):
    app, _ = make_app()
    used_requests(19)
    res = await trace.trace_request(app, "req-7f3a9c", range="1m")
    assert res["timeline"] == [] and "suggestions" not in res and "Graylog requests used" in res["hint"]


def test_the_request_cap_comes_from_the_config():
    assert make_config().limits.max_requests == 20
    assert make_config(limits={"max_requests": 35}).limits.max_requests == 35
    with pytest.raises(ConfigError, match=r"limits\.max_requests"):
        make_config(limits={"max_requests": 0})


async def test_outside_a_tool_call_nothing_is_counted(make_app):
    app, _ = make_app(limits={"max_requests": 3})
    res = await trace.trace_request(app, "req-7f3a9c", range="1h")  # no counter: the admin page, a script
    assert tools.requests_left(app) == 3 and "note" not in res


ORDER_CODES = {"codes": {"order": {"pattern": r"\d{6}", "fields": ["order_id", "orderId"]}}}


def _numbered_order(fake):
    fake.messages = []
    _add(fake, 300, trace_id="req-aaa111", service="gateway", order_id="100200", message="order received")
    _add(fake, 299, trace_id="req-aaa111", service="payment", level=3, message="charge refused")
    _add(fake, 200, trace_id="req-zzz999", service="report", message="took 100200 ms")


async def test_a_code_is_looked_up_in_its_field_before_full_text(make_app):
    app, fake = make_app(glossary=ORDER_CODES)
    _numbered_order(fake)
    res = await trace.trace_request(app, "100200", range="1h")
    assert res["strategy"] == "business_code" and res["code"] == {"kind": "order", "fields": ["order_id"]}
    assert [t["trace_id"] for t in res["traces"]] == ["req-aaa111"]  # not the line that only holds the number
    assert res["first_error"]["message"] == "charge refused"


async def test_without_the_glossary_the_same_number_is_a_full_text_search(make_app):
    app, fake = make_app()
    _numbered_order(fake)
    res = await trace.trace_request(app, "100200", range="1h")
    assert "code" not in res and [t["trace_id"] for t in res["traces"]] == ["req-zzz999"]


async def test_a_code_whose_field_holds_nothing_falls_back_to_full_text(make_app):
    app, fake = make_app(glossary=ORDER_CODES)
    fake.messages = []
    _add(fake, 300, trace_id="req-aaa111", service="gateway", message="order 100200 received")
    res = await trace.trace_request(app, "100200", range="1h")
    assert res["strategy"] == "business_code" and res["code"] == {"kind": "order"}
    assert res["shown_trace"] == "req-aaa111"


async def test_a_code_found_in_its_field_without_a_trace_id(make_app):
    app, fake = make_app(glossary=ORDER_CODES)
    fake.messages = []
    _add(fake, 300, service="batch", order_id="100200", message="exported")
    _add(fake, 200, service="report", message="took 100200 ms")
    res = await trace.trace_request(app, "100200", range="1h")
    assert res["strategy"] == "code_field" and [m["message"] for m in res["timeline"]] == ["exported"]
    assert res["code"] == {"kind": "order", "fields": ["order_id"]}


async def test_a_trace_id_shaped_like_a_code_is_still_a_trace_id(make_app):
    app, fake = make_app(glossary=ORDER_CODES)
    fake.messages = []
    _add(fake, 300, trace_id="100200", service="gateway", message="hello")
    res = await trace.trace_request(app, "100200", range="1h")
    assert res["strategy"] == "trace_fields" and res["code"] == {"kind": "order"}


async def test_an_empty_range_looks_further_back_in_the_code_field_too(make_app):
    app, fake = make_app(glossary=ORDER_CODES)
    fake.messages = []
    _add(fake, 600, trace_id="req-aaa111", service="gateway", order_id="100200", message="order received")
    res = await trace.trace_request(app, "100200", range="1m")
    assert res["suggestions"][0]["count"] == 1 and res["code"] == {"kind": "order"}


def _wide_lines(fake, lines):
    fake.messages = []
    for i in range(lines):
        _add(fake, 600 - i, trace_id="T-wide", service="gateway", message=f"step {i} " + "x" * 900)


async def test_a_trace_the_size_cap_cut_says_so_and_claims_no_more_than_it_shows(make_app):
    app, fake = make_app()
    _wide_lines(fake, 300)
    res = await trace.trace_request(app, "T-wide", range="1h", limit=200)
    shown = len(res["timeline"])
    assert shown < 200 and res["hidden"] == 300 - shown
    assert f"showing {shown} of 300 messages" in res["note"] and "size cap" in res["note"]
    assert "the earliest 200" not in res["note"] and "raise limit" not in res["note"]


async def test_a_trace_read_whole_but_cut_by_the_size_cap_says_so(make_app):
    app, fake = make_app()
    _wide_lines(fake, 60)
    res = await trace.trace_request(app, "T-wide", range="1h")
    shown = len(res["timeline"])
    assert shown < 60 and f"showing {shown} of 60 messages" in res["note"] and "size cap" in res["note"]
