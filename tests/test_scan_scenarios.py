"""Scan accuracy scenarios: situations that used to fire for nothing, or to stay quiet, and what scan says now.

Each scenario builds its own logs. Times are offsets before the fake server's "now"; a scan with range '1h'
looks at the last hour, and its default baseline at the hour before.
"""

from __future__ import annotations

import itertools
import math
import random
import uuid
from datetime import timedelta
from typing import Any

import pytest

from graylog_mcp import scan
from tests.fake_graylog import ALL

H = 3600
D = 24 * H
_ids = itertools.count()


def spread(fake, start_ago: float, end_ago: float, n: int, **fields: Any) -> None:
    """n messages spread evenly between start_ago and end_ago seconds before now (start_ago > end_ago)."""
    margin = 60  # keep clear of window edges: the scan's "now" is a little later than the fake's
    span = start_ago - end_ago - 2 * margin
    for i in range(n):
        ago = start_ago - margin - span * (i + 0.5) / n
        ts = fake.now - timedelta(seconds=ago)
        mid = str(uuid.uuid5(uuid.NAMESPACE_OID, f"scenario-{next(_ids)}"))
        fake.messages.append(
            {
                "_id": mid,
                "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z",
                "streams": [ALL],
                "source": "app-1",
                "service": "shop",
                **fields,
            }
        )


def traffic(fake, start_ago: float, end_ago: float, n: int) -> None:
    spread(fake, start_ago, end_ago, n, level=6, message="GET /api/items 200")


def errors(fake, start_ago: float, end_ago: float, n: int, text: str = "upstream timeout calling stock") -> None:
    spread(fake, start_ago, end_ago, n, level=3, message=text, exception_class="java.net.SocketTimeoutException")


@pytest.fixture
def empty(make_app):
    def factory(**overrides):
        app, fake = make_app(**overrides)
        fake.messages = []
        return app, fake

    return factory


def fired(res: dict) -> set[str]:
    return {f["rule"] for f in res["findings"]}


def quiet(res: dict) -> dict[str, dict]:
    return {q["rule"]: q for q in res["quiet"]}


TIME_RULE = {"name": "per_time", "errors_only": True, "growth": 2.0, "min_count": 5}
SHARE_RULE = {"name": "per_share", "errors_only": True, "growth": 2.0, "min_count": 5, "per_traffic": True}


# --------------------------------------------------------------------------- traffic


async def test_errors_that_only_follow_traffic_do_not_fire(empty):
    """A sale triples the traffic; the error share stays at 2%. The rate per hour triples, the share does not."""
    app, fake = empty()
    traffic(fake, 2 * H, H, 1000)
    errors(fake, 2 * H, H, 20)
    traffic(fake, H, 0, 3000)
    errors(fake, H, 0, 60)
    res = await scan.scan(app, checks=[TIME_RULE, SHARE_RULE])
    assert fired(res) == {"per_time"}  # the old way: a false alarm
    share = quiet(res)["per_share"]
    assert share["count"] == 60 and share["trend"] == "steady"
    # the built-in error rule compares shares too
    assert "error_spike" not in fired(await scan.scan(app, rules=["error_spike"]))


async def test_error_share_rising_under_traffic_growth_fires(empty):
    """Traffic doubles and the error share goes from 1% to 10%: a real problem hidden in a busy hour."""
    app, fake = empty()
    traffic(fake, 2 * H, H, 1000)
    errors(fake, 2 * H, H, 10)
    traffic(fake, H, 0, 1800)
    errors(fake, H, 0, 200)
    res = await scan.scan(app, rules=["error_spike"])
    spike = res["findings"][0]
    assert spike["rule"] == "error_spike" and spike["ratio"] > 5
    assert spike["share"]["baseline"] == "0.99%" and spike["confidence"] > 0.999


async def test_traffic_query_sets_the_denominator(empty):
    """Checkout errors double while checkout traffic doubles; other traffic is flat. Against all traffic the
    share doubles, against checkout traffic it does not."""
    app, fake = empty()
    traffic(fake, 2 * H, 0, 4000)  # flat browsing traffic
    spread(fake, 2 * H, H, 500, level=6, message="POST /checkout 200", path="/checkout")
    spread(fake, H, 0, 1000, level=6, message="POST /checkout 200", path="/checkout")
    spread(fake, 2 * H, H, 100, level=3, message="checkout failed", path="/checkout")
    spread(fake, H, 0, 200, level=3, message="checkout failed", path="/checkout")
    rule = {"query": "path:/checkout", "errors_only": True, "growth": 1.5, "min_count": 5, "per_traffic": True}
    res = await scan.scan(
        app,
        checks=[{**rule, "name": "vs_all"}, {**rule, "name": "vs_checkout", "traffic_query": "path:/checkout"}],
    )
    assert fired(res) == {"vs_all"}
    assert quiet(res)["vs_checkout"]["trend"] == "steady"


# --------------------------------------------------------------------------- seasonality


def daily(fake, days: int, busy: int, calm: int, today_busy: int | None = None) -> None:
    """Every day: a calm hour, then a busy hour (like 08:00-09:00) at the same time as the scan window."""
    for k in range(days + 1):
        base = k * D
        traffic(fake, base + 2 * H, base, 600)
        errors(fake, base + 2 * H, base + H, calm)
        errors(fake, base + H, base, today_busy if k == 0 and today_busy is not None else busy)


async def test_daily_peak_is_normal_against_the_same_hour_yesterday(empty):
    app, fake = empty()
    daily(fake, days=3, busy=60, calm=10)
    res = await scan.scan(app, checks=[TIME_RULE])
    assert fired(res) == {"per_time"}  # against the calm hour before: a false alarm every morning
    res = await scan.scan(app, checks=[TIME_RULE], baseline_shift="1d")
    assert quiet(res)["per_time"]["trend"] == "steady"
    assert res["baseline"].startswith("median of 3 periods")


