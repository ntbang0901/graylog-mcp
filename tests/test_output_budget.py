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


@pytest.mark.parametrize(
    "text",
    ['"' * 2100, "\n" * 2100, '{"a": "b"}\n' * 800, ("x" * 74 + "\n") * 106],
    ids=["quotes", "newlines", "json-lines", "lines"],
)
async def test_get_message_trims_values_with_escapes_instead_of_omitting_them(make_app, text):
    app, fake = make_app()
    fake.messages = []
    _add(fake, _id="big", message=text, full_message="x")
    res = await tools.get_message(app, "graylog_3/big")
    assert len(dumps(res)) <= 4000
    assert res["message"]["message"] == text[: len(res["message"]["message"])]
    assert len(dumps(res)) > 3000  # the cap was used, not most of it thrown away


async def test_get_message_trims_the_message_before_dropping_short_fields(make_app):
    app, fake = make_app()
    fake.messages = []
    _add(fake, _id="big", message="M" * 3800, trace_id="abc-123", logger="acme.orders", status="500")
    res = await tools.get_message(app, "graylog_3/big")
    msg = res["message"]
    assert len(dumps(res)) <= 4000
    assert (msg["trace_id"], msg["logger"], msg["status"]) == ("abc-123", "acme.orders", "500")
    assert list(res["cut_fields"]) == ["message"]


async def test_get_message_drops_short_fields_when_trimming_is_not_enough(make_app):
    app, fake = make_app()
    fake.messages = []
    _add(fake, _id="wide", message="M" * 3000, **{f"field_{i:03}": "v" for i in range(400)})
    res = await tools.get_message(app, "graylog_3/wide")
    assert len(dumps(res)) <= 4000
    assert res["message"]["message"].startswith("MMM")  # not replaced by a placeholder


@pytest.mark.parametrize(
    "fields",
    [
        {f"kubernetes_label_{i:04}": "v" for i in range(500)},
        {f"{i}" + "n" * 2000: "v" for i in range(5)},
        {"source": "s" * 15000},
        {"level": "l" * 15000},
        {"payload": {"nested": "x" * 15000}},
    ],
    ids=["many-fields", "long-names", "long-source", "long-level", "non-string"],
)
async def test_get_message_never_exceeds_its_cap(make_app, fields):
    app, fake = make_app()
    fake.messages = []
    _add(fake, _id="big", **fields)
    res = await tools.get_message(app, "graylog_3/big")
    assert len(dumps(res)) <= 4000 and res["truncated"]


async def test_get_message_keeps_some_of_an_escape_heavy_message_while_short_fields_remain(make_app):
    app, fake = make_app()
    fake.messages = []
    _add(fake, _id="big", message='"' * 3000, **{f"field_{i:03}": "v" for i in range(120)})
    res = await tools.get_message(app, "graylog_3/big")
    assert len(dumps(res)) <= 4000 and res["message"]["message"].startswith('"')


async def test_get_message_never_cuts_ts_or_ref(make_app):
    app, fake = make_app()
    fake.messages = []
    long_id = "i" * 300
    _add(fake, _id=long_id, message="M" * 5000)
    res = await tools.get_message(app, f"graylog_3/{long_id}")
    assert res["message"]["ref"] == f"graylog_3/{long_id}" and "ts" in res["message"]
    assert "ref" not in res["cut_fields"] and len(dumps(res)) <= 4000


@pytest.mark.parametrize("value", ["\x01" * 3000, ["x" * 3000] * 40], ids=["control-chars", "list"])
async def test_context_stays_under_its_cap_with_an_oversized_anchor(make_app, value):
    app, fake = make_app()
    fake.messages = []
    _add(fake, seconds_ago=100, _id="anc", message=value)
    _add(fake, seconds_ago=99, message="near")
    res = await tools.context_around(app, "graylog_3/anc", seconds=60)
    assert len(dumps(res)) <= 8000 and res["truncated"]
    assert "cut_fields" in res and "get_message" in res["hint"]
    assert any(m["message"] == "near" for m in res["messages"])


async def test_get_message_counts_the_names_it_cannot_list(make_app):
    app, fake = make_app()
    fake.messages = []
    _add(fake, _id="wide", **{f"kubernetes_label_{i:04}": "v" for i in range(500)})
    res = await tools.get_message(app, "graylog_3/wide")
    kept = [k for k in res["message"] if k.startswith("kubernetes_label")]
    assert len(kept) + len(res["cut_fields"]) + res["cut_more"] == 500


async def test_context_keeps_shorter_lines_after_one_that_does_not_fit(make_app):
    app, fake = make_app()
    fake.messages = []
    _add(fake, seconds_ago=100, _id="anc", message="anchor")
    for i in range(1, 7):
        _add(fake, seconds_ago=100 + i, message=f"long {i} " + "L" * 3000)
    for i in range(7, 17):
        _add(fake, seconds_ago=100 + i, message=f"short{i}")
    res = await tools.context_around(app, "graylog_3/anc", seconds=60)
    texts = " ".join(m["message"] for m in res["messages"])
    assert len(dumps(res)) <= 8000 and res["truncated"]
    assert "long 6" not in texts and "short7" in texts and "short8" in texts


async def test_context_keeps_lines_when_the_anchor_itself_does_not_fit_again(make_app):
    app, fake = make_app(limits={"context_output_chars": 2000})
    fake.messages = []
    _add(fake, seconds_ago=100, _id="anc", message="A" * 900)
    for i in range(1, 6):
        _add(fake, seconds_ago=100 - i, message=f"near{i}")
    res = await tools.context_around(app, "graylog_3/anc", seconds=60)
    assert any(m["message"].startswith("near") for m in res["messages"])


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
