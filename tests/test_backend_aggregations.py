"""Aggregations the log explorer needs: percentiles, groups ordered by a metric, two-field groups."""

from __future__ import annotations

from datetime import timedelta

import pytest

from graylog_mcp.backends.base import COUNT, Metric, normalize_value
from graylog_mcp.timerange import TimeRange
from tests.conftest import VERSIONS
from tests.test_regressions import _add

P50, P95 = Metric("percentile", "took_ms", 50), Metric("percentile", "took_ms", 95)
NEWEST = Metric("max", "timestamp", sort="desc")


def _window(fake) -> TimeRange:
    return TimeRange(fake.now - timedelta(hours=1), fake.now + timedelta(seconds=1), "1h")


def _seed(fake) -> None:
    for i in range(10):  # took_ms 100 .. 1000
        _add(fake, seconds_ago=600 + i, source="acme-api", endpoint="/pay", took_ms=100 * (i + 1),
             status=500 if i >= 8 else 200, trace_id="t-old")  # fmt: skip
    for i in range(3):
        _add(fake, seconds_ago=60 + i, source="acme-api", endpoint="/orders", status=200, trace_id="t-new")
    _add(fake, seconds_ago=300, source="acme-api", endpoint="/orders", status=404, trace_id="t-mid")


@pytest.mark.parametrize("version", VERSIONS)
async def test_percentiles_of_one_field_each_keep_their_own_value(make_app, version):
    app, fake = make_app(version)
    _seed(fake)
    result = await app.gl(None).aggregate("source:acme-api", _window(fake), (), ["endpoint"], 10, [COUNT, P50, P95])
    rows = {r.keys[0]: r.values for r in result.rows}
    assert rows["/pay"] == {"count()": 10, "percentile(took_ms,50)": 500, "percentile(took_ms,95)": 1000}
    # Graylog answers "NaN" for a percentile over no values
    assert rows["/orders"]["percentile(took_ms,50)"] is None


@pytest.mark.parametrize("version", VERSIONS)
async def test_groups_can_be_ordered_by_their_newest_message(make_app, version):
    app, fake = make_app(version)
    _seed(fake)
    result = await app.gl(None).aggregate("source:acme-api", _window(fake), (), ["trace_id"], 2, [COUNT, NEWEST])
    assert [r.keys[0] for r in result.rows] == ["t-new", "t-mid"]  # by count it would be t-old first


@pytest.mark.parametrize("version", VERSIONS)
async def test_two_field_groups_count_each_pair(make_app, version):
    app, fake = make_app(version)
    _seed(fake)
    result = await app.gl(None).aggregate("source:acme-api", _window(fake), (), ["endpoint", "status"], 10, [COUNT])
    pairs = {tuple(str(k) for k in r.keys): r.values["count()"] for r in result.rows}
    assert pairs == {("/pay", "200"): 8, ("/pay", "500"): 2, ("/orders", "200"): 3, ("/orders", "404"): 1}


async def test_the_views_pivot_gives_the_same_answer_when_the_scripting_api_is_refused(make_app):
    app, fake = make_app("7.0.1")
    _seed(fake)
    fake.forbidden.add("search/aggregate")
    gl = app.gl(None)
    result = await gl.aggregate("source:acme-api", _window(fake), (), ["endpoint"], 10, [COUNT, P50, P95, NEWEST])
    assert result.api == "views"
    assert [r.keys[0] for r in result.rows] == ["/orders", "/pay"]  # newest first
    assert result.rows[1].values["percentile(took_ms,95)"] == 1000


async def test_a_sampled_group_keeps_the_order_asked_for(make_app):
    app, fake = make_app("6.1.2")
    _add(fake, seconds_ago=500, source="acme-api", message="older line")
    _add(fake, seconds_ago=400, source="acme-api", message="older line")
    _add(fake, seconds_ago=30, source="acme-api", message="newest line")
    result = await app.gl(None).aggregate("source:acme-api", _window(fake), (), ["message"], 5, [COUNT, NEWEST])
    assert result.api == "sampled"
    assert [r.keys[0] for r in result.rows] == ["newest line", "older line"]


def test_a_metric_over_no_values_reads_as_none():
    assert [normalize_value(v) for v in ("NaN", "-Infinity", "Infinity", float("nan"), 0, "a")] == [
        None, None, None, None, 0, "a"
    ]  # fmt: skip
    assert Metric("percentile", "took_ms", 99.9).name == "percentile(took_ms,99.9)"
