"""Time phrases of a question, in Vietnamese and English, as an exact range.

Rules, no model: a phrase is read to ``[start, end)`` in the instance's timezone and the words it was read from are
kept, so the answer can show what was understood. Matching runs on the text without diacritics, which reads "hom
qua" like "hôm qua"; words typed with diacritics must still be the words of a phrase ("tôi qua" is not "tối qua").

A phrase read wrong searches the wrong period in silence, so what is half understood is not read at all ("vài ngày
qua", a date that does not exist, "12/11" with nothing saying it is a day): the provider is then asked instead.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo

from graylog_mcp.config import TimeSettings
from graylog_mcp.glossary import alias_key, fold, strip_marks


@dataclass(frozen=True)
class TimeSpan:
    start: datetime  # aware, in the instance's zone
    end: datetime  # exclusive, never after now
    text: str  # the words it was read from, as typed


class _Unreadable(Exception):
    """A time is named, but not one the rules can place."""


_VIETNAMESE = (
    "sáng trưa chiều tối đêm nay qua hôm kia ngày tuần tháng năm này trước phút giờ tiếng nửa vừa rồi gần đây nhất "
    "mới từ đến tới lúc vào khoảng thứ chủ nhật một hai ba bốn tư sáu bảy tám chín mười trong vòng bây"
)
_KNOWN = frozenset(alias_key(word) for word in _VIETNAMESE.split())
# words that only mean a time with their accents: "ngay" in a sentence typed with accents is "at once", not "ngày"
_ONLY_ACCENTED = frozenset(strip_marks(w) for w in _VIETNAMESE.split() if w != strip_marks(w)) - frozenset(
    _VIETNAMESE.split()
)
_LETTERS = re.compile(r"[^\W\d_]+")

_SECONDS = {
    "phut": 60, "minute": 60, "min": 60,
    "gio": 3600, "tieng": 3600, "h": 3600, "hour": 3600, "hr": 3600,
    "ngay": 86400, "day": 86400,
    "tuan": 7 * 86400, "week": 7 * 86400,
    "thang": 30 * 86400, "month": 30 * 86400,
}  # fmt: skip
_NUMBERS = {
    "mot": 1, "hai": 2, "ba": 3, "bon": 4, "nam": 5, "sau": 6, "bay": 7, "tam": 8, "chin": 9, "muoi": 10,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "a": 1, "an": 1,
}  # fmt: skip
_VAGUE = frozenset({"vai", "may", "nhung", "nhieu", "few", "several", "many", "some", "couple"})
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
_COUNT_VI = r"\d{1,5}(?:[.,]\d{1,2})?|nua|mot|hai|ba|bon|nam|sau|bay|tam|chin|muoi"
_COUNT_EN = r"\d{1,5}(?:\.\d{1,2})?|half\s+an?|an?|one|two|three|four|five|six|seven|eight|nine|ten"


def _words(pattern: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w/.:-])(?:{pattern})(?![\w/:-])")


_ISO_DATE = _words(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_FULL_DATE = _words(r"(?:ngay\s+)?(\d{1,2})[/-](\d{1,2})[/-](\d{4})")
_SHORT_DATE = _words(r"(?:(ngay)\s+)?(\d{1,2})/(\d{1,2})")
_SPELLED_DATE = _words(r"(?:ngay\s+)?(\d{1,2})\s+thang\s+(\d{1,2})(?:\s+nam\s+(\d{4}))?")
_DAY_OF_MONTH = _words(r"ngay\s+(\d{1,2})")
# what says the two numbers around a slash are a day: alone, 3/4 is as much a share and 24/7 a habit
_DATE_LEAD = re.compile(rf"(?<!\w)(?:hom|on|vao|tu|den|toi|since|from|to|until|till|{_PART})\s+$")
_WEEKDAY_VI = _words(
    r"(?:thu\s+(2|3|4|5|6|7|hai|ba|tu|nam|sau|bay)|chu\s+(nhat))(?:\s+(tuan\s+nay|tuan\s+truoc|vua\s+roi|vua\s+qua))?"
)
_WEEKDAY_EN = _words(rf"(?:(last|this)\s+)?({_NAMED_DAY})(?:\s+(this\s+week|last\s+week))?")
_PART_BEFORE = re.compile(rf"(?<!\w)({_PART})\s+(?:ngay\s+)?$")
_PART_AFTER = re.compile(rf"\s*({_PART})(?!\w)(?!\s+(?:nay|qua|hom)(?!\w))")  # "tối qua" is read with its day
_SINCE_BEFORE = re.compile(r"(?<!\w)(?:tu|since|from)\s+(?:(?:ngay|hom)\s+)?$")
_UNTIL_NOW = re.compile(r"\s*(?:den|toi|to|until|till)\s+(?:nay|hom\s+nay|bay\s+gio|now|today)(?!\w)")
_UNTIL = re.compile(r"\s*(?:den|toi|to|until|till|-|–)\s+")
_CLOCK_RANGE = _words(rf"(?:(?:tu|from)\s+)?({_CLOCK})\s*(?:den|toi|to|until|-|–)\s*({_CLOCK})")
_CLOCK_BETWEEN = _words(rf"between\s+({_CLOCK})\s+and\s+({_CLOCK})")
_CLOCK_SINCE = _words(rf"(?:tu|since|from)\s+({_CLOCK})")
_CLOCK_AT = _words(rf"(?:vao\s+luc|luc|at|khoang|around|about)\s+({_CLOCK})")
_PART_VI = _words(rf"({_PART})\s+(nay|qua|hom\s+nay|hom\s+qua|hom\s+kia)")
_PART_EN = _words(r"(this|yesterday)\s+(morning|noon|afternoon|evening)|(last)\s+(night)|(tonight)")
_DAY = _words(
    r"(?:(tu|since)\s+)?(?:ngay\s+)?(hom\s+nay|hom\s+qua|hom\s+kia|today|yesterday|the\s+day\s+before\s+yesterday)"
)
_BACK_VI_N = _words(rf"(?:trong\s+(?:vong\s+)?)?({_COUNT_VI})\s*({_UNIT_VI})\s+(?:{_BACK_VI})")
_BACK_EN_N = _words(rf"(?:in\s+the\s+)?(?:last|past)\s+({_COUNT_EN})\s+({_UNIT_EN})s?")
_AGO = _words(rf"({_COUNT_EN})\s+({_UNIT_EN})s?\s+ago")
_PERIOD = _words(r"(tuan|thang)\s+(nay|truoc)|(this|last)\s+(week|month)")
_BACK_VI_ONE = _words(rf"(?:trong\s+(?:vong\s+)?)?(phut|gio|tieng|ngay|tuan|thang)\s+(?:{_BACK_VI})")
_BACK_EN_ONE = _words(rf"(?:in\s+the\s+)?(?:last|past)\s+({_UNIT_EN})")
_LATELY = _words(r"vua\s+roi|moi\s+day|gan\s+day|just\s+now|right\s+now|recently")


def _clock(text: str) -> tuple[int, int, bool] | None:
    """ "9h30", "9 gio", "9:30am", "2pm" as (hour, minute, whether the half of the day is said)."""
    numbers = [int(n) for n in re.findall(r"\d+", text)]
    hour, minute = numbers[0], numbers[1] if len(numbers) > 1 else 0
    said = hour > 12
    if text.endswith(("am", "pm")):
        if not 1 <= hour <= 12:
            return None
        hour, said = hour % 12 + (12 if text.endswith("pm") else 0), True
    return (hour, minute, said) if minute < 60 and (hour, minute) <= (24, 0) else None


def _amount(text: str) -> float:
    if text[0].isdigit():
        return float(text.replace(",", "."))
    if text.startswith(("nua", "half")):
        return 0.5
    return float(_NUMBERS[text])


class _Question:
    def __init__(self, text: str, tz: tzinfo, now: datetime, settings: TimeSettings):
        self.typed = unicodedata.normalize("NFC", text)
        self.folded = fold(self.typed)
        self.bare = strip_marks(self.folded)
        self.accented = self.folded != self.bare
        self.tz, self.settings = tz, settings
        self.now = now.astimezone(tz)
        self.today = self.now.date()
        self.taken: list[tuple[int, int]] = []

    def as_meant(self, start: int, end: int) -> bool:
        """Whether the words typed there are the words of a time phrase: with accents, those very words; without,
        only in a question typed without any."""
        for word in _LETTERS.findall(self.folded[start:end]):
            if word != strip_marks(word):
                if alias_key(word) not in _KNOWN:
                    return False
            elif self.accented and word in _ONLY_ACCENTED:
                return False
        return True

    def claim(self, span: tuple[int, int]) -> bool:
        """Take ``span`` unless an earlier phrase holds part of it or its words are not meant as a time."""
        start, end = span
        if any(start < e and s < end for s, e in self.taken) or not self.as_meant(start, end):
            return False
        self.taken.append(span)
        return True

    def before(self, pattern: re.Pattern[str], end: int) -> re.Match[str] | None:
        """``pattern`` right before ``end``, when its words are meant as a time."""
        m = pattern.search(self.bare[:end])
        return m if m and self.as_meant(m.start(), m.end()) else None

    def find(self, pattern: re.Pattern[str]) -> re.Match[str] | None:
        return next((m for m in pattern.finditer(self.bare) if self.claim(m.span())), None)

    def at(self, day: date, hour: int = 0, minute: int = 0) -> datetime:
        if hour == 24:
            day, hour = day + timedelta(days=1), 0
        return datetime(day.year, day.month, day.day, hour, minute, tzinfo=self.tz)

    def back(self, moment: datetime, seconds: float) -> datetime:
        """``seconds`` of real time before ``moment``: two datetimes of one zone subtract by the clock, which is
        an hour off across a change of the clocks."""
        return (moment.astimezone(UTC) - timedelta(seconds=seconds)).astimezone(self.tz)

    def part(self, name: str, day: date) -> tuple[datetime, datetime]:
        first, last = getattr(self.settings, name)
        return self.at(day, first), self.at(day + timedelta(days=1) if first > last else day, last)

    def in_part(self, name: str, day: date, clock: tuple[int, int, bool]) -> datetime | None:
        """A clock time said with a part of the day: "9h tối" is 21:00. None when no hour of that part fits."""
        hour, minute, said = clock
        first, last = self.part(name, day)
        for h in (hour,) if said else (hour, hour + 12):
            for on in (day, day + timedelta(days=1)):  # the small hours of a night belong to the next day
                moment = self.at(on, h, minute) if h <= 24 else None
                if moment is not None and first <= moment <= last:
                    return moment
        return None

    def week_start(self, back: int = 0) -> date:
        return self.today - timedelta(days=(self.today.weekday() - self.settings.week_start) % 7 + 7 * back)

    def text(self, spans: list[tuple[int, int]]) -> str:
        return " ".join(self.typed[s:e].strip() for s, e in sorted(spans))


def _dated(q: _Question) -> tuple[date, tuple[int, int]] | None:
    """A day named by its date or by its weekday."""
    for pattern, order in ((_ISO_DATE, "ymd"), (_FULL_DATE, "dmy"), (_SPELLED_DATE, "DMy"), (_SHORT_DATE, "dm")):
        m = q.find(pattern)
        if m is None:
            continue
        if order == "dm":
            if not (m.group(1) or q.before(_DATE_LEAD, m.start())):
                q.taken.pop()
                continue
            a, b, year = int(m.group(2)), int(m.group(3)), None
        else:
            a, b = int(m.group(1)), int(m.group(2))
            year = int(m.group(3)) if m.group(3) else None
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
            raise _Unreadable from None  # 29 February of a common year: no other reading of it is safer
        return found, m.span()
    if m := q.find(_DAY_OF_MONTH):
        number = int(m.group(1))
        month_start = q.today.replace(day=1)
        for first in (month_start, (month_start - timedelta(days=1)).replace(day=1)):
            try:
                found = first.replace(day=number)
            except ValueError:
                continue
            if found <= q.today:
                return found, m.span()
        raise _Unreadable
    for pattern in (_WEEKDAY_VI, _WEEKDAY_EN):
        m = q.find(pattern)
        if m is None:
            continue
        if pattern is _WEEKDAY_VI:
            wanted, which, before = _WEEKDAYS[m.group(1) or m.group(2)], m.group(3) or "", ""
            # "thứ 2" alone is also "the second": a day needs its week, or a part of the day before it
            if m.group(1) and not which and not q.before(_PART_BEFORE, m.start()):
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
    """The time range ``text`` speaks of, or None when it names none, one that has not begun, or one the rules
    cannot place for sure.

    Where several phrases stand together, the most exact wins: clock times, then a part of a day, a length of
    time back from now, a day, a week or month."""
    try:
        return _read(_Question(text, tz, now, settings or TimeSettings()))
    except _Unreadable:
        return None


def _read(q: _Question) -> TimeSpan | None:
    used: list[tuple[int, int]] = []
    day: date | None = None
    until: date | None = None
    part: str | None = None
    since = False
    if dated := _dated(q):
        day, (first, last) = dated
        used.append((first, last))
        lead = q.before(_SINCE_BEFORE, first)
        rest = q.bare[last:]
        if m := _UNTIL_NOW.match(rest):
            since = q.claim((last, last + m.end()))
        elif m := _UNTIL.match(rest):
            held = len(q.taken)
            second = _dated(q)
            if second and second[1][0] == last + m.end() and second[0] >= day:
                until = second[0]
                used.append((last, second[1][1]))
            else:
                del q.taken[held:]  # a day named elsewhere in the question is not the end of this one
        else:
            since = lead is not None
        if lead is not None and (since or until):
            used.append((lead.start(), first))
        elif before := q.before(_PART_BEFORE, first):
            part = _PARTS[before.group(1)]
            used.append((before.start(), first))

    clock: tuple[str, tuple[int, int, bool], tuple[int, int, bool] | None] | None = None
    kinds = (("range", _CLOCK_RANGE), ("range", _CLOCK_BETWEEN), ("since", _CLOCK_SINCE), ("at", _CLOCK_AT))
    for kind, pattern in kinds:
        m = q.find(pattern)
        if m is None:
            continue
        opening, closing = _clock(m.group(1)), _clock(m.group(2)) if kind == "range" else None
        if opening is None or (kind == "range" and closing is None):
            q.taken.pop()
            continue
        clock = (kind, opening, closing)
        used.append(m.span())
        after = _PART_AFTER.match(q.bare[m.end() :]) if part is None else None
        if after and q.claim((m.end(), m.end() + after.end())):  # "lúc 9h tối": the part of the day behind the hour
            part = _PARTS[after.group(1)]
            used.append((m.end(), m.end() + after.end()))
        break

    if m := q.find(_PART_VI):
        part, day = part or _PARTS[m.group(1)], day or q.today - timedelta(days=_DAYS["hom " + m.group(2).split()[-1]])
        used.append(m.span())
    elif part is None and (m := q.find(_PART_EN)):
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

    start: datetime | None
    end: datetime | None
    if clock is not None:
        kind, opening, closing = clock
        on = day or q.today
        if part is None:
            start = q.at(on, *opening[:2])
            end = q.at(on, *closing[:2]) if closing else None
            if end is not None and end <= start:
                end = q.at(on + timedelta(days=1), *closing[:2]) if closing else None
        else:
            # the part of the day is said once, for the end of a range: its start is the hour that leads to it
            end = q.in_part(part, on, closing) if closing else None
            start = q.in_part(part, on, opening)
            if closing and end is not None and (start is None or start >= end):
                start = q.at(on, *opening[:2])
            if start is None or (closing and (end is None or start >= end)):
                raise _Unreadable
        if kind == "since":
            end = q.now
        elif kind == "at":
            start, end = q.back(start, 1800), q.back(start, -1800)
        if end is None:
            raise _Unreadable
    elif part is not None and day is not None:
        start, end = q.part(part, day)
    elif back := _length_back(q):
        seconds, span = back
        start, end, used = q.back(q.now, seconds), q.now, [span]
    elif day is not None:
        start, end = q.at(day), q.now if since else q.at((until or day) + timedelta(days=1))
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
            return _amount(" ".join(m.group(1).split())) * _SECONDS[m.group(2)], m.span()
    for pattern in (_BACK_VI_ONE, _BACK_EN_ONE):
        for m in pattern.finditer(q.bare):
            # "tuần trước" and "last week" are the calendar ones: only a count makes them a length of time
            if _PERIOD.fullmatch(" ".join(m.group().split())):
                continue
            before = q.bare[: m.start()].split()
            counted = bool(before) and (before[-1][-1].isdigit() or before[-1] in _VAGUE or before[-1] in _NUMBERS)
            if counted:
                raise _Unreadable  # "vài ngày qua": a count the rules cannot use is not a count of one
            if q.claim(m.span()):
                return float(_SECONDS[m.group(1)]), m.span()
    if m := q.find(_LATELY):
        return 3600.0, m.span()
    return None


def _period(q: _Question) -> tuple[datetime, datetime, tuple[int, int]] | None:
    """This or the last calendar week or month; from its start to now after "từ" or "since"."""
    m = q.find(_PERIOD)
    if m is None:
        return None
    unit = m.group(1) or m.group(4)
    last = (m.group(2) or m.group(3)) in ("truoc", "last")
    lead = q.before(_SINCE_BEFORE, m.start())
    span = (lead.start() if lead else m.start(), m.end())
    if unit in ("tuan", "week"):
        first = q.week_start(1 if last else 0)
        return q.at(first), q.now if lead else q.at(first + timedelta(days=7)), span
    first = q.today.replace(day=1)
    if last:
        after, first = first, (first - timedelta(days=1)).replace(day=1)
        return q.at(first), q.now if lead else q.at(after), span
    return q.at(first), q.now, span