async def test_daily_peak_that_is_worse_than_usual_fires(empty):
    app, fake = empty()
    daily(fake, days=3, busy=60, calm=10, today_busy=240)
    res = await scan.scan(app, checks=[TIME_RULE], baseline_shift="1d")
    finding = res["findings"][0]
    assert finding["baseline"] == 60 and finding["ratio"] == 4.0


async def test_one_bad_day_does_not_hide_today(empty):
    """Yesterday had an incident (600 errors in the hour). The median of three days ignores it."""
    app, fake = empty()
    daily(fake, days=3, busy=60, calm=10, today_busy=240)
    errors(fake, D + H, D, 540)  # yesterday: 600 in total
    res = await scan.scan(app, checks=[TIME_RULE], baseline_shift="1d", baseline_periods=3)
    assert res["findings"][0]["baseline"] == 60
    one = await scan.scan(app, checks=[TIME_RULE], baseline_shift="1d", baseline_periods=1)
    assert not one["findings"]  # only yesterday as the reference: today looks better than "normal"


async def test_periods_without_data_are_dropped(empty):
    """Only two earlier days are still in the indices: the third period counts nothing and is left out."""
    app, fake = empty()
    daily(fake, days=2, busy=60, calm=10, today_busy=240)
    res = await scan.scan(app, checks=[TIME_RULE], baseline_shift="1d", baseline_periods=3)
    finding = res["findings"][0]
    assert finding["baseline_note"] == "1 of 3 baseline periods had no data"
    assert finding["baseline"] == 60
    empty_past = await scan.scan(app, checks=[TIME_RULE], baseline_shift="7d")
    assert quiet(empty_past)["per_time"]["trend"] == "unknown"
    assert "no data in any baseline period" in quiet(empty_past)["per_time"]["baseline_note"]


async def test_shift_shorter_than_window_is_refused(empty):
    app, _ = empty()
    with pytest.raises(scan.ToolInputError, match="baseline_shift"):
        await scan.scan(app, range="2h", baseline_shift="1h")
    with pytest.raises(scan.ToolInputError, match="not both"):
        await scan.scan(app, baseline="2h", baseline_shift="1d")


# --------------------------------------------------------------------------- significance


async def test_small_counts_are_not_significant(empty):
    """3 errors, then 7: x2.3, but well within chance."""
    app, fake = empty()
    traffic(fake, 2 * H, 0, 200)
    errors(fake, 2 * H, H, 3)
    errors(fake, H, 0, 7)
    res = await scan.scan(app, checks=[{**TIME_RULE, "min_count": 1}])
    q = quiet(res)["per_time"]
    assert q["trend"] == "rising" and "not significant" in q["note"]


async def test_large_moderate_rise_is_significant(empty):
    """400 errors, then 600: only x1.5, but with that much data it is no accident."""
    app, fake = empty()
    traffic(fake, 2 * H, 0, 200)
    errors(fake, 2 * H, H, 400)
    errors(fake, H, 0, 600)
    res = await scan.scan(app, checks=[{**TIME_RULE, "growth": 1.4}])
    finding = res["findings"][0]
    assert finding["ratio"] == 1.5 and finding["confidence"] > 0.9999
    strict = await scan.scan(app, checks=[{**TIME_RULE, "growth": 2.0}])
    assert not strict["findings"]  # the effect size still has to reach the rule's growth


async def test_identical_counts_are_made_once(empty, monkeypatch):
    """Rules share counts (the error query in the window, the traffic): each is asked of Graylog once."""
    app, fake = empty()
    traffic(fake, 2 * H, 0, 100)
    errors(fake, H, 0, 30)
    gl = app.gl(None)
    real, made = gl.count, []

    async def count(q, tr, streams):
        made.append((q, tr.start, tr.end, streams))
        return await real(q, tr, streams)

    monkeypatch.setattr(gl, "count", count)
    await scan.scan(app, rules=["error_spike", "new_error_types", "http_5xx", "connectivity"], samples=False)
    assert len(made) == len(set(made))
    window_end = max(m[2] for m in made)
    window_errors = [m for m in made if m[0] == "level:<=3" and m[2] == window_end]
    assert len(window_errors) == 1  # shared by error_spike and new_error_types


# --------------------------------------------------------------------------- the test itself


def brute_sf(k: int, n: int, p: float) -> float:
    return sum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k, n + 1))


@pytest.mark.parametrize(
    ("k", "n", "p"),
    [
        (7, 10, 0.5),
        (10, 10, 0.5),
        (1, 1, 0.04),
        (1, 25, 0.04),
        (2, 3, 0.9),
        (5, 100, 0.01),
        (30, 40, 0.3),
        (60, 80, 0.5),
        (0, 5, 0.5),
        (6, 5, 0.5),
        (3, 10, 0.5),
    ],
)
def test_binom_sf_matches_brute_force(k, n, p):
    assert scan.binom_sf(k, n, p) == pytest.approx(brute_sf(k, n, p), rel=1e-9, abs=1e-12)


def test_binom_sf_large_and_random():
    assert scan.binom_sf(600, 1000, 0.5) < 1e-9
    assert scan.binom_sf(100_000, 150_000, 0.5) == 0.0  # underflows cleanly, no loop over 50k terms
    rng = random.Random(7)
    for _ in range(500):
        n = rng.randint(1, 120)
        p = rng.uniform(0.01, 0.99)
        k = rng.randint(0, n + 1)
        assert scan.binom_sf(k, n, p) == pytest.approx(brute_sf(k, n, p), rel=1e-9, abs=1e-15)
