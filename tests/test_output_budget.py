"""What one call costs the model: the caps per tool, what get_message cuts, and the size of the tool list."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from graylog_mcp import tools, usage
from graylog_mcp.config import ConfigError
from graylog_mcp.server import build_server
from graylog_mcp.shaping import dumps
from tests.conftest import make_config


def _add(fake, seconds_ago=5, **fields):
    ts = datetime.now(UTC) - timedelta(seconds=seconds_ago)
    base = {
        "_id": f"id-{len(fake.messages)}",
        "index": "graylog_3",
        "timestamp": ts.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "source": "api",
        "level": 3,
        "message": "m",
    }
    fake.messages.append({**base, **fields})


async def test_search_stays_under_its_cap_and_pages(make_app):
    app, fake = make_app()
    fake.messages = []
    for i in range(50):
        _add(fake, seconds_ago=i + 1, message=f"line {chr(97 + i % 26) * 40} " + "x" * 1000 + chr(65 + i % 26) * 20)
    res = await tools.search_logs(app, query="*", range="1h", dedup_lines=False)
    assert len(dumps(res)) <= 12000 and res["truncated"]
    assert res["next_offset"] == len(res["messages"])


async def test_max_output_chars_still_wins_when_lower(make_app):
    app, fake = make_app(limits={"max_output_chars": 3000})
    fake.messages = []
    _add(fake, _id="big", message="y" * 9000)
    assert len(dumps(await tools.get_message(app, "graylog_3/big"))) <= 3000


async def test_get_message_cuts_long_values_and_names_them(make_app):
    app, fake = make_app()
    fake.messages = []
    _add(fake, _id="big", message="M" * 3000, payload="p" * 15000, trace_id="abc-123")
    res = await tools.get_message(app, "graylog_3/big")
    msg = res["message"]
    assert len(dumps(res)) <= 4000 and res["truncated"]
    assert res["cut_fields"]["payload"] > 15000
    assert msg["trace_id"] == "abc-123" and msg["source"] == "api"  # short fields stay
    assert len(msg["message"]) > len(msg["payload"])  # the message is cut last

    asked = await tools.get_message(app, "graylog_3/big", fields=["payload"])
    assert asked["message"]["payload"] == "p" * 15000 and "cut_fields" not in asked


async def test_get_message_small_is_untouched(make_app):
    app, fake = make_app()
    fake.messages = []
    _add(fake, _id="small", message="short")
    res = await tools.get_message(app, "graylog_3/small")
    assert res["message"]["message"] == "short" and "truncated" not in res and "cut_fields" not in res


async def test_get_message_with_many_short_fields_still_fits(make_app):
    app, fake = make_app()
    fake.messages = []
    _add(fake, _id="wide", **{f"field_{i}": "v" * 150 for i in range(60)})
    res = await tools.get_message(app, "graylog_3/wide")
    assert len(dumps(res)) <= 4000
    assert all(name in res["cut_fields"] or name in res["message"] for name in (f"field_{i}" for i in range(60)))


async def test_context_keeps_both_sides_of_the_anchor_when_cut(make_app):
    app, fake = make_app()
    fake.messages = []
    for i in range(1, 16):
        _add(fake, seconds_ago=100 + i, message=f"before {i} " + "b" * 800)
        _add(fake, seconds_ago=100 - i, message=f"after {i} " + "a" * 800)
    _add(fake, seconds_ago=100, _id="anc", message="anchor")
    res = await tools.context_around(app, "graylog_3/anc", seconds=60)
    texts = [m["message"] for m in res["messages"]]
    assert len(dumps(res)) <= 8000 and res["truncated"]
    assert any(t.startswith("before") for t in texts) and any(t.startswith("after") for t in texts)
    assert "after 1 " in " ".join(texts)
    assert [m["dt"] for m in res["messages"]] == sorted((m["dt"] for m in res["messages"]), key=lambda d: int(d[:-2]))


def test_tool_caps_cannot_be_set_too_low():
    with pytest.raises(ConfigError, match="search_output_chars"):
        make_config(limits={"search_output_chars": 500})


async def test_usage_records_requests_and_what_shaped_the_result(make_app):
    app, _ = make_app("6.1.2")
    server = build_server(app)
    await server.call_tool("search_logs", {"query": "*", "range": "1h", "limit": 5, "fields": ["message"]})
    entry = json.loads(usage.path().read_text(encoding="utf-8").splitlines()[-1])
    assert entry["requests"] >= 1
    assert entry["params"]["range"] == "1h" and entry["params"]["limit"] == "5" and "offset" not in entry["params"]
    assert "message" in entry["params"]["fields"] and "range" not in entry.get("args", {})
    assert entry["returned"] == 5


async def test_tool_list_stays_small(make_app):
    app, _ = make_app()
    listed = await build_server(app).list_tools()
    size = sum(len(json.dumps(t.model_dump(exclude_none=True, by_alias=True))) for t in listed)
    assert size <= 30000, f"tool list grew to {size} characters; every session pays for it"
