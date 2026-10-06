"""Regression tests for review findings (redaction gaps, paging, budgets, edge cases)."""

from __future__ import annotations

import pytest

from graylog_mcp import tools
from graylog_mcp.config import ConfigError, parse_config
from graylog_mcp.redact import Redactor
from graylog_mcp.shaping import dumps


def _add(fake, seconds_ago=60, **fields):
    from datetime import timedelta

    ts = fake.now - timedelta(seconds=seconds_ago)
    msg = {
        "_id": fields.pop("_id", f"id-{len(fake.messages)}"),
        "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z",
        "streams": ["000000000000000000000001"],
        "source": "x",
        "level": 6,
        "message": "m",
    }
    msg.update(fields)
    fake.messages.append(msg)
    fake.messages.sort(key=lambda m: m["timestamp"], reverse=True)
    return msg


def test_numeric_values_are_masked():
    r = Redactor()
    assert r.value(4111111111111111) == "[CARD]"
    assert r.value(42) == 42 and r.value(3.5) == 3.5 and r.value(True) is True


async def test_trace_service_names_are_masked(make_app):
    app, fake = make_app()
    fake.messages = []
    _add(fake, service="bob@example.com token=abc123secret", trace_id="T1", card=4111111111111111)
    trace = await tools.trace_request(app, "T1", range="1h")
    text = dumps(trace)
    assert "bob@example.com" not in text and "abc123secret" not in text
    search = await tools.search_logs(app, query="trace_id:T1", range="1h", fields=["*"])
    assert search["messages"][0]["card"] == "[CARD]"


async def test_oversized_message_still_advances_paging(make_app):
    app, fake = make_app(limits={"max_output_chars": 2000, "max_value_chars": 1900})
    fake.messages = []
    for i in range(3):
        _add(fake, seconds_ago=10 + i, message=f"line {i} " + "x" * 1800, _id=f"big{i}")
    res = await tools.search_logs(app, range="1h", dedup_lines=False)
    assert res["messages"] and res["next_offset"] == 1 and res["returned"] == 1
    assert len(dumps(res)) <= 2600


async def test_budget_cut_without_total_does_not_skip(make_app):
    app, _ = make_app(
        "6.1.2",
        limits={"max_output_chars": 2000},
        instances={"main": {"url": "https://g.test", "token_env": "TEST_GRAYLOG_TOKEN", "message_api": "scripting"}},
    )
    res = await tools.search_logs(app, range="2h", limit=40, dedup_lines=False)
    assert res.get("total") is None and res["truncated"]
    assert res["next_offset"] == len(res["messages"])


async def test_dedup_budget_cut_resumes_at_first_unshown_group(make_app):
    app, fake = make_app(limits={"max_output_chars": 2000})
    fake.messages = []
    for i in range(20):
        _add(fake, seconds_ago=10 + i, message=f"distinct event kind {chr(65 + i)} " + "y" * 150)
    res = await tools.search_logs(app, range="1h", limit=20)
    assert res["truncated"] and res["next_offset"] == len(res["messages"])


async def test_get_message_respects_budget(make_app):
    app, fake = make_app(limits={"max_output_chars": 6000})
    fake.messages = []
    big = {f"f{i}": "z" * 3900 for i in range(12)}
    _add(fake, _id="huge", tags=["t" * 500] * 20, **big)
    res = await tools.get_message(app, "graylog_3/huge")
    assert len(dumps(res)) <= 6000 and res["truncated"]


async def test_context_around_counts_exclude_anchor(make_app):
    app, fake = make_app()
    fake.messages = []
    _add(fake, seconds_ago=12, message="before1")
    _add(fake, seconds_ago=11, message="before2")
    _add(fake, seconds_ago=10, message="anchor", _id="anc")
    _add(fake, seconds_ago=9, message="after1")
    res = await tools.context_around(app, "graylog_3/anc", seconds=30, scope="all", limit=4)
    assert [m["message"] for m in res["messages"]] == ["before1", "before2", "anchor", "after1"]
    assert res["more_before"] is False and res["more_after"] is False


