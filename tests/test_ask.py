from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from graylog_mcp.ask.pipeline import ask
from graylog_mcp.ask.understand import time_args
from graylog_mcp.config import ConfigError, parse_config
from graylog_mcp.server import build_server
from graylog_mcp.tools import App, ToolInputError
from tests.conftest import make_config
from tests.fake_graylog import PAY, FakeGraylog
from tests.fake_jev import FakeJev
from tests.test_tools_contract import LEAKS, assert_read_only


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "fake-typesafe-key")


@pytest.fixture
def make_ask_app(key):
    apps = []

    def factory(jev: FakeJev, dataset: str = "incident", **overrides):
        fake = FakeGraylog(dataset=dataset)
        if dataset == "incident":  # the flow crosses streams: payment logs to its own
            for m in fake.messages:
                if m["service"] == "payment":
                    m["streams"] = [PAY]
        app = App.create(make_config(**overrides), transport=fake.transport, jev_transport=jev.transport)
        apps.append(app)
        return app, fake

    return factory


async def test_follows_the_flow_of_the_described_error(make_ask_app):
    jev = FakeJev(relevant=("bank call failed",))
    app, fake = make_ask_app(jev)

    out = await ask(app, "why did checkout fail with a bank connection error in the last hour?")

    assert out["understood"] == {"instance": "main", "time": "last_hour", "errors_only": True, "intent": "follow_flow"}
    assert out["summary"].startswith("Understood: main, last hour, errors only.")
    (match,) = out["matches"]
    assert "bank call failed" in match["sample"] and match["score"] >= 0.9
    services = {s["service"] for s in out["flow"]["services"]}
    assert {"gateway", "payment"} <= services
    assert out["flow"]["first_error"]["service"] == "payment"
    assert "gateway" in out["summary"] and "payment" in out["summary"]
    assert out["cost"]["jev_requests"] == 2
    assert out["cost"]["graylog_requests"] <= 8
    assert_read_only(fake)


async def test_lookup_of_traces_is_one_query_not_one_per_trace(make_ask_app):
    app, fake = make_ask_app(FakeJev(relevant=("bank call failed",)))
    await ask(app, "bank connection error")
    searches = [r for r in fake.requests if r.method == "POST"]
    assert len(searches) <= 4  # candidates, traces, the timeline of one trace (+ one for the full-text retry)


async def test_asks_back_instead_of_guessing(make_ask_app):
    jev = FakeJev(confidence=0.3)
    app, fake = make_ask_app(jev)

    out = await ask(app, "Is the system ok?")

    assert "clarify" in out and "matches" not in out
    assert out["cost"] == {"graylog_requests": 0, "jev_requests": 1}
    assert fake.requests == []


async def test_says_so_when_no_error_matches(make_ask_app):
    app, _ = make_ask_app(FakeJev(relevant=("nothing like this",)))

    out = await ask(app, "the printer is on fire")

    assert out["matches"] == [] and "flow" not in out
    assert "No error in this period matches" in out["summary"]


async def test_lists_close_matches_instead_of_picking(make_ask_app):
    app, _ = make_ask_app(FakeJev(relevant=("bank call failed", "502")))

    out = await ask(app, "checkout error")

    assert len(out["matches"]) == 2 and "flow" not in out
    assert "Several errors match" in out["summary"]


async def test_error_without_trace_id_gets_a_hint(make_ask_app):
    app, _ = make_ask_app(FakeJev(relevant=("deadlock",), time="last_7_days"))

    out = await ask(app, "database deadlock last week")

    assert "deadlock" in out["matches"][0]["sample"]
    assert "no trace id" in out["hint"] and "flow" not in out


async def test_other_intents_use_the_existing_tools(make_ask_app):
    app, _ = make_ask_app(FakeJev(intent="count"))
    out = await ask(app, "how many errors?")
    assert out["result"]["count"] > 0 and out["cost"]["jev_requests"] == 1

    app, _ = make_ask_app(FakeJev(intent="error_summary"))
    assert (await ask(app, "what are the main errors?"))["result"]["groups"]

    app, _ = make_ask_app(FakeJev(intent="root_cause"))
    assert "result" in await ask(app, "what is breaking?")


