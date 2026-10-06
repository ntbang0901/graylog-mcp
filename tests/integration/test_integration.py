"""All tools against a real Graylog seeded by seed.py.

GRAYLOG_IT_URL=http://127.0.0.1:9050 pytest -m integration
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

import httpx
import pytest

from graylog_mcp import scan, tools
from graylog_mcp.client import READ_ONLY_POSTS, QueryError
from graylog_mcp.config import parse_config
from graylog_mcp.tools import App
from tests.test_tools_contract import LEAKS, assert_clean

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("GRAYLOG_IT_URL"), reason="GRAYLOG_IT_URL not set"),
]


class RecordingTransport(httpx.AsyncBaseTransport):
    """Real network transport that remembers every request the server sends."""

    def __init__(self):
        self.inner = httpx.AsyncHTTPTransport()
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


@pytest.fixture
def recorder():
    return RecordingTransport()


@pytest.fixture
async def app(monkeypatch, recorder):
    monkeypatch.setenv("IT_USER", os.environ.get("GRAYLOG_IT_USER", "admin"))
    monkeypatch.setenv("IT_PASSWORD", os.environ.get("GRAYLOG_IT_PASSWORD", "admin"))
    cfg = parse_config(
        {
            "timezone": "Asia/Ho_Chi_Minh",
            "redaction": {"packs": ["vn"]},
            "stacktrace": {"app_packages": ["com.acme"]},
            "instances": {
                "it": {
                    "url": os.environ["GRAYLOG_IT_URL"],
                    "auth": "basic",
                    "username_env": "IT_USER",
                    "password_env": "IT_PASSWORD",
                    "timeout": 60,
                    "message_api": os.environ.get("GRAYLOG_IT_MESSAGE_API", "auto"),
                    "aggregation_api": os.environ.get("GRAYLOG_IT_AGGREGATION_API", "auto"),
                }
            },
        }
    )
    application = App.create(cfg, transport=recorder)
    yield application
    await application.close()


async def test_every_tool(app):
    status = await tools.list_instances(app)
    inst = status["instances"][0]
    assert inst["status"] == "ok", inst
    print("instance:", inst)

    count = await tools.count_logs(app, query="level:3", range="1d")
    assert count["count"] == 32, count

    search = await tools.search_logs(app, query="level:<=3", range="1d", limit=100)
    assert search["messages"]
    assert_clean(search)

    trace = await tools.trace_request(app, "req-7f3a9c", range="1d")
    assert trace["strategy"] == "trace_fields", trace
    assert [s["service"] for s in trace["steps"]] == ["gateway", "payment", "notify", "gateway"]
    assert trace["first_error"]["message"] == "payment failed"
    assert_clean(trace)

    ref = trace["first_error"].get("ref")
    if ref:
        msg = await tools.get_message(app, ref)
        assert_clean(msg)
        assert "frames" in msg["message"].get("full_message", "frames")
        ctx = await tools.context_around(app, ref, seconds=5, scope="all")
        assert any(m.get("anchor") for m in ctx["messages"])
        assert_clean(ctx)

    errors = await tools.error_summary(app, range="1d")
    assert errors["total_errors"] == 32, errors
    groups = {g["value"]: g["count"] for g in errors["groups"]}
    assert groups.get("java.net.SocketTimeoutException") == 25, errors
    assert_clean(errors)

    hist = await tools.log_histogram(app, query="level:3", range="3h")
    assert hist["total"] == 32 and sum(b[1] for b in hist["buckets"]) == 32, hist
    assert_clean(hist)

    top = await tools.top_values(app, "source", range="1d")
    assert top["values"] and top["values"][0]["count"] > 0
    sensitive = await tools.top_values(app, "user_email", range="1d")
    assert all(v["value"] == "[EMAIL]" for v in sensitive["values"]), sensitive

    cmp = await tools.compare_periods(app, window="15m")
    assert_clean(cmp)

    streams = await tools.list_streams(app)
    assert any(s["title"] == "Payments" for s in streams["streams"])
    in_stream = await tools.count_logs(app, query="*", range="1d", streams=["Payments"])
    assert in_stream["count"] >= 26, in_stream
    searched = await tools.search_logs(app, query="*", range="1d", streams=["Payments"], dedup_lines=False)
    assert searched["total"] in (None, in_stream["count"]), searched
    assert searched["messages"] and all(m["source"].startswith("pay") for m in searched["messages"])
    if ref:
        around = await tools.context_around(app, ref, seconds=5, scope="stream")
        assert any(m.get("anchor") for m in around["messages"])

    health = await scan.scan(app, range="1d", baseline="1h")
    assert_clean(health)
    assert not health.get("skipped"), health
    timeouts = await scan.scan(
        app,
        range="1d",
        checks=[
            {"name": "timeouts", "query": '"upstream timeout"', "severity": "high"},
            {"name": "timeouts_but_pay2", "query": '"upstream timeout"', "exclude": "source:pay-2"},
        ],
    )
    counts = {f["rule"]: f["count"] for f in timeouts["findings"]}
    assert counts == {"timeouts": 25, "timeouts_but_pay2": 16}, timeouts

    fields = await tools.list_fields(app, contains="trace")
    assert any(f.startswith("trace_id") for f in fields["fields"]), fields

    for leak in LEAKS:
        assert leak not in str(status)

    empty = await tools.count_logs(app, query="sevrity:3", range="1h")
    assert empty["count"] == 0
    with pytest.raises(QueryError, match="query"):
        await tools.search_logs(app, query="level:(3 AND", range="1h")


async def test_never_writes(app, recorder):
    await test_every_tool(app)
    assert recorder.requests
    for req in recorder.requests:
        path = urlparse(str(req.url)).path.removeprefix("/api/")
        assert req.method in ("GET", "POST"), (req.method, path)
        if req.method == "POST":
            assert path in READ_ONLY_POSTS, path
    print(f"{len(recorder.requests)} requests, methods: {sorted({r.method for r in recorder.requests})}")


async def test_root_cause_on_incident(app, recorder):
    """The bad-deploy scenario seeded 30 hours ago (see seed.py)."""
    from datetime import datetime, timedelta
    from pathlib import Path

    from graylog_mcp import rca
    from tests.fake_graylog import INCIDENT

    seeded = datetime.fromisoformat(
        Path(os.environ.get("GRAYLOG_IT_SEEDED_AT_FILE", "/tmp/graylog-it-seeded-at")).read_text().strip()
    )
    end = seeded - timedelta(hours=30)
    window = {"from_time": (end - timedelta(hours=1)).isoformat(), "to_time": end.isoformat()}

    res = await rca.root_cause(app, **window)
    print("verdict:", res["verdict"])
    assert_clean(res)
    assert res["candidates"][0]["service"] == "payment", res
    assert "1.3.9 -> 1.4.0" in res["verdict"]
    assert "connection refused" in res["first_error"]["message"]
    first = datetime.fromisoformat(res["first_error"]["ts"].replace(" ", "T"))
    onset = end - INCIDENT["onset_before_end"]
    assert timedelta(0) <= first - onset <= timedelta(seconds=15), (first, onset)

    changes = await rca.detect_changes(app, **window)
    kinds = {(c["service"], c["kind"]) for c in changes["changes"]}
    assert {("payment", "version"), ("payment", "rollout"), ("payment", "restart")} <= kinds, changes

    smap = await rca.service_map(app, **window)
    edges = {line.split(" (")[0] for line in smap["diagram"]}
    assert {"gateway -> payment", "payment -> bank-adapter", "gateway -> orders", "orders -> postgres"} <= edges

    for req in recorder.requests:
        path = urlparse(str(req.url)).path.removeprefix("/api/")
        assert req.method == "GET" or path in READ_ONLY_POSTS, (req.method, path)
