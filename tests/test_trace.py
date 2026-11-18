"""trace_request: error kinds, long traces, empty traces and business codes."""

from __future__ import annotations

from graylog_mcp import trace


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
