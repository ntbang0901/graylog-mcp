"""Sample questions of business analysts and testers, with what the rules must read from each.

Written for the project with neutral names, not collected from users: the share read right says the rules do what
was meant, not how they fare on real questions. Add real questions (made neutral) here when there are some; a
question the rules get wrong stays in, the thresholds in test_questions.py are what must hold.

Times are relative to ``NOW`` in ``ZONE``: "HH:MM" today, "MM-DD HH:MM" this year; None when the question names none.
"""

# ruff: noqa: E501  (one question a line reads better than wrapped rows)
from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Asia/Ho_Chi_Minh")
NOW = datetime(2026, 11, 19, 10, 30, tzinfo=ZONE)  # a Thursday
NOW_TEXT = "10:30"

GLOSSARY: dict[str, Any] = {
    "terms": {
        "payment": {"aliases": ["thanh toán", "payment"], "query": "service:payment"},
        "gateway": {"aliases": ["cổng thanh toán"], "query": "service:gateway"},
        "refund": {"aliases": ["hoàn tiền", "refund"], "keywords": ["refund"]},
        "orders": {"aliases": ["đơn hàng", "order service"], "query": "service:orders"},
        "login": {"aliases": ["đăng nhập", "login"], "query": "service:auth"},
    },
    "codes": {"order": {"pattern": r"ORD-\d+"}, "transaction": {"pattern": r"TXN\d{6,}"}},
}

# (question, time or None, term names in order, codes in order, kind of error or None)
Question = tuple[str, tuple[str, str] | None, list[str], list[str], str | None]
QUESTIONS: list[Question] = [
    ("thanh toán lỗi trong 15 phút qua ở prod", ("10:15", NOW_TEXT), ["payment"], [], "both"),
    ("hôm qua hoàn tiền bị từ chối nhiều không", ("11-18 00:00", "11-19 00:00"), ["refund"], [], "business"),
    ("đơn ORD-1001 sáng nay đi qua đâu", ("06:00", NOW_TEXT), [], ["ORD-1001"], None),
    ("cổng thanh toán timeout từ 9h đến 10h", ("09:00", "10:00"), ["gateway"], [], "technical"),
    ("how many errors did payment have in the last 2 hours", ("08:30", NOW_TEXT), ["payment"], [], "both"),
    ("login failed for many users yesterday afternoon", ("11-18 12:00", "11-18 18:00"), ["login"], [], "both"),
    ("TXN20261119 bị gì", None, [], ["TXN20261119"], None),
    ("dang nhap loi tu hom qua", ("11-18 00:00", NOW_TEXT), ["login"], [], "both"),
    ("thanh toan bi tu choi tuan truoc", ("11-09 00:00", "11-16 00:00"), ["payment"], [], "business"),
    ("show me refund logs for ORD-2002 and ORD-2003 today", ("00:00", NOW_TEXT), ["refund"], ["ORD-2002", "ORD-2003"], None),
    ("đơn hàng bị exception lúc 9h", ("08:30", "09:30"), ["orders"], [], "technical"),
    ("what changed in the order service since yesterday", ("11-18 00:00", NOW_TEXT), ["orders"], [], None),
    ("hệ thống có sự cố gì trong 1 tiếng qua", ("09:30", NOW_TEXT), [], [], "both"),
    ("tối qua thanh toán có bị sập không", ("11-18 18:00", "11-19 00:00"), ["payment"], [], "technical"),
    ("refund declined last week", ("11-09 00:00", "11-16 00:00"), ["refund"], [], "business"),
    ("đăng nhập hôm nay thế nào", ("00:00", NOW_TEXT), ["login"], [], None),
    ("payment errors between 8am and 9am", ("08:00", "09:00"), ["payment"], [], "both"),
    ("ORD-3003 thanh toán không hợp lệ ngày 12/11", ("11-12 00:00", "11-13 00:00"), ["payment"], ["ORD-3003"], "business"),
    ("có bao nhiêu lỗi 5xx ở cổng thanh toán 30 phút gần đây", ("10:00", NOW_TEXT), ["gateway"], [], "technical"),
    ("why is checkout slow", None, [], [], None),
    ("hoan tien cham tu 8h", ("08:00", NOW_TEXT), ["refund"], [], None),
    ("lỗi thanh toán thứ 6 tuần trước", ("11-13 00:00", "11-14 00:00"), ["payment"], [], "both"),
    ("connection refused in login this morning", ("06:00", NOW_TEXT), ["login"], [], "technical"),
    ("đơn hàng tháng trước lỗi nhiều không", ("10-01 00:00", "11-01 00:00"), ["orders"], [], "both"),
    ("thanh toán và hoàn tiền hôm kia", ("11-17 00:00", "11-18 00:00"), ["payment", "refund"], [], None),
    ("TXN20261118 and ORD-4004 failed", None, [], ["TXN20261118", "ORD-4004"], "both"),
    ("last 24 hours order service validation problems", ("11-18 10:30", NOW_TEXT), ["orders"], [], "business"),
    ("đêm qua có crash không", ("11-18 22:00", "11-19 06:00"), [], [], "technical"),
    ("ngày 12 tháng 11 đăng nhập thất bại", ("11-12 00:00", "11-13 00:00"), ["login"], [], "both"),
    ("payment invalid amount 10 minutes ago", ("10:20", NOW_TEXT), ["payment"], [], "business"),
    ("what are the main errors this week", ("11-16 00:00", NOW_TEXT), [], [], "both"),
    ("cổng thanh toán chiều qua bị treo", ("11-18 12:00", "11-18 18:00"), ["gateway"], [], "technical"),
    ("khoảng 14h hôm qua hoàn tiền lỗi hệ thống", ("11-18 13:30", "11-18 14:30"), ["refund"], [], "technical"),
    ("refund for order ORD-5005 was rejected at 9:15am", ("08:45", "09:45"), ["refund"], ["ORD-5005"], "business"),
    ("thanh toán", None, ["payment"], [], None),
]  # fmt: skip
