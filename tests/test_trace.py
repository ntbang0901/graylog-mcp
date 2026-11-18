"""trace_request: error kinds, long traces, empty traces and business codes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import ANY

import pytest

from graylog_mcp import recover, trace
from graylog_mcp.shaping import Budget


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
