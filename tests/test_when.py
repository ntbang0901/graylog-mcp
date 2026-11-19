"""Time phrases of a question, in Vietnamese and English, as exact ranges."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from graylog_mcp.ask.when import mentions_time, read_time
from graylog_mcp.config import ConfigError, TimeSettings
from tests.conftest import make_config

SAIGON = ZoneInfo("Asia/Ho_Chi_Minh")
NOW = datetime(2026, 11, 19, 10, 30, tzinfo=SAIGON)  # a Thursday
NOW_TEXT = "10:30"

# (question, start, end); times are "HH:MM" today, "MM-DD HH:MM" this year or "YYYY-MM-DD HH:MM"; None: nothing
# can be read. A phrase the reader gets wrong stays in the table: the share read right is what is measured.
PHRASES: list[tuple[str, str | None, str | None]] = [
    # a length of time back from now
    ("thanh toán lỗi trong 15 phút qua ở prod", "10:15", NOW_TEXT),
    ("15 phut qua", "10:15", NOW_TEXT),
    ("2 giờ qua", "08:30", NOW_TEXT),
    ("1 tiếng qua", "09:30", NOW_TEXT),
    ("nửa tiếng qua", "10:00", NOW_TEXT),
    ("nửa giờ qua", "10:00", NOW_TEXT),
    ("3 ngày qua", "11-16 10:30", NOW_TEXT),
    ("trong giờ qua", "09:30", NOW_TEXT),
    ("24h qua", "11-18 10:30", NOW_TEXT),
    ("5 phút trước", "10:25", NOW_TEXT),
    ("2 tuần qua", "11-05 10:30", NOW_TEXT),
    ("tuần qua", "11-12 10:30", NOW_TEXT),
    ("1 tháng qua", "10-20 10:30", NOW_TEXT),
    ("30 phút gần đây", "10:00", NOW_TEXT),
    ("vừa rồi", "09:30", NOW_TEXT),
    ("last 2 hours", "08:30", NOW_TEXT),
    ("in the last 30 minutes", "10:00", NOW_TEXT),
    ("past 7 days", "11-12 10:30", NOW_TEXT),
    ("last hour", "09:30", NOW_TEXT),
    ("last 24 hours", "11-18 10:30", NOW_TEXT),
    ("10 minutes ago", "10:20", NOW_TEXT),
    ("past week", "11-12 10:30", NOW_TEXT),
    ("recently", "09:30", NOW_TEXT),
    # days
    ("hôm nay", "00:00", NOW_TEXT),
    ("today", "00:00", NOW_TEXT),
    ("hom qua", "11-18 00:00", "11-19 00:00"),
    ("ngày hôm qua", "11-18 00:00", "11-19 00:00"),
    ("yesterday", "11-18 00:00", "11-19 00:00"),
    ("hôm kia", "11-17 00:00", "11-18 00:00"),
    ("từ hôm qua", "11-18 00:00", NOW_TEXT),
    ("since yesterday", "11-18 00:00", NOW_TEXT),
    # parts of a day
    ("sáng nay", "06:00", NOW_TEXT),
    ("sang nay", "06:00", NOW_TEXT),
    ("this morning", "06:00", NOW_TEXT),
    ("chiều nay", None, None),  # still ahead at 10:30
    ("chiều qua", "11-18 12:00", "11-18 18:00"),
    ("tối qua", "11-18 18:00", "11-19 00:00"),
    ("đêm qua", "11-18 22:00", "11-19 06:00"),
    ("last night", "11-18 22:00", "11-19 06:00"),
    ("yesterday afternoon", "11-18 12:00", "11-18 18:00"),
    ("yesterday morning", "11-18 06:00", "11-18 12:00"),
    ("sáng hôm qua", "11-18 06:00", "11-18 12:00"),
    ("trưa qua", "11-18 11:00", "11-18 14:00"),
    # weeks and months
    ("tuần này", "11-16 00:00", NOW_TEXT),
    ("this week", "11-16 00:00", NOW_TEXT),
    ("tuần trước", "11-09 00:00", "11-16 00:00"),
    ("last week", "11-09 00:00", "11-16 00:00"),
    ("tháng này", "11-01 00:00", NOW_TEXT),
    ("this month", "11-01 00:00", NOW_TEXT),
    ("tháng trước", "10-01 00:00", "11-01 00:00"),
    ("last month", "10-01 00:00", "11-01 00:00"),
    ("thứ 2 tuần này", "11-16 00:00", "11-17 00:00"),
    ("thứ ba tuần trước", "11-10 00:00", "11-11 00:00"),
    ("thu 6 tuan truoc", "11-13 00:00", "11-14 00:00"),
    ("monday", "11-16 00:00", "11-17 00:00"),
    ("chủ nhật", "11-15 00:00", "11-16 00:00"),
    ("last friday", "11-13 00:00", "11-14 00:00"),
    ("chiều thứ 3 tuần trước", "11-10 12:00", "11-10 18:00"),
    # clock times
    ("sáng nay từ 9h đến 10h", "09:00", "10:00"),
    ("từ 9h đến 10h", "09:00", "10:00"),
    ("hôm qua từ 14h đến 16h30", "11-18 14:00", "11-18 16:30"),
    ("between 8am and 9am", "08:00", "09:00"),
    ("from 9am to 10:15am", "09:00", "10:15"),
    ("9h-10h", "09:00", "10:00"),
    ("since 9am", "09:00", NOW_TEXT),
    ("từ 8h", "08:00", NOW_TEXT),
    ("lúc 9h", "08:30", "09:30"),
    ("at 9:15am", "08:45", "09:45"),
    ("khoảng 14h hôm qua", "11-18 13:30", "11-18 14:30"),
    ("hôm qua lúc 14h30", "11-18 14:00", "11-18 15:00"),
    ("từ 9 giờ 30 đến 10 giờ", "09:30", "10:00"),
    ("yesterday from 2pm to 4pm", "11-18 14:00", "11-18 16:00"),
    # a clock time said with its part of the day: the hour is the one of that part
    ("lúc 9h tối qua", "11-18 20:30", "11-18 21:30"),
    ("từ 2h đến 4h chiều hôm qua", "11-18 14:00", "11-18 16:00"),
    ("lúc 3 giờ chiều hôm qua", "11-18 14:30", "11-18 15:30"),
    ("từ 8h tối qua", "11-18 20:00", NOW_TEXT),
    ("hôm qua lúc 9h tối", "11-18 20:30", "11-18 21:30"),
    ("lúc 8h sáng nay", "07:30", "08:30"),
    ("từ 10h đến 2h chiều hôm qua", "11-18 10:00", "11-18 14:00"),
    # a number written as a word, or with a fraction
    ("hai giờ qua", "08:30", NOW_TEXT),
    ("ba ngày qua", "11-16 10:30", NOW_TEXT),
    ("mười phút trước", "10:20", NOW_TEXT),
    ("last two hours", "08:30", NOW_TEXT),
    ("1000 phút qua", "11-18 17:50", NOW_TEXT),
    ("1,5 giờ qua", "09:00", NOW_TEXT),
    ("1.5 hours ago", "09:00", NOW_TEXT),
    ("vài ngày qua", None, None),  # how many is not said
    ("mấy giờ qua", None, None),
    # from one day to another, or to now
    ("từ ngày 12/11 đến ngày 15/11", "11-12 00:00", "11-16 00:00"),
    ("từ 12/11 đến nay", "11-12 00:00", NOW_TEXT),
    ("from 2026-11-10 to 2026-11-12", "11-10 00:00", "11-13 00:00"),
    ("since monday", "11-16 00:00", NOW_TEXT),
    ("since last week", "11-09 00:00", NOW_TEXT),
    ("từ tuần trước", "11-09 00:00", NOW_TEXT),
    ("từ hôm 12/11", "11-12 00:00", NOW_TEXT),
    # dates
    ("ngày 12 tháng 11", "11-12 00:00", "11-13 00:00"),
    ("ngày 12/11", "11-12 00:00", "11-13 00:00"),
    ("hôm 12/11", "11-12 00:00", "11-13 00:00"),
    ("on 12/11", "11-12 00:00", "11-13 00:00"),
    ("12/11", None, None),  # alone it may as well be a share: 3/4, 24/7
    ("12/11/2026", "11-12 00:00", "11-13 00:00"),
    ("12-11-2026", "11-12 00:00", "11-13 00:00"),
    ("2026-11-12", "11-12 00:00", "11-13 00:00"),
    ("ngày 12/11 từ 9h đến 11h", "11-12 09:00", "11-12 11:00"),
    ("sáng 12/11", "11-12 06:00", "11-12 12:00"),
    ("ngày 25 tháng 12", "2025-12-25 00:00", "2025-12-26 00:00"),  # not yet come this year: the last one
    ("ngày 12", "11-12 00:00", "11-13 00:00"),
    # not read yet: they stay here so the share below can move
    ("tuần rồi", "11-09 00:00", "11-16 00:00"),
    ("lúc nãy", "09:30", NOW_TEXT),
    ("5 ngày nay", "11-14 10:30", NOW_TEXT),
    ("last 24h", "11-18 10:30", NOW_TEXT),
    ("5m ago", "10:25", NOW_TEXT),
]
NOT_READ_YET = {"tuần rồi", "lúc nãy", "5 ngày nay", "last 24h", "5m ago"}


def _at(text: str) -> datetime:
    day, _, clock = text.rpartition(" ")
    day = day or "11-19"
    if day.count("-") == 1:
        day = "2026-" + day
    return datetime.fromisoformat(f"{day} {clock}").replace(tzinfo=SAIGON)


def _reads(question: str, start: str | None, end: str | None) -> bool:
    got = read_time(question, SAIGON, NOW)
    if start is None or end is None:
        return got is None
    return got is not None and (got.start, got.end) == (_at(start), _at(end))


READ = [p for p in PHRASES if p[0] not in NOT_READ_YET]


@pytest.mark.parametrize(("question", "start", "end"), READ, ids=[p[0] for p in READ])
def test_time_phrases_are_read(question, start, end):
    got = read_time(question, SAIGON, NOW)
    assert _reads(question, start, end), got


def test_at_least_nine_in_ten_phrases_are_read():
    right = sum(_reads(*phrase) for phrase in PHRASES)
    assert len(PHRASES) >= 50 and right / len(PHRASES) >= 0.9, f"{right}/{len(PHRASES)}"


@pytest.mark.parametrize("question", sorted(NOT_READ_YET))
def test_a_phrase_not_read_yet_is_left_to_the_provider(question):
    # read wrong would be worse than not read: these must give nothing until the reader knows them
    assert read_time(question, SAIGON, NOW) is None


def test_the_words_read_are_returned_as_typed():
    assert read_time("Lỗi SÁNG NAY từ 9h đến 10h ở prod", SAIGON, NOW).text == "SÁNG NAY từ 9h đến 10h"
    assert read_time("thanh toán lỗi trong 15 phút qua ở prod", SAIGON, NOW).text == "trong 15 phút qua"


@pytest.mark.parametrize(
    "question",
    [
        "lỗi 500 ở cổng 8080",
        "ORD-1001 bị gì",
        "payment v2.3.1 lỗi",
        "có 12 lỗi",
        "lỗi thứ 2 trong danh sách",
        "tôi qua bước 3 thì lỗi",
        "thanh toán bị lỗi",
        "host 10.0.0.12 lỗi",
        "hệ thống chạy 24/7 bị lỗi",
        "tỉ lệ lỗi 3/4 request",
        "2/3 pod bị crash",
        "kết quả 10/10",
        "ngày 29 tháng 2",
        "ngày 12 tháng 13",
        "cần xử lý ngay 5 đơn lỗi",  # "ngay" (at once) in a sentence typed with accents is not "ngày"
        "đẩy sang 12/11 mới làm",
        "SAP bị lỗi, thu 2 khoản",
    ],
)
def test_words_and_numbers_that_are_not_times_are_left_alone(question):
    assert read_time(question, SAIGON, NOW) is None


def test_day_boundaries_follow_the_zone():
    utc = ZoneInfo("UTC")
    here, there = read_time("hôm qua", SAIGON, NOW), read_time("hôm qua", utc, NOW)
    assert (here.start - there.start).total_seconds() == -7 * 3600
    new_year = datetime(2027, 1, 1, 0, 30, tzinfo=SAIGON)
    got = read_time("hôm qua", SAIGON, new_year)
    assert (got.start, got.end) == (datetime(2026, 12, 31, tzinfo=SAIGON), datetime(2027, 1, 1, tzinfo=SAIGON))
    berlin = ZoneInfo("Europe/Berlin")
    changed = datetime(2026, 10, 25, 12, 0, tzinfo=berlin)  # the night the clocks went back
    today = read_time("hôm nay", berlin, changed)
    assert today.start.isoformat() == "2026-10-25T00:00:00+02:00"
    assert today.end.isoformat() == "2026-10-25T12:00:00+01:00"
    # thirteen hours: two datetimes of one zone subtract by the clock, so the instants are compared in UTC
    assert (today.end.astimezone(UTC) - today.start.astimezone(UTC)).total_seconds() == 13 * 3600


def test_a_length_of_time_is_real_time_across_a_clock_change():
    berlin = ZoneInfo("Europe/Berlin")
    after = datetime(2026, 3, 29, 3, 10, tzinfo=berlin)  # ten minutes after 02:00 became 03:00
    got = read_time("trong 15 phút qua", berlin, after)
    assert (got.end.astimezone(UTC) - got.start.astimezone(UTC)).total_seconds() == 900
    assert got.start.isoformat() == "2026-03-29T01:55:00+01:00"


def test_time_settings_come_from_the_config():
    cfg = make_config(ask={"time": {"morning": [5, 11], "week_start": "sunday", "date_order": "MDY"}})
    settings = cfg.ask.time
    assert settings == TimeSettings(morning=(5, 11), week_start=6, day_first=False)
    assert read_time("sáng qua", SAIGON, NOW, settings).start == _at("11-18 05:00")
    assert read_time("tuần trước", SAIGON, NOW, settings).start == _at("11-08 00:00")
    assert read_time("on 11/12", SAIGON, NOW, settings).start == _at("11-12 00:00")
    wrongs = ({"morning": [12, 5]}, {"week_start": "friday"}, {"date_order": "YMD"}, {"noon": [11]}, {"dusk": [1, 2]})
    for wrong in wrongs:
        with pytest.raises(ConfigError, match=r"ask\.time"):
            make_config(ask={"time": wrong})


@pytest.mark.parametrize("question", ["lỗi tuần rồi", "errors 5m ago", "last 24h", "lúc nãy", "since the release"])
def test_a_time_the_rules_cannot_place_is_still_seen_as_one(question):
    assert read_time(question, SAIGON, NOW) is None
    assert mentions_time(question)


@pytest.mark.parametrize("question", ["thanh toán lỗi", "đơn ORD-1001 đi qua đâu", "how many errors", "top 5 errors"])
def test_a_question_without_time_words_mentions_none(question):
    assert not mentions_time(question)