async def test_compare_rechecks_when_missing_bucket_takes_a_slot(make_app):
    app, fake = make_app(limits={"max_groups": 2})
    fake.messages = []
    # baseline (16-30 min ago): Old x6, no field x5, Rare x2; current: Old x6, Rare x2
    for i in range(6):
        _add(fake, seconds_ago=1200 + i, level=3, exception_class="Old")
        _add(fake, seconds_ago=300 + i, level=3, exception_class="Old")
    for i in range(5):
        _add(fake, seconds_ago=1300 + i, level=3)
    for i in range(2):
        _add(fake, seconds_ago=1400 + i, level=3, exception_class="Rare")
        _add(fake, seconds_ago=400 + i, level=3, exception_class="Rare")
    res = await tools.compare_periods(app, window="15m", limit=1)
    rare = await tools.compare_periods(app, window="15m", limit=5)
    statuses = {g["value"]: g for g in rare["groups"]}
    assert statuses["Rare"]["baseline"] == 2 and statuses["Rare"]["status"] != "new"
    assert res["groups"]


@pytest.mark.parametrize(
    ("data", "match"),
    [
        ({"instances": {"a": {"url": "https://x", "token_env": 5}}}, "environment variable"),
        ({"http": {"port": 99999}, "instances": {"a": {"url": "https://x", "token_env": "T"}}}, "TCP port"),
        ({"redaction": {"vn_cmnd": True}, "instances": {"a": {"url": "https://x", "token_env": "T"}}}, "vn"),
        ({"redaction": {"allow": [1]}, "instances": {"a": {"url": "https://x", "token_env": "T"}}}, "allow"),
    ],
)
def test_config_type_checks(monkeypatch, data, match):
    monkeypatch.setenv("T", "t")
    with pytest.raises(ConfigError, match=match):
        parse_config(data)


async def test_instance_without_token_is_disabled_not_fatal(monkeypatch):
    from graylog_mcp.client import GraylogError
    from graylog_mcp.tools import App
    from tests.fake_graylog import FakeGraylog

    monkeypatch.setenv("STG_TOKEN", "s")
    monkeypatch.delenv("PROD_TOKEN", raising=False)
    cfg = parse_config(
        {
            "instances": {
                "staging": {"url": "https://stg.test", "token_env": "STG_TOKEN", "description": "Staging"},
                "prod": {"url": "https://prod.test", "token_env": "PROD_TOKEN", "description": "Production"},
            }
        }
    )
    assert cfg.default_instance == "staging"
    assert "PROD_TOKEN" in cfg.instance("prod").unavailable
    app = App.create(cfg, transport=FakeGraylog("6.1.2").transport)
    status = {i["name"]: i for i in (await tools.list_instances(app))["instances"]}
    assert status["staging"]["status"] == "ok" and status["staging"]["description"] == "Staging"
    assert status["prod"]["status"].startswith("not configured") and "PROD_TOKEN" in status["prod"]["status"]
    assert (await tools.count_logs(app, range="2h", instance="staging"))["count"] > 0
    with pytest.raises(GraylogError, match="PROD_TOKEN"):
        await tools.count_logs(app, range="2h", instance="prod")


def test_all_instances_without_secrets_is_fatal(monkeypatch):
    monkeypatch.delenv("A_TOKEN", raising=False)
    monkeypatch.delenv("B_PW", raising=False)
    with pytest.raises(ConfigError, match=r"A_TOKEN.*B_PW"):
        parse_config(
            {
                "instances": {
                    "a": {"url": "https://a.test", "token_env": "A_TOKEN"},
                    "b": {"url": "https://b.test", "auth": "basic", "username": "u", "password_env": "B_PW"},
                }
            }
        )


@pytest.mark.parametrize("key", ["token_env", "password_env", "username_env"])
def test_secret_typed_into_env_field_is_refused_without_echo(monkeypatch, key):
    secret = "cajvym-jibva0-maMcen"
    inst = {"url": "https://x.test", "auth": "basic", "username": "u", "password_env": "P"}
    if key == "token_env":
        inst = {"url": "https://x.test"}
    inst[key] = secret
    with pytest.raises(ConfigError) as exc:
        parse_config({"instances": {"F88-dev": inst}}, require_usable=False)
    assert "NAME of an environment variable" in str(exc.value) and secret not in str(exc.value)


def test_configfile_refuses_secret_in_env_field():
    from graylog_mcp.setup import configfile

    with pytest.raises(ConfigError, match="NAME of an environment variable") as exc:
        configfile.upsert_instance({}, "F88-dev", {"url": "https://x", "auth": "basic", "username": "u",
                                                   "password_env": "cajvym-jibva0-maMcen"})  # fmt: skip
    assert "cajvym" not in str(exc.value)
