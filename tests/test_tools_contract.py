"""Every tool against emulated Graylog 4.3 / 5.0 / 5.2 / 6.1 / 7.0 response shapes."""

from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest

from graylog_mcp import tools
from graylog_mcp.client import READ_ONLY_POSTS, QueryError
from graylog_mcp.shaping import dumps
from graylog_mcp.tools import ToolInputError

# Sensitive values planted in the fake dataset; none may appear in any output.
LEAKS = [
    "hunter2",
    "alice@example.com",
    "user1@example.com",
    "4111 1111 1111 1111",
    "0912345678",
    "sk_live_abc123",
]
TOKEN_BUDGET_CHARS = 30_000  # ~10k tokens


def assert_clean(result: dict) -> str:
    text = dumps(result)
    for leak in LEAKS:
        assert leak not in text, f"leaked {leak!r}"
    assert len(text) < TOKEN_BUDGET_CHARS, len(text)
    return text


def assert_read_only(fake) -> None:
    for req in fake.requests:
        path = urlparse(str(req.url)).path.removeprefix("/api/")
        assert req.method in ("GET", "POST"), req.method
        if req.method == "POST":
            assert path in READ_ONLY_POSTS, path


async def run_all(app, fake) -> dict[str, dict]:
    out = {}
    out["list_instances"] = await tools.list_instances(app)
    out["search"] = await tools.search_logs(app, query="level:<=3", range="2h", limit=100)
    out["search_raw"] = await tools.search_logs(app, query="*", range="2h", limit=30, dedup_lines=False)
    out["search_stream"] = await tools.search_logs(app, query="*", range="2h", streams=["Payments"])
    out["count"] = await tools.count_logs(app, query="level:3", range="2h")
    out["trace"] = await tools.trace_request(app, "req-7f3a9c", range="1h")
    ref = next((m["ref"] for m in out["trace"]["timeline"] if "ref" in m), None)
    if ref:
        out["get"] = await tools.get_message(app, ref)
        out["context"] = await tools.context_around(app, ref, seconds=5, scope="all")
    out["errors"] = await tools.error_summary(app, range="2h")
    out["errors_by_source"] = await tools.error_summary(app, range="2h", group_by="source")
    out["hist"] = await tools.log_histogram(app, query="level:3", range="2h")
    out["top"] = await tools.top_values(app, "source", range="2h")
    out["top_sensitive"] = await tools.top_values(app, "user_email", range="2h")
    out["compare"] = await tools.compare_periods(app, window="15m")
    out["streams"] = await tools.list_streams(app)
    out["fields"] = await tools.list_fields(app)
    return out


