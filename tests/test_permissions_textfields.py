"""Roles that allow only some search APIs, and fields Graylog cannot aggregate (full-text ``message``)."""

from __future__ import annotations

import pytest

from graylog_mcp import tools
from graylog_mcp.client import PermissionDenied
from tests.test_regressions import _add

VERSIONS = ["5.0.13+083613e", "6.1.2"]


@pytest.mark.parametrize("version", VERSIONS)
async def test_search_falls_back_when_universal_search_is_refused(make_app, version):
    app, fake = make_app(version)
    fake.forbidden.add("search/universal/absolute")
    out = await tools.search_logs(app, query="*", range="2h", limit=5)
    assert out["messages"]
    gl = app.gl(None)
    assert gl.message_apis[0] != "universal" and gl.message_apis[-1] == "universal"  # kept as a last resort
    before = len(fake.requests)
    await tools.search_logs(app, query="*", range="2h", limit=5)
    paths = [r.url.path for r in fake.requests[before:]]
    assert "/api/search/universal/absolute" not in paths  # the refused API is not retried first


async def test_every_search_api_refused(make_app):
    app, fake = make_app("5.0.13+083613e")
    fake.forbidden.update({"search/universal/absolute", "views/search/sync"})
    with pytest.raises(PermissionDenied, match=r"permission denied \(403\).*also refused: views"):
        await tools.search_logs(app, query="*", range="1h")


async def test_aggregation_falls_back_when_scripting_is_refused(make_app):
    app, fake = make_app("6.1.2")
    fake.forbidden.add("search/aggregate")
    out = await tools.top_values(app, "source", range="2h")
    assert out["values"] and "method" not in out


@pytest.mark.parametrize("version", VERSIONS)
async def test_top_values_of_message_is_sampled(make_app, version):
    app, fake = make_app(version)
    for i in range(12):
        _add(fake, seconds_ago=30 + i, source="mdm", level=3, message=f"Rule sync failed for tenant {1000 + i}")
    for i in range(3):
        _add(fake, seconds_ago=60 + i, source="mdm", level=3, message=f"Timeout calling DBB after {200 + i} ms")
    out = await tools.top_values(app, "message", query="source:mdm", range="15m", limit=5)
    assert out["method"] == "sampled" and out["sampled"] == 15 and "full-text" in out["note"]
    top = out["values"][0]
    assert top["value"] == "Rule sync failed for tenant <n>" and top["count"] == 12 and top["pct"] == 80.0
    assert top["example"].startswith("Rule sync failed for tenant 10")
    assert out["values"][1]["count"] == 3 and out["other"] == 0


async def test_error_summary_grouped_by_message_is_sampled(make_app):
    app, fake = make_app("6.1.2")
    for i in range(4):
        _add(fake, seconds_ago=30 + i, source="mdm", level=3, message=f"Rule sync failed for tenant {i}")
    out = await tools.error_summary(app, group_by="message", query="source:mdm", range="15m")
    assert out["method"] == "sampled"
    group = out["groups"][0]
    assert group["count"] == 4 and group["first"] and group["last"] and "Rule sync failed" in group["sample"]["message"]


async def test_other_aggregation_errors_still_raise(make_app):
    app, _ = make_app("6.1.2")
    with pytest.raises(Exception, match=r"(?i)query"):
        await tools.top_values(app, "source", query="level:(3", range="1h")
