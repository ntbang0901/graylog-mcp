"""The admin page's log explorer API: dashboard, messages, traces, one trace."""

from __future__ import annotations

import httpx
import pytest

from graylog_mcp import usage
from graylog_mcp.usage_store import store
from tests.fake_graylog import FakeGraylog
from tests.test_regressions import _add

CARD = "4111 1111 1111 1111"
VERSIONS = ["4.3.15+1234567", "6.1.2", "7.0.1"]
Q = {"q": "source:acme-api", "range": "1h"}


def _seed(fake: FakeGraylog) -> None:
    def add(ago: int, **fields) -> None:
        _add(fake, seconds_ago=ago, source="acme-api", **fields)

    pay = [(200, 100), (200, 200), (200, 300), (200, 400), (404, 1500), (503, 4000)]
    for i, (status, took) in enumerate(pay):
        add(600 - i, service="payment", route="/pay", http_status=status, took_ms=took,
            trace_id="t1" if i < 3 else "t2", level=3 if status == 503 else 6)  # fmt: skip
    for i in range(3):
        add(60 - i, service="orders", route="/orders", http_status=200, took_ms=50, trace_id="t3")
    add(599, service="payment", trace_id="t1", message=f"charging card {CARD} password=fake-secret")


@pytest.fixture(params=VERSIONS)
def explorer(request, tmp_path):
    from graylog_mcp.admin.app import AdminState, build_app

    fake = FakeGraylog(request.param)
    _seed(fake)
    cfg = tmp_path / ".graylog-mcp.toml"
    cfg.write_text('[instances.main]\nurl = "https://graylog.test"\ntoken_env = "TEST_GRAYLOG_TOKEN"\n', "utf-8")
    state = AdminState(tmp_path, cfg, "tok", transport=fake.transport, allowed_hosts=("testserver",))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(state)), base_url="http://testserver",
                               headers={"X-Admin-Token": "tok"})  # fmt: skip
    return client, fake


def _clean(response: httpx.Response) -> None:
    assert CARD not in response.text and "fake-secret" not in response.text


async def test_the_dashboard_counts_every_api_from_aggregations(explorer):
    client, _ = explorer
    async with client:
        response = await client.get("/api/explorer/dashboard", params=Q)
    assert response.status_code == 200, response.text
    _clean(response)
    d = response.json()
    assert d["total"] == 10 and d["fields"]["endpoint"] == "route" and d["fields"]["status"] == "http_status"
    assert {a["app"]: (a["count"], a["business_errors"], a["technical_errors"]) for a in d["apps"]} == {
        "payment": (7, 1, 1), "orders": (3, 0, 0)
    }  # fmt: skip
    assert d["status"] == {"2xx": 7, "4xx": 1, "5xx": 1, "none": 1}
    assert sum(b["count"] for b in d["histogram"]["buckets"]) == 10
    assert sum(b["errors"] for b in d["histogram"]["buckets"]) == 1
    rows = {r["endpoint"]: r for r in d["endpoints"]["rows"]}
    pay = rows["/pay"]
    assert (pay["count"], pay["business_errors"], pay["technical_errors"], pay["error_pct"]) == (6, 1, 1, 33.3)
    assert pay["latency_ms"]["p50"] == 300 and pay["latency_ms"]["p95"] == 4000
    assert pay["slow"] == {"1000": 2, "3000": 1, "10000": 0}
    assert rows["/orders"]["error_pct"] == 0 and d["endpoints"]["more"] is False
    assert d["error_queries"]["business"] == "http_status:[400 TO 499]"


async def test_a_page_of_messages_says_how_many_match(explorer):
    client, _ = explorer
    async with client:
        page = await client.get("/api/explorer/messages", params={**Q, "limit": 2, "offset": 2})
        capped = await client.get("/api/explorer/messages", params={**Q, "limit": 10000})
        too_far = await client.get("/api/explorer/messages", params={**Q, "offset": 9950})
    body = page.json()
    assert (body["total"], body["offset"], len(body["messages"])) == (10, 2, 2)
    assert capped.json()["limit"] == 500 and len(capped.json()["messages"]) == 10
    _clean(capped)
    assert too_far.status_code == 400 and "10000" in too_far.json()["error"]


async def test_traces_newest_first_and_one_trace_in_order(explorer):
    client, _ = explorer
    async with client:
        listing = (await client.get("/api/explorer/traces", params=Q)).json()
        one = await client.get("/api/explorer/trace/t1", params={"range": "1h"})
    assert [t["trace"] for t in listing["traces"]] == ["t3", "t2", "t1"]
    t1 = next(t for t in listing["traces"] if t["trace"] == "t1")
    assert t1["count"] == 4 and t1["duration_ms"] == 2000 and t1["services"] == ["payment"]
    assert t1["first_endpoint"] == "/pay" and t1["worst_level"] == 6
    assert next(t for t in listing["traces"] if t["trace"] == "t2")["worst_level"] == 3
    _clean(one)
    detail = one.json()
    assert detail["total"] == 4 and detail["duration_ms"] == 2000
    assert [m["offset_ms"] for m in detail["messages"]] == [0, 1000, 1000, 2000]


async def test_bad_requests_are_refused_and_reads_are_recorded_apart(explorer):
    client, _ = explorer
    async with client:
        unknown = await client.get("/api/explorer/dashboard", params={**Q, "instance": "nope"})
        bad_query = await client.get("/api/explorer/messages", params={"q": "level:(3", "range": "1h"})
        bad_number = await client.get("/api/explorer/messages", params={"limit": "many"})
        await client.get("/api/explorer/traces", params=Q)
        refreshed = await client.get("/api/explorer/dashboard", params={**Q, "refresh": "1"})
    assert refreshed.status_code == 200 and refreshed.json()["total"] == 10
    assert not [c for c in store().read(0, 2e9) if c.get("tool") == "explorer_dashboard" and c.get("ok")]
    assert unknown.status_code == 400 and "nope" in unknown.json()["error"]
    assert bad_query.status_code == 400
    assert bad_number.status_code == 400 and "limit" in bad_number.json()["error"]
    (call,) = [c for c in store().read(0, 2e9) if c.get("tool") == "explorer_traces"]
    assert call["source"] == "explorer" and call["conversation"] == "explorer"
    assert call["args"]["query"] == "source:acme-api"
    assert "explorer_traces" not in {t["tool"] for t in usage.summarize(24)["tools"]}  # not one of the model's