async def test_all_tools_on_every_version(make_app, version):
    app, fake = make_app(version)
    out = await run_all(app, fake)
    for result in out.values():
        assert_clean(result)
    assert_read_only(fake)

    inst = out["list_instances"]["instances"][0]
    assert inst["status"] == "ok" and inst["version"] == version
    major_minor = tuple(int(x) for x in version.split("+")[0].split(".")[:2])
    if major_minor >= (5, 2):
        assert inst["aggregation_api"] == "scripting"
    else:
        assert inst["aggregation_api"] == "views"

    assert out["count"]["count"] == 32  # 25 timeouts + 6 deadlocks + 1 payment failure
    assert out["errors"]["total_errors"] == 32
    groups = {g["value"]: g for g in out["errors"]["groups"]}
    assert groups["java.net.SocketTimeoutException"]["count"] == 25
    assert groups["org.postgresql.util.PSQLException"]["count"] == 6
    assert groups["java.net.SocketTimeoutException"]["first"].endswith("+07:00")
    sample = groups["java.net.SocketTimeoutException"]["sample"]
    assert sample is not None and "upstream timeout" in sample["message"]

    trace = out["trace"]
    assert trace["strategy"] == "trace_fields"
    assert [s["service"] for s in trace["steps"]] == ["gateway", "payment", "notify", "gateway"]
    assert trace["first_error"]["message"] == "payment failed"
    assert trace["duration_ms"] == 1000
    assert trace["matched_fields"] == ["trace_id"]

    top = {v["value"]: v["count"] for v in out["top"]["values"]}
    expected = {}
    for m in fake.messages:
        expected[m["source"]] = expected.get(m["source"], 0) + 1
    assert top["web-1"] == expected["web-1"] and top["web-2"] == expected["web-2"]
    assert all(v["value"] == "[EMAIL]" for v in out["top_sensitive"]["values"])
    if major_minor >= (5, 0):
        assert out["top_sensitive"]["without_field"] == len(fake.messages) - 25
    assert out["errors_by_source"]["ungrouped"] == 0

    assert out["hist"]["total"] == 32
    assert sum(b[1] for b in out["hist"]["buckets"]) == 32
    assert out["hist"]["onset"] is not None

    cmp_groups = {g["value"]: g for g in out["compare"]["groups"]}
    assert cmp_groups["java.net.SocketTimeoutException"]["status"] in ("new", "increased")

    assert out["streams"]["count"] == 2  # disabled stream hidden
    assert out["search_stream"]["total"] in (None, 26)

    if "get" in out:
        msg = out["get"]["message"]
        assert msg.get("password") in (None, "[REDACTED]")
        assert "streams" in msg
        ctx = out["context"]
        assert any(m.get("anchor") for m in ctx["messages"])
        same_stream = await tools.context_around(app, out["get"]["message"]["ref"], seconds=5, scope="stream")
        assert any(m.get("anchor") for m in same_stream["messages"])


async def test_search_dedup_groups_repeated_lines(make_app):
    app, _ = make_app()
    res = await tools.search_logs(app, query="exception_class:java.net.SocketTimeoutException", range="1h", limit=50)
    assert res["total"] == 25
    grouped = [m for m in res["messages"] if "count" in m]
    assert grouped and grouped[0]["count"] == 25


async def test_paging_and_budget(make_app):
    app, _ = make_app(limits={"max_output_chars": 3000})
    res = await tools.search_logs(app, query="*", range="2h", limit=100, dedup_lines=False)
    assert res["truncated"] is True
    assert res["next_offset"] == len(res["messages"])
    assert len(dumps(res)) <= 3000


async def test_stacktrace_compacted_in_output(make_app):
    app, _ = make_app()
    res = await tools.search_logs(app, query='message:"payment failed"', range="1h", fields=["*"])
    full = res["messages"][0]["full_message"]
    assert "com.acme.pay.PaymentService.charge" in full
    assert "com.acme.pay.PaymentController.post" in full
    assert "Proxy10" not in full and "frames" in full
    assert "Caused by: java.net.SocketTimeoutException" in full
    assert "alice@example.com" not in full


async def test_trace_falls_back_to_full_text(make_app):
    app, _ = make_app()
    res = await tools.trace_request(app, "/api/pay", range="1h")
    assert res["strategy"] == "full_text" and res["timeline"]


async def test_bad_query_is_reported_with_position(make_app, version):
    app, _ = make_app(version)
    with pytest.raises(QueryError) as exc:
        await tools.search_logs(app, query="level:(3", range="1h")
    assert "Cannot parse" in str(exc.value)
    with pytest.raises(QueryError):
        await tools.count_logs(app, query="level:(3", range="1h")


async def test_empty_result_hints_unknown_field(make_app):
    app, _ = make_app("6.1.2")
    res = await tools.search_logs(app, query="sevrity:3", range="1h")
    assert res["messages"] == [] and "unknown field: sevrity" in res["hint"]
    res = await tools.count_logs(app, query="sevrity:3", range="1h")
    assert res["count"] == 0 and "sevrity" in res["hint"]


async def test_bad_query_explained_by_validate(make_app):
    app, _ = make_app("6.1.2")
    with pytest.raises(QueryError, match=r"invalid query.*Cannot parse"):
        await tools.top_values(app, "source", query="level:(3", range="1h")


