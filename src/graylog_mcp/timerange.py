"""Time handling: parsing user input, Graylog timestamps, display and intervals.

Every range is resolved to an absolute UTC window once per tool call, so all
sub-queries of one tool (count + groups + samples, ...) see the same window.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_UNIT_ALIASES = {
    "sec": "s", "secs": "s", "second": "s", "seconds": "s",
    "min": "m", "mins": "m", "minute": "m", "minutes": "m",
    "hr": "h", "hrs": "h", "hour": "h", "hours": "h",
    "day": "d", "days": "d",
    "week": "w", "weeks": "w",
}  # fmt: skip
_DURATION_PART = re.compile(r"(\d+)\s*([a-z]+)")


def parse_duration(value: str | int) -> int:
    """'15m', '2h', '1h30m', '7 days', 90 -> seconds."""
    if isinstance(value, int) and not isinstance(value, bool):
        if value <= 0:
            raise ValueError("duration must be positive")
        return value
    if not isinstance(value, str):
        raise ValueError(f"invalid duration {value!r}")
    text = value.strip().lower()
    if text.startswith("last "):
        text = text[5:]
    if text.isdigit():
        seconds = int(text)
    else:
        pos = 0
        seconds = 0
        for match in _DURATION_PART.finditer(text):
            if text[pos : match.start()].strip():
                break
            unit = _UNIT_ALIASES.get(match.group(2), match.group(2))
            if unit not in _UNIT_SECONDS:
                raise ValueError(f"invalid duration {value!r}: unknown unit {match.group(2)!r}")
            seconds += int(match.group(1)) * _UNIT_SECONDS[unit]
            pos = match.end()
        if pos == 0 or text[pos:].strip():
            raise ValueError(f"invalid duration {value!r}; use forms like 15m, 2h, 1d, 1h30m")
    if seconds <= 0:
        raise ValueError("duration must be positive")
    return seconds


_NAIVE_FORMATS = (
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
)


def parse_time(value: str, tz: tzinfo, now: datetime | None = None) -> datetime:
    """Parse a point in time. Naive values are interpreted in ``tz``.

    Accepts ISO 8601 (with or without offset), 'YYYY-MM-DD HH:MM[:SS]',
    'DD/MM/YYYY HH:MM', 'HH:MM' (today), 'now', '-2h' / '2h ago', epoch s/ms.
    """
    now = now or datetime.now(UTC)
    text = str(value).strip()
    low = text.lower()
    if low == "now":
        return now
    if low.startswith("-") or low.endswith(" ago"):
        return now - timedelta(seconds=parse_duration(low.lstrip("-").removesuffix(" ago")))
    if re.fullmatch(r"\d{10}(\d{3})?", text):
        ts = int(text)
        return datetime.fromtimestamp(ts / 1000 if len(text) == 13 else ts, UTC)
    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", text):
        local_now = now.astimezone(tz)
        parts = [int(p) for p in text.split(":")] + [0]
        return local_now.replace(hour=parts[0], minute=parts[1], second=parts[2], microsecond=0).astimezone(UTC)

    iso = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    parsed: datetime | None
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        parsed = None
        for fmt in _NAIVE_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            raise ValueError(
                f"cannot parse time {value!r}; use ISO 8601 (2024-05-01T10:00:00+07:00), "
                "'2024-05-01 10:00' (configured timezone), 'now' or '-2h'"
            ) from None
    assert parsed is not None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class TimeRange:
    start: datetime  # UTC, aware
    end: datetime  # UTC, aware
    label: str

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()

    def graylog_from(self) -> str:
        return to_graylog(self.start)

    def graylog_to(self) -> str:
        return to_graylog(self.end)

    def views(self) -> dict[str, str]:
        return {"type": "absolute", "from": self.graylog_from(), "to": self.graylog_to()}

    def display(self, tz: tzinfo) -> dict[str, str]:
        return {"from": format_ts(self.start, tz), "to": format_ts(self.end, tz)}


def resolve_range(
    range: str | None,
    from_time: str | None,
    to_time: str | None,
    tz: tzinfo,
    now: datetime | None = None,
    default: str = "15m",
) -> TimeRange:
    now = now or datetime.now(UTC)
    if from_time:
        start = parse_time(from_time, tz, now)
        end = parse_time(to_time, tz, now) if to_time else now
        label = f"{from_time} .. {to_time or 'now'}"
    else:
        span = parse_duration(range or default)
        end = parse_time(to_time, tz, now) if to_time else now
        start = end - timedelta(seconds=span)
        label = f"last {range or default}" if not to_time else f"{range or default} before {to_time}"
    if start >= end:
        raise ValueError(f"empty time range: start {format_ts(start, tz)} is not before end {format_ts(end, tz)}")
    return TimeRange(start=start, end=end, label=label)


def to_graylog(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_graylog_ts(value: object) -> datetime | None:
    """Graylog returns ISO strings ('...Z', '... 10:00:00.000') or epoch millis (pivot min/max)."""
    if value is None or value == "":
        return None
    if isinstance(value, int | float) and not isinstance(value, bool):
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, UTC)
    text = str(value).strip()
    if re.fullmatch(r"-?\d+(\.\d+)?", text):
        return parse_graylog_ts(float(text))
    iso = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def format_ts(value: object, tz: tzinfo) -> str:
    dt = value if isinstance(value, datetime) else parse_graylog_ts(value)
    if dt is None:
        return str(value)
    local = dt.astimezone(tz)
    ms = f".{local.microsecond // 1000:03d}"
    offset = local.strftime("%z")
    offset = offset[:3] + ":" + offset[3:] if offset else ""
    return local.strftime("%Y-%m-%d %H:%M:%S") + ms + offset


# Graylog TimeUnitInterval accepts '<n><unit>' with s/m/h/d/w/M.
_INTERVALS = [
    ("1s", 1), ("5s", 5), ("10s", 10), ("30s", 30),
    ("1m", 60), ("5m", 300), ("10m", 600), ("15m", 900), ("30m", 1800),
    ("1h", 3600), ("2h", 7200), ("3h", 10800), ("6h", 21600), ("12h", 43200),
    ("1d", 86400), ("1w", 604800),
]  # fmt: skip


def choose_interval(tr: TimeRange, max_buckets: int = 60) -> tuple[str, int]:
    for name, secs in _INTERVALS:
        if tr.seconds / secs <= max_buckets:
            return name, secs
    return _INTERVALS[-1]


def interval_seconds(value: str) -> tuple[str, int]:
    """Validate a user supplied interval ('5m') and return (graylog_unit, seconds)."""
    match = re.fullmatch(r"\s*(\d+)\s*([smhdw])\s*", value.lower())
    if not match or int(match.group(1)) <= 0:
        raise ValueError(f"invalid interval {value!r}; use e.g. 30s, 1m, 5m, 1h, 1d")
    n, unit = int(match.group(1)), match.group(2)
    return f"{n}{unit}", n * _UNIT_SECONDS[unit]


def floor_to(dt: datetime, seconds: int) -> datetime:
    epoch = int(dt.timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, UTC)
