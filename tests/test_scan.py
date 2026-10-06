"""scan / list_scan_rules: built-in rules, configured and ad hoc rules, against every emulated version."""

from __future__ import annotations

import json

import pytest

from graylog_mcp import scan, tools
from graylog_mcp.config import ConfigError, parse_scan_rule
from graylog_mcp.scanrules import BUILTIN_SCAN_RULES
from graylog_mcp.tools import ToolInputError
from tests.conftest import make_config
from tests.test_tools_contract import assert_clean, assert_read_only


def by_rule(result: dict) -> dict[str, dict]:
    return {f["rule"]: f for f in result["findings"]}


async def test_default_scan_on_every_version(make_app, version):
    app, fake = make_app(version)
    res = await scan.scan(app, range="1h")
    assert_clean(res)
    assert_read_only(fake)
    found = by_rule(res)
    # 25 timeouts in the last 10 minutes, none before: a new connectivity problem
    assert found["connectivity"]["count"] == 25 and found["connectivity"]["trend"] == "new"
    assert found["error_spike"]["trend"] == "rising"
    assert "upstream timeout" in found["connectivity"]["sample"]["message"]
    new_types = found["new_error_types"]
    assert new_types["groups"][0] == {
        **new_types["groups"][0],
        "value": "java.net.SocketTimeoutException",
        "status": "new",
    }
    # the steady deadlocks (same rate before and during the window) do not fire
    quiet = {q["rule"]: q for q in res["quiet"]}
    assert quiet["database"]["trend"] == "steady"
    assert {"crash", "resource_exhaustion"} <= set(quiet)
    # most severe first, and the verdict names the top finding
    assert res["verdict"].startswith(f"{len(found)} of {len(BUILTIN_SCAN_RULES)} rules fired")
    # one round of requests per rule, not one per message
    assert len(fake.requests) < 60


async def test_sample_shown_once(make_app):
    app, _ = make_app()
    res = await scan.scan(app, range="1h")
    refs = [f["sample"]["ref"] for f in res["findings"] if "ref" in (f.get("sample") or {})]
    assert len(refs) == len(set(refs))
    assert any("same_as" in (f.get("sample") or {}) for f in res["findings"])


async def test_select_by_name_tag_and_severity(make_app):
    app, fake = make_app()
    res = await scan.scan(app, rules=["database"])
    assert res["checked"] == 1 and not res["findings"]
    res = await scan.scan(app, rules=["dependencies"])  # a tag
    assert {q["rule"] for q in res["quiet"]} | set(by_rule(res)) == {"connectivity", "database"}
    before = len(fake.requests)
    res = await scan.scan(app, min_severity="critical")
    assert res["checked"] == 2 and res["verdict"].startswith("nothing abnormal")
    assert "left_out" in res
    assert len(fake.requests) - before <= 4  # two counts per critical rule, nothing else
    with pytest.raises(ToolInputError, match="unknown rule or tag"):
        await scan.scan(app, rules=["nope"])
    with pytest.raises(ToolInputError, match="min_severity"):
        await scan.scan(app, min_severity="urgent")


async def test_ad_hoc_checks(make_app):
    app, _ = make_app()
    res = await scan.scan(
        app,
        range="1h",
        checks=[
            {"name": "payment_failed", "query": 'message:"payment failed"', "severity": "critical"},
            {"name": "orders_errors", "query": "service:orders", "errors_only": True, "growth": 2, "min_count": 1},
        ],
    )
    assert res["checked"] == 2  # only the checks, not the built-in rules
    found = by_rule(res)
    assert found["payment_failed"]["why"] == ["1 matches, above the threshold of 0"]
    assert res["findings"][0]["rule"] == "payment_failed"  # critical first
    assert "orders_errors" not in found  # steady
    both = await scan.scan(app, rules=["crash"], checks=[{"query": "pay-1"}])
    assert both["checked"] == 2
    asked = await scan.scan(app, rules=["all"], min_severity="critical", checks=[{"query": "pay-1", "severity": "low"}])
    assert asked["checked"] == 3  # the two critical rules, and the check even though it is low
    with pytest.raises(ToolInputError, match="growth"):
        await scan.scan(app, checks=[{"query": "x", "growth": 0.5}])
    with pytest.raises(ToolInputError, match="unknown key"):
        await scan.scan(app, checks=[{"query": "x", "limit": 3}])