async def test_unknown_stream_suggests(make_app):
    app, _ = make_app()
    with pytest.raises(Exception) as exc:
        await tools.search_logs(app, streams=["Paymants"])
    assert "Payments" in str(exc.value)


async def test_input_errors(make_app):
    app, _ = make_app()
    with pytest.raises(ToolInputError):
        await tools.search_logs(app, range="15 parsecs")
    with pytest.raises(ToolInputError):
        await tools.search_logs(app, sort="timestamp:sideways")
    with pytest.raises(ToolInputError):
        await tools.context_around(app, "graylog_3/x", scope="planet")


async def test_version_from_api_root_when_system_forbidden(make_app):
    app, fake = make_app("5.0.13+083613e")
    fake.system_forbidden = True
    status = await tools.list_instances(app)
    assert status["instances"][0]["version"] == "5.0.13+083613e"


async def test_unsupported_version(make_app):
    app, _ = make_app("3.3.16")
    status = await tools.list_instances(app)
    assert "not supported" in status["instances"][0]["status"]


async def test_universal_removed_falls_back_to_views(make_app):
    app, fake = make_app("7.0.1")
    await tools.search_logs(app, range="1h")
    status = await tools.list_instances(app)
    assert status["instances"][0]["message_api"] == "views"
    paths = [urlparse(str(r.url)).path for r in fake.requests]
    assert paths.count("/api/search/universal/absolute") == 1  # tried once, then dropped


@pytest.mark.parametrize(("message_api", "aggregation_api"), [("scripting", "views"), ("views", "scripting")])
async def test_forced_apis(make_app, message_api, aggregation_api):
    app, fake = make_app(
        "6.1.2",
        instances={
            "main": {
                "url": "https://graylog.test",
                "token_env": "TEST_GRAYLOG_TOKEN",
                "message_api": message_api,
                "aggregation_api": aggregation_api,
            }
        },
    )
    trace = await tools.trace_request(app, "req-7f3a9c", range="1h")
    assert [s["service"] for s in trace["steps"]] == ["gateway", "payment", "notify", "gateway"]
    ref = trace["first_error"]["ref"]
    msg = await tools.get_message(app, ref)
    assert msg["message"]["exception_class"] == "java.lang.IllegalStateException"
    assert_clean(msg)
    errors = await tools.error_summary(app, range="2h")
    assert errors["total_errors"] == 32 and errors["aggregation_api"] == aggregation_api
    used = {urlparse(str(r.url)).path for r in fake.requests}
    assert ("/api/search/messages" in used) == (message_api == "scripting")
    assert "/api/search/universal/absolute" not in used


async def test_presets(make_app):
    app, _ = make_app(
        presets={
            "pay_errors": {
                "description": "payment errors",
                "tool": "error_summary",
                "args": {"query": "service:payment", "range": "2h"},
            }
        }
    )
    listed = tools.list_presets(app)
    assert listed["presets"][0]["name"] == "pay_errors"
    res = await tools.run_preset(app, "pay_errors", overrides={"group_by": "source"})
    assert res["tool"] == "error_summary" and res["total_errors"] == 26
    with pytest.raises(ToolInputError):
        await tools.run_preset(app, "pay_errors", overrides={"bogus": 1})
    with pytest.raises(ToolInputError):
        await tools.run_preset(app, "nope")


async def test_mcp_server_roundtrip(make_app):
    from graylog_mcp.server import build_server

    app, _ = make_app()
    server = build_server(app)
    tools_list = await server.list_tools()
    assert {t.name for t in tools_list} >= {"search_logs", "trace_request", "compare_periods", "list_instances"}
    assert all(t.annotations.read_only_hint for t in tools_list)
    result = await server.call_tool("count_logs", {"query": "level:3", "range": "2h"})
    payload = json.loads(result.content[0].text)
    assert payload["count"] == 32
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="Cannot parse"):
        await server.call_tool("search_logs", {"query": "level:(3"})
