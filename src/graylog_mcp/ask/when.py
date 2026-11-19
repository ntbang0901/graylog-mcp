"""Time phrases of a question, in Vietnamese and English, as an exact range.

Rules, no model: a phrase is read to ``[start, end)`` in the instance's timezone and the words it was read from are
kept, so the answer can show what was understood. Matching runs on the text without diacritics, which reads "hom
qua" like "hôm qua"; words typed with diacritics must still be the words of a phrase ("tôi qua" is not "tối qua").
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo

from graylog_mcp.config import TimeSettings
from graylog_mcp.glossary import alias_key, fold, strip_marks


@dataclass(frozen=True)
class TimeSpan:
    start: datetime  # aware, in the instance's zone
    end: datetime  # exclusive, never after now
    text: str  # the words it was read from, as typed


_VIETNAMESE = (
    "sáng trưa chiều tối đêm nay qua hôm kia ngày tuần tháng năm này trước phút giờ tiếng nửa vừa rồi gần đây nhất "
    "mới từ đến tới lúc vào khoảng thứ chủ nhật hai ba tư sáu bảy trong vòng một"
)
_KNOWN = frozenset(alias_key(word) for word in _VIETNAMESE.split())
_LETTERS = re.compile(r"[^\W\d_]+")

_SECONDS = {
    "phut": 60, "minute": 60, "min": 60,
    "gio": 3600, "tieng": 3600, "h": 3600, "hour": 3600, "hr": 3600,
    "ngay": 86400, "day": 86400,
    "tuan": 7 * 86400, "week": 7 * 86400,
    "thang": 30 * 86400, "month": 30 * 86400,
}  # fmt: skip
_PARTS = {
    "sang": "morning", "morning": "morning",
    "trua": "noon", "noon": "noon",
    "chieu": "afternoon", "afternoon": "afternoon",
    "toi": "evening", "evening": "evening",
    "dem": "night", "night": "night",
}  # fmt: skip
_WEEKDAYS = {
    "2": 0, "hai": 0, "monday": 0,
    "3": 1, "ba": 1, "tuesday": 1,
    "4": 2, "tu": 2, "wednesday": 2,
    "5": 3, "nam": 3, "thursday": 3,
    "6": 4, "sau": 4, "friday": 4,
    "7": 5, "bay": 5, "saturday": 5,
    "nhat": 6, "sunday": 6,
}  # fmt: skip
_DAYS = {"hom nay": 0, "today": 0, "hom qua": 1, "yesterday": 1, "hom kia": 2, "the day before yesterday": 2}

_CLOCK = r"\d{1,2}(?:\s*(?:h|gio)(?:\s*\d{1,2}(?!\s*(?:h\b|gio|:|am|pm|/|\d)))?|:\d{2}(?:\s*(?:am|pm))?|\s*(?:am|pm))"
_PART = r"sang|trua|chieu|toi|dem"
_UNIT_VI = r"phut|gio|tieng|h|ngay|tuan|thang"
_UNIT_EN = r"minute|min|hour|hr|day|week|month"
_BACK_VI = r"vua\s+qua|qua|gan\s+day|gan\s+nhat|truoc|vua\s+roi"
_NAMED_DAY = r"monday|tuesday|wednesday|thursday|friday|saturday|sunday"


def _words(pattern: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w/.:-])(?:{pattern})(?![\w/:-])")


_ISO_DATE = _words(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_FULL_DATE = _words(r"(?:ngay\s+)?(\d{1,2})[/-](\d{1,2})[/-](\d{4})")
_SHORT_DATE = _words(r"(?:ngay\s+)?(\d{1,2})/(\d{1,2})")
_SPELLED_DATE = _words(r"(?:ngay\s+)?(\d{1,2})\s+thang\s+(\d{1,2})(?:\s+nam\s+(\d{4}))?")
_DAY_OF_MONTH = _words(r"ngay\s+(\d{1,2})")
_WEEKDAY_VI = _words(
    r"(?:thu\s+(2|3|4|5|6|7|hai|ba|tu|nam|sau|bay)|chu\s+(nhat))(?:\s+(tuan\s+nay|tuan\s+truoc|vua\s+roi|vua\s+qua))?"
)
_WEEKDAY_EN = _words(rf"(?:(last|this)\s+)?({_NAMED_DAY})(?:\s+(this\s+week|last\s+week))?")
_PART_BEFORE = re.compile(rf"(?<!\w)({_PART})\s+(?:ngay\s+)?$")
_CLOCK_RANGE = _words(rf"(?:(?:tu|from)\s+)?({_CLOCK})\s*(?:den|toi|to|until|-|–)\s*({_CLOCK})")
_CLOCK_BETWEEN = _words(rf"between\s+({_CLOCK})\s+and\s+({_CLOCK})")
_CLOCK_SINCE = _words(rf"(?:tu|since|from)\s+({_CLOCK})")
_CLOCK_AT = _words(rf"(?:vao\s+luc|luc|at|khoang|around|about)\s+({_CLOCK})")
_PART_VI = _words(rf"({_PART})\s+(nay|qua|hom\s+nay|hom\s+qua|hom\s+kia)")
_PART_EN = _words(r"(this|yesterday)\s+(morning|noon|afternoon|evening)|(last)\s+(night)|(tonight)")
_DAY = _words(
    r"(?:(tu|since)\s+)?(?:ngay\s+)?(hom\s+nay|hom\s+qua|hom\s+kia|today|yesterday|the\s+day\s+before\s+yesterday)"
)
_BACK_VI_N = _words(rf"(?:trong\s+(?:vong\s+)?)?(\d{{1,3}}|nua|mot)\s*({_UNIT_VI})\s+(?:{_BACK_VI})")
_BACK_EN_N = _words(rf"(?:in\s+the\s+)?(?:last|past)\s+(\d{{1,3}}|half\s+an?|an?)\s+({_UNIT_EN})s?")
_AGO = _words(rf"(\d{{1,3}}|half\s+an?|an?)\s+({_UNIT_EN})s?\s+ago")
_PERIOD = _words(r"(tuan|thang)\s+(nay|truoc)|(this|last)\s+(week|month)")
_BACK_VI_ONE = _words(rf"(?:trong\s+(?:vong\s+)?)?(phut|gio|tieng|ngay|tuan|thang)\s+(?:{_BACK_VI})")
_BACK_EN_ONE = _words(rf"(?:in\s+the\s+)?(?:last|past)\s+({_UNIT_EN})")
_LATELY = _words(r"vua\s+roi|moi\s+day|gan\s+day|just\s+now|right\s+now|recently")


def _clock(text: str) -> tuple[int, int] | None:
    """ "9h30", "9 gio", "9:30am", "2pm" as (hour, minute)."""
    numbers = [int(n) for n in re.findall(r"\d+", text)]
    hour, minute = numbers[0], numbers[1] if len(numbers) > 1 else 0
    if text.endswith(("am", "pm")):
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if text.endswith("pm") else 0)
    return (hour, minute) if hour <= 24 and minute < 60 and (hour, minute) <= (24, 0) else None


def _amount(text: str) -> float:
    if text.isdigit():
        return float(text)
    return 0.5 if text.startswith(("nua", "half")) else 1.0


class _Question:
    def __init__(self, text: str, tz: tzinfo, now: datetime, settings: TimeSettings):
        self.typed = unicodedata.normalize("NFC", text)
        self.folded = fold(self.typed)
        self.bare = strip_marks(self.folded)
        self.tz, self.settings = tz, settings
        self.now = now.astimezone(tz)
        self.today = self.now.date()
        self.taken: list[tuple[int, int]] = []

    def find(self, pattern: re.Pattern[str]) -> re.Match[str] | None:
        """The first match that no earlier phrase holds and whose accented words are words of a time phrase."""
        for m in pattern.finditer(self.bare):
            start, end = m.span()
            if any(start < e and s < end for s, e in self.taken):
                continue
            written = _LETTERS.findall(self.folded[start:end])
            if all(word == strip_marks(word) or alias_key(word) in _KNOWN for word in written):
                self.taken.append((start, end))
                return m
        return None

    def at(self, day: date, hour: int = 0, minute: int = 0) -> datetime:
        if hour == 24:
            day, hour = day + timedelta(days=1), 0
        return datetime(day.year, day.month, day.day, hour, minute, tzinfo=self.tz)

    def part(self, name: str, day: date) -> tuple[datetime, datetime]:
        first, last = getattr(self.settings, name)
        return self.at(day, first), self.at(day + timedelta(days=1) if first > last else day, last)

    def week_start(self, back: int = 0) -> date:
        return self.today - timedelta(days=(self.today.weekday() - self.settings.week_start) % 7 + 7 * back)

    def text(self, spans: list[tuple[int, int]]) -> str:
        return " ".join(self.typed[s:e] for s, e in sorted(spans))


def _dated(q: _Question) -> tuple[date, tuple[int, int]] | None:
    """A day named by its date or by its weekday."""
    for pattern, order in ((_ISO_DATE, "ymd"), (_FULL_DATE, "dmy"), (_SPELLED_DATE, "DMy"), (_SHORT_DATE, "dm")):
        m = q.find(pattern)
        if m is None:
            continue
        a, b = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.lastindex == 3 and m.group(3) else None
        if order == "ymd":
            year, month, day = a, b, int(m.group(3))
        elif order == "DMy" or q.settings.day_first:
            day, month = a, b
        else:
            month, day = a, b
        try:
            found = date(year or q.today.year, month, day)
            if year is None and found > q.today:
                found = date(q.today.year - 1, month, day)  # no year and not yet come: the last one
        except ValueError:
            q.taken.pop()
            continue
        return found, m.span()
    if m := q.find(_DAY_OF_MONTH):
        try:
            found = q.today.replace(day=int(m.group(1)))
            if found > q.today:
                found = (q.today.replace(day=1) - timedelta(days=1)).replace(day=int(m.group(1)))
            return found, m.span()
        except ValueError:
            q.taken.pop()
    for pattern in (_WEEKDAY_VI, _WEEKDAY_EN):
        m = q.find(pattern)
        if m is None:
            continue
        if pattern is _WEEKDAY_VI:
            wanted, which, before = _WEEKDAYS[m.group(1) or m.group(2)], m.group(3) or "", ""
            # "thứ 2" alone is also "the second": a day needs its week, or a part of the day before it
            if m.group(1) and not which and not _PART_BEFORE.search(q.bare[: m.start()]):
                q.taken.pop()
                continue
        else:
            wanted, which, before = _WEEKDAYS[m.group(2)], m.group(3) or "", m.group(1) or ""
        offset = (wanted - q.settings.week_start) % 7
        if which.endswith(("nay", "this week")) or before == "this":
            return q.week_start() + timedelta(days=offset), m.span()
        if which.startswith(("tuan truoc", "last week")):
            return q.week_start(1) + timedelta(days=offset), m.span()
        back = (q.today.weekday() - wanted) % 7
        if back == 0 and (which or before):  # "last friday" on a Friday is the one before
            back = 7
        return q.today - timedelta(days=back), m.span()
    return None


def read_time(text: str, tz: tzinfo, now: datetime, settings: TimeSettings | None = None) -> TimeSpan | None:
    """The time range ``text`` speaks of, or None when it names none or one that has not begun.

    Where several phrases stand together, the most exact wins: clock times, then a part of a day, a length of
    time back from now, a day, a week or month."""
    q = _Question(text, tz, now, settings or TimeSettings())
    used: list[tuple[int, int]] = []
    day: date | None = None
    part: str | None = None
    since = False
    if dated := _dated(q):
        day = dated[0]
        used.append(dated[1])
        if before := _PART_BEFORE.search(q.bare[: dated[1][0]]):
            part = _PARTS[before.group(1)]
            used.append((before.start(), before.end() - 1))

    clock: tuple[str, tuple[int, int], tuple[int, int] | None] | None = None
    kinds = (("range", _CLOCK_RANGE), ("range", _CLOCK_BETWEEN), ("since", _CLOCK_SINCE), ("at", _CLOCK_AT))
    for kind, pattern in kinds:
        m = q.find(pattern)
        if m is None:
            continue
        first, last = _clock(m.group(1)), _clock(m.group(2)) if kind == "range" else None
        if first is None or (kind == "range" and last is None):
            q.taken.pop()
            continue
        clock = (kind, first, last)
        used.append(m.span())
        break

    if part is None:
        if m := q.find(_PART_VI):
            part, day = _PARTS[m.group(1)], q.today - timedelta(days=_DAYS["hom " + m.group(2).split()[-1]])
            used.append(m.span())
        elif m := q.find(_PART_EN):
            if m.group(5):
                part, day = "night", q.today
            elif m.group(3):
                part, day = "night", q.today - timedelta(days=1)
            else:
                part, day = _PARTS[m.group(2)], q.today - timedelta(days=m.group(1) == "yesterday")
            used.append(m.span())
    if day is None and (m := q.find(_DAY)):
        day, since = q.today - timedelta(days=_DAYS[" ".join(m.group(2).split())]), bool(m.group(1))
        used.append(m.span())

    start: datetime
    end: datetime
    if clock is not None:
        kind, (hour, minute), last = clock
        on = day or q.today
        start = q.at(on, hour, minute)
        if kind == "range" and last is not None:
            end = q.at(on, *last)
            if end <= start:
                end = q.at(on + timedelta(days=1), *last)
        elif kind == "since":
            end = q.now
        else:
            start, end = start - timedelta(minutes=30), start + timedelta(minutes=30)
    elif part is not None and day is not None:
        start, end = q.part(part, day)
    elif back := _length_back(q):
        seconds, span = back
        start, end, used = q.now - timedelta(seconds=seconds), q.now, [span]
    elif day is not None:
        start, end = q.at(day), q.now if since else q.at(day + timedelta(days=1))
    elif period := _period(q):
        start, end, span = period
        used = [span]
    else:
        return None
    end = min(end, q.now)
    return TimeSpan(start, end, q.text(used)) if start < end else None


def _length_back(q: _Question) -> tuple[float, tuple[int, int]] | None:
    """Seconds back from now: "15 phút qua", "last 2 hours", "10 minutes ago", "recently"."""
    for pattern in (_BACK_VI_N, _BACK_EN_N, _AGO):
        if m := q.find(pattern):
            return _amount(m.group(1)) * _SECONDS[m.group(2)], m.span()
    # "tuần trước" and "last week" are the calendar ones: only a count makes them a length of time
    for pattern in (_BACK_VI_ONE, _BACK_EN_ONE):
        for m in pattern.finditer(q.bare):
            if not _PERIOD.fullmatch(" ".join(m.group().split())) and q.find(pattern):
                return float(_SECONDS[m.group(1)]), m.span()
    if m := q.find(_LATELY):
        return 3600.0, m.span()
    return None


def _period(q: _Question) -> tuple[datetime, datetime, tuple[int, int]] | None:
    """This or the last calendar week or month."""
    m = q.find(_PERIOD)
    if m is None:
        return None
    unit = m.group(1) or m.group(4)
    last = (m.group(2) or m.group(3)) in ("truoc", "last")
    if unit in ("tuan", "week"):
        first = q.week_start(1 if last else 0)
        return q.at(first), q.at(first + timedelta(days=7)), m.span()
    first = q.today.replace(day=1)
    if last:
        after, first = first, (first - timedelta(days=1)).replace(day=1)
        return q.at(first), q.at(after), m.span()
    return q.at(first), q.now, m.span()
