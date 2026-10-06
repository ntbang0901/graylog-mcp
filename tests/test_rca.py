"""Root cause analysis: pure helpers and the three tools against the incident scenario."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from graylog_mcp import rca
from graylog_mcp.backends.base import AggResult, AggRow
from graylog_mcp.shaping import dumps
from tests.fake_graylog import INCIDENT
from tests.test_tools_contract import assert_clean, assert_read_only

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def ts(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def ms(dt: datetime) -> float:
    return dt.timestamp() * 1000


# --------------------------------------------------------------------------- detectors


def test_detect_rise_needs_confirmation():
    base = [0, 1, 0, 0, 1, 0]
    assert rca.detect_rise(base, [(0, 0), (60, 8), (120, 0), (180, 0)], min_abs=3) is None  # lone blip
    assert rca.detect_rise(base, [(0, 0), (60, 8), (120, 9), (180, 7)], min_abs=3) == (60, 9)


def test_detect_rise_respects_noisy_baseline():
    base = [10, 12, 9, 11, 30, 10, 11]
    assert rca.detect_rise(base, [(0, 25), (60, 28)], min_abs=3) is None
    assert rca.detect_rise(base, [(0, 60), (60, 70)], min_abs=3) == (0, 70)


def test_detect_drop():
    assert rca.detect_drop([6, 6, 6, 7, 5], [(0, 6), (60, 0), (120, 0), (180, 1)]) == (60, 0)
    assert rca.detect_drop([6, 6, 6], [(0, 6), (60, 0), (120, 6)]) is None  # not sustained
    assert rca.detect_drop([1, 2, 1], [(0, 0), (60, 0)]) is None  # too quiet to tell


# --------------------------------------------------------------------------- service map


def test_trace_edges_nested_calls():
    steps = [
        (ts(0), "gateway", False),
        (ts(0.02), "payment", False),
        (ts(0.12), "bank", False),
        (ts(0.3), "payment", False),
        (ts(0.35), "gateway", False),
    ]
    entry, edges = rca.trace_edges(steps)
    assert entry == "gateway"
    assert {(a, b) for a, b, _, _ in edges} == {("gateway", "payment"), ("payment", "bank")}
    durations = {(a, b): d for a, b, d, _ in edges}
    assert durations[("payment", "bank")] == 180 and durations[("gateway", "payment")] == 330


def test_trace_edges_failure_without_return():
    steps = [(ts(0), "gateway", False), (ts(0.02), "payment", False), (ts(2.02), "payment", True)]
    _, edges = rca.trace_edges(steps)
    assert edges == [("gateway", "payment", 2000, True)]


def test_build_graph_counts():
    traces = {
        "a": [(ts(0), "g", False), (ts(1), "p", True), (ts(2), "g", True)],
        "b": [(ts(0), "g", False), (ts(1), "p", False), (ts(1.5), "g", False)],
        "c": [(ts(0), "g", False)],  # single service: ignored
    }
    graph = rca.build_graph(traces)
    assert graph.traces == 2
    edge = graph.edges[("g", "p")]
    assert edge.traces == 2 and edge.errors == 1
    assert graph.nodes["g"]["entry"] == 2 and graph.callees("g") == {"p"} and graph.callers("p") == {"g"}


# --------------------------------------------------------------------------- changes


def _row(keys, first, last, count=10):
    return AggRow(keys=keys, values={"count()": count, "min(timestamp)": ms(first), "max(timestamp)": ms(last)})


def test_version_changes():
    window = ts(3600)
    agg = AggResult(
        rows=[
            _row(["payment", "1.3.9"], ts(0), ts(4000)),
            _row(["payment", "1.4.0"], ts(4100), ts(7000)),
            _row(["gateway", "5.2.0"], ts(0), ts(7000)),
            _row(["orders", None], ts(0), ts(7000)),
        ]
    )
    changes = rca.version_changes(agg, window)
    assert len(changes) == 1
    c = changes[0]
    assert (c.service, c.kind, c.detail["from"], c.detail["to"], c.at) == (
        "payment",
        "version",
        "1.3.9",
        "1.4.0",
        ts(4100),
    )


def test_rollouts_and_silent_services():
    window_start, window_end = ts(3600), ts(7200)
    agg = AggResult(
        rows=[
            _row(["payment", "pay-1"], ts(0), ts(4000)),
            _row(["payment", "pay-3"], ts(4005), ts(7100)),
            _row(["bank", "bank-1"], ts(0), ts(5000)),  # whole service went silent: not a change
            _row(["cache", "c-1"], ts(0), ts(7100)),
            _row(["cache", "c-2"], ts(0), ts(4500)),  # one of two hosts lost
            _row(["search", "s-1"], ts(5000), ts(7100)),  # brand new service
        ]
    )
    changes = {c.service: c for c in rca.rollouts(agg, window_start, window_end, timedelta(minutes=5))}
    assert changes["payment"].kind == "rollout" and changes["payment"].detail["new_sources"] == ["pay-3"]
    assert "bank" not in changes
    assert changes["cache"].kind == "hosts_gone"
    assert changes["search"].kind == "new_service"


# --------------------------------------------------------------------------- ranking


def test_rank_prefers_changed_service_whose_callers_fail_later():
    secs = 60
    onsets = {
        "payment": [rca.Onset("payment", "errors", 0, 0, 6, at=datetime.fromtimestamp(10, UTC))],
        "gateway": [rca.Onset("gateway", "errors", 0, 0, 6, at=datetime.fromtimestamp(10.05, UTC))],
        "bank": [rca.Onset("bank", "traffic_drop", 0, 6, 0)],
    }
    graph = rca.Graph(edges={("gateway", "payment"): rca.EdgeStats(), ("payment", "bank"): rca.EdgeStats()})
    changes = [rca.Change(datetime.fromtimestamp(-170, UTC), "payment", "version", {"from": "1", "to": "2"})]
    ranked = rca.rank(onsets, changes, graph, secs, str)
    assert ranked[0].service == "payment"
    gateway = next(c for c in ranked if c.service == "gateway")
    assert any("dependency payment failed earlier" in r for r in gateway.reasons)
    assert ranked[0].score - ranked[1].score >= 3


def test_rank_without_change_still_orders_by_onset():
    onsets = {
        "db": [rca.Onset("db", "latency", 0, 5, 500)],
        "api": [rca.Onset("api", "errors", 60, 0, 9, at=datetime.fromtimestamp(70, UTC))],
    }
    graph = rca.Graph(edges={("api", "db"): rca.EdgeStats()})
    ranked = rca.rank(onsets, [], graph, 60, str)
    assert ranked[0].service == "db"


# --------------------------------------------------------------------------- tools on the incident scenario


async def test_root_cause_finds_bad_deploy(make_app, version):
    app, fake = make_app(version, dataset="incident")
    res = await rca.root_cause(app, range="1h")
    assert_clean(res)
    assert_read_only(fake)
    top = res["candidates"][0]
    assert top["service"] == "payment", res["candidates"]
    assert "confidence high" in res["verdict"]
    assert "1.3.9 -> 1.4.0" in res["verdict"] and "pay-3" in res["verdict"]
    assert "connection refused" in res["first_error"]["message"]
    onset = fake.now - INCIDENT["onset_before_end"]
    first = datetime.fromisoformat(res["first_error"]["ts"].replace(" ", "T"))
    assert timedelta(0) <= first - onset <= timedelta(seconds=15)
    gateway = next(c for c in res["candidates"] if c["service"] == "gateway")
    assert any("dependency payment failed earlier" in r for r in gateway["reasons"])
    events = " ".join(e["event"] for e in res["timeline"])
    assert "traffic fell" in events and "first error" in events
    assert any(line.startswith("gateway -> payment") for line in res["service_map"])
    assert res["next_steps"]


async def test_root_cause_quiet_period(make_app):
    app, fake = make_app("6.1.2", dataset="incident")
    end = fake.now - timedelta(minutes=20)
    res = await rca.root_cause(app, from_time=(end - timedelta(hours=1)).isoformat(), to_time=end.isoformat())
    assert "no service deviates" in res["verdict"]


async def test_detect_changes(make_app, version):
    app, _ = make_app(version, dataset="incident")
    res = await rca.detect_changes(app, range="1h")
    assert_clean(res)
    kinds = {(c["service"], c["kind"]) for c in res["changes"]}
    assert {("payment", "version"), ("payment", "rollout"), ("payment", "restart")} <= kinds
    version_change = next(c for c in res["changes"] if c["kind"] == "version")
    assert (version_change["from"], version_change["to"], version_change["field"]) == ("1.3.9", "1.4.0", "app_version")
    assert not any(c["service"] == "bank-adapter" for c in res["changes"])


async def test_service_map(make_app, version):
    app, _ = make_app(version, dataset="incident")
    res = await rca.service_map(app, range="1h")
    assert_clean(res)
    edges = {line.split(" (")[0] for line in res["diagram"]}
    assert edges == {"gateway -> payment", "payment -> bank-adapter", "gateway -> orders", "orders -> postgres"}
    assert res["entry_points"] == ["gateway"]
    gateway_payment = next(line for line in res["diagram"] if line.startswith("gateway -> payment"))
    assert "errors" in gateway_payment


async def test_service_map_without_trace_fields(make_app):
    app, _ = make_app(investigation={"trace_fields": ["nope_trace"]})
    res = await rca.service_map(app)
    assert "configure trace_fields" in res["hint"]


async def test_root_cause_via_mcp_and_preset(make_app):
    from graylog_mcp import tools
    from graylog_mcp.server import build_server

    app, _ = make_app(
        "5.2.4",
        dataset="incident",
        presets={"triage": {"tool": "root_cause", "args": {"range": "1h"}, "description": "what broke"}},
    )
    server = build_server(app)
    names = {t.name for t in await server.list_tools()}
    assert {"root_cause", "detect_changes", "service_map"} <= names
    result = await server.call_tool("root_cause", {"range": "1h"})
    assert "Most likely origin: payment" in result.content[0].text
    preset = await tools.run_preset(app, "triage")
    assert preset["candidates"][0]["service"] == "payment"
    assert len(dumps(preset)) < 30_000