async def test_trace_code_uses_the_configured_patterns(make_ask_app):
    app, _ = make_ask_app(FakeJev(intent="trace_code"), ask={"code_patterns": [r"co-\d+"]})
    out = await ask(app, "what happened to order co-12?")
    assert out["codes"] == ["co-12"] and out["traces"][0]["trace_id"] == "co-12"

    app, _ = make_ask_app(FakeJev(intent="trace_code"))
    assert "code_patterns" in (await ask(app, "what happened to order co-12?"))["hint"]


async def test_vietnamese_summary(make_ask_app):
    app, _ = make_ask_app(FakeJev(relevant=("nothing",)), ask={"language": "vi"})
    out = await ask(app, "lỗi thanh toán")
    assert out["summary"].startswith("Đã hiểu: main, 1 giờ qua, chỉ lỗi.")


async def test_nothing_unredacted_is_sent_to_typesafe(make_ask_app):
    jev = FakeJev(relevant=("calling bank api",))
    app, fake = make_ask_app(jev, dataset="basic")
    assert any(leak in json.dumps(fake.messages) for leak in LEAKS)  # the data really holds them

    await ask(app, "bank api error", instance=None)

    sent = json.dumps(jev.bodies)
    assert jev.bodies and all(leak not in sent for leak in LEAKS)


async def test_typesafe_errors_are_explained(make_ask_app):
    app, _ = make_ask_app(FakeJev(status=401))
    with pytest.raises(Exception, match="refused the API key"):
        await ask(app, "anything")


async def test_empty_question_and_missing_key(make_ask_app, monkeypatch):
    app, _ = make_ask_app(FakeJev())
    with pytest.raises(ToolInputError):
        await ask(app, "   ")
    monkeypatch.delenv("TYPESAFE_API_KEY")
    bare = App.create(make_config())
    assert bare.jev is None
    with pytest.raises(ToolInputError, match="ask is off"):
        await ask(bare, "hello")


async def test_tool_is_registered_only_with_a_key(make_ask_app, make_app, monkeypatch):
    app, _ = make_ask_app(FakeJev())
    assert "ask" in {t.name for t in await build_server(app).list_tools()}
    monkeypatch.delenv("TYPESAFE_API_KEY")
    bare, _ = make_app()
    assert "ask" not in {t.name for t in await build_server(bare).list_tools()}


def test_time_choices_become_tool_arguments():
    tz = make_config().instance(None).tz
    now = datetime(2026, 11, 6, 3, 0, tzinfo=UTC)  # 10:00 in Ho Chi Minh City
    assert time_args("last_hour", tz, now) == {"range": "1h"}
    assert time_args("today", tz, now)["from_time"].startswith("2026-11-06T00:00:00+07:00")
    assert time_args("yesterday", tz, now)["to_time"].startswith("2026-11-06T00:00:00+07:00")
    assert time_args("unspecified", tz, now) == {"range": "24h"}


def test_ask_config_is_validated(key):
    base = {"instances": {"main": {"url": "https://graylog.test", "token_env": "TEST_GRAYLOG_TOKEN"}}}
    cfg = parse_config({**base, "ask": {"language": "vi", "relevance": 0.7}}, source="test").ask
    assert cfg.enabled and cfg.language == "vi" and cfg.relevance == 0.7
    assert "fake-typesafe-key" not in repr(cfg)
    for bad in ({"language": "fr"}, {"base_url": "http://x.test"}, {"relevance": 2}, {"code_patterns": ["("]},
                {"api_key": "x"}, {"api_key_env": "fake-typesafe-key"}):  # fmt: skip
        with pytest.raises(ConfigError):
            parse_config({**base, "ask": bad}, source="test")
