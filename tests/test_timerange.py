from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from graylog_mcp.timerange import (
    choose_interval,
    format_ts,
    interval_seconds,
    parse_duration,
    parse_graylog_ts,
    parse_time,
    resolve_range,
    to_graylog,
)

HCM = ZoneInfo("Asia/Ho_Chi_Minh")
NOW = datetime(2024, 5, 1, 3, 0, 0, tzinfo=UTC)  # 10:00 in Ho Chi Minh


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("15m", 900), ("2h", 7200), ("1d", 86400), ("1h30m", 5400), ("90", 90), ("7 days", 604800), ("last 5m", 300)],
)
def test_parse_duration(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("bad", ["", "abc", "5y", "0m", "-5m", "5m garbage"])
def test_parse_duration_errors(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)


def test_parse_time_variants():
    assert parse_time("2024-05-01 10:00", HCM, NOW) == NOW
    assert parse_time("2024-05-01T10:00:00+07:00", HCM, NOW) == NOW
    assert parse_time("2024-05-01T03:00:00Z", HCM, NOW) == NOW
    assert parse_time("01/05/2024 10:00", HCM, NOW) == NOW
    assert parse_time("now", HCM, NOW) == NOW
    assert parse_time("-2h", HCM, NOW) == datetime(2024, 5, 1, 1, 0, tzinfo=UTC)
    assert parse_time("2h ago", HCM, NOW) == datetime(2024, 5, 1, 1, 0, tzinfo=UTC)
    assert parse_time("09:30", HCM, NOW) == datetime(2024, 5, 1, 2, 30, tzinfo=UTC)
    assert parse_time("1714532400", HCM, NOW) == NOW
    assert parse_time("1714532400000", HCM, NOW) == NOW
    with pytest.raises(ValueError):
        parse_time("yesterday-ish", HCM, NOW)


def test_resolve_range():
    tr = resolve_range("15m", None, None, HCM, NOW)
    assert tr.seconds == 900 and tr.end == NOW
    tr = resolve_range(None, "2024-05-01 09:00", "2024-05-01 10:00", HCM, NOW)
    assert tr.seconds == 3600
    assert tr.graylog_from() == "2024-05-01T02:00:00.000Z"
    with pytest.raises(ValueError):
        resolve_range(None, "2024-05-01 10:00", "2024-05-01 09:00", HCM, NOW)


def test_graylog_timestamps():
    assert parse_graylog_ts("2024-05-01T03:00:00.000Z") == NOW
    assert parse_graylog_ts("2024-05-01 03:00:00.000") == NOW
    assert parse_graylog_ts(1714532400000.0) == NOW
    assert parse_graylog_ts(None) is None
    assert format_ts("2024-05-01T03:00:00.000Z", HCM) == "2024-05-01 10:00:00.000+07:00"
    assert to_graylog(NOW) == "2024-05-01T03:00:00.000Z"


def test_intervals():
    assert choose_interval(resolve_range("1h", None, None, HCM, NOW))[0] == "1m"
    assert choose_interval(resolve_range("24h", None, None, HCM, NOW))[0] == "30m"
    assert choose_interval(resolve_range("7d", None, None, HCM, NOW))[0] == "3h"
    assert interval_seconds("5m") == ("5m", 300)
    with pytest.raises(ValueError):
        interval_seconds("5 parsecs")