async def test_configured_rules_override_disable_exclude(make_app):
    app, _ = make_app(
        scan={
            "disable": ["auth_failures"],
            "exclude": "source:pay-2",
            "rules": {
                "connectivity": {"min_count": 100},  # override one key of a built-in
                "card_declined": {"query": "declined", "severity": "high", "tags": ["payment"]},
                "kafka_lag": {"query": "lag", "requires": ["consumer_lag"]},
                "other_system": {"query": "boom", "instances": ["erp"]},
            },
        }
    )
    rules = app.config.scan.rules
    assert "auth_failures" not in rules
    assert rules["connectivity"].min_count == 100 and rules["connectivity"].growth == 3.0
    assert rules["connectivity"].builtin and not rules["card_declined"].builtin
    res = await scan.scan(app, range="1h")
    found = by_rule(res)
    assert "connectivity" not in found
    assert "other_system" not in {q["rule"] for q in res["quiet"]} | set(found)
    assert res["skipped"] == [{"rule": "kafka_lag", "skipped": "none of the fields consumer_lag exist in these logs"}]
    # exclude: pay-2 timeouts (every third one) are dropped from every rule
    spike = await scan.scan(app, rules=["error_spike"])
    assert "NOT ((source:pay-2))" in spike["findings"][0]["query"]
    timeouts = await scan.scan(app, checks=[{"query": '"upstream timeout"', "exclude": "source:db-1"}])
    assert timeouts["findings"][0]["count"] == 16  # 25 minus the 9 on pay-2
    listed = scan.list_scan_rules(app)
    names = [r["name"] for r in listed["rules"]]
    assert names.index("crash") < names.index("card_declined")  # by severity
    assert listed["disabled"] == ["auth_failures"] and listed["exclude"] == "source:pay-2"
    assert "other_system" not in [r["name"] for r in scan.list_scan_rules(app, instance="main")["rules"]]


def test_rule_validation():
    with pytest.raises(ConfigError, match="new_groups needs group_by"):
        parse_scan_rule("r", {"query": "x", "new_groups": True})
    with pytest.raises(ConfigError, match="fires on all traffic"):
        parse_scan_rule("r", {"query": "*"})
    with pytest.raises(ConfigError, match="severity"):
        parse_scan_rule("r", {"query": "x", "severity": "urgent"})
    with pytest.raises(ConfigError, match="threshold"):
        parse_scan_rule("r", {"query": "x", "threshold": -1})
    with pytest.raises(ConfigError, match="baseline"):
        parse_scan_rule("r", {"query": "x", "baseline": "soon"})
    assert parse_scan_rule("r", {"query": "x"}).threshold == 0  # a plain query fires on any match
    assert parse_scan_rule("r", {"errors_only": True, "growth": 2}).threshold is None
    with pytest.raises(ConfigError, match=r"scan\.disable: unknown rule"):
        make_config(scan={"disable": ["nope"]})
    with pytest.raises(ConfigError, match=r"scan: unknown key"):
        make_config(scan={"rule": {}})
    for name, data in BUILTIN_SCAN_RULES.items():
        parse_scan_rule(name, data)  # every built-in is a valid rule


async def test_scan_via_mcp_and_preset(make_app):
    from graylog_mcp.server import build_server

    app, _ = make_app(presets={"health": {"tool": "scan", "args": {"min_severity": "high"}}})
    server = build_server(app)
    names = {t.name for t in await server.list_tools()}
    assert {"scan", "list_scan_rules"} <= names
    result = await server.call_tool("scan", {"range": "1h", "rules": ["error_spike", "crash"]})
    payload = json.loads(result.content[0].text)
    assert set(by_rule(payload)) == {"error_spike"} and payload["checked"] == 2
    preset = await tools.run_preset(app, "health")
    assert preset["tool"] == "scan" and "auth_failures" not in {q["rule"] for q in preset["quiet"]}
    prompts = await server.list_prompts()
    assert [p.name for p in prompts] == ["scan"]
    got = await server.get_prompt("scan", {"target": "payment timeouts", "range": "2h"})
    assert "last 2h" in got.messages[0].content.text and "payment timeouts" in got.messages[0].content.text
