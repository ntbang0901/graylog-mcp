"""Fixed sentences of the answer in English and Vietnamese. No generated prose: Jev only chooses."""

from __future__ import annotations

from datetime import datetime
from typing import Any

TEXT: dict[str, dict[str, str]] = {
    "en": {
        "understood": "Understood: {parts}. Say so if this is not what you meant.",
        "errors_any": "all lines",
        "errors_technical": "technical errors",
        "errors_business": "business errors",
        "errors_both": "technical and business errors",
        "time_read": '"{words}" ({start} to {end})',
        "no_business_errors": "This instance defines no business errors (business_error_query, or an HTTP status "
        "field), so only technical errors are counted.",
        "no_trace_around": "This error has no trace id ({fields}), so its path across services cannot be followed. "
        "'around' holds the lines of the same source within {seconds} seconds of it: near in time, not one request.",
        "last_hour": "last hour",
        "today": "today",
        "yesterday": "yesterday",
        "last_7_days": "last 7 days",
        "unspecified": "last 24 hours",
        "clarify": "I am not sure what you mean. Name what went wrong, in which environment and when, "
        'for example: "checkout failed with a timeout on prod in the last hour".',
        "no_match": "No error in this period matches your description (best score {score:.2f}).",
        "ambiguous": "Several errors match your description about equally; say which one you mean:",
        "match": "{n}x, {first} to {last}: {sample}",
        "no_trace": "This error has no trace id ({fields}), so its path across services cannot be followed. "
        "Search around its time with search_logs, or get_message with context_seconds.",
        "no_flow": "None of its trace ids was found in the logs of this period.",
        "flow": 'The error "{sample}" ({n}x) went through {services}.',
        "first_error": "The first error came from {service}.",
        "no_code": "No order or transaction code was found in the question. Add a [glossary.codes.<name>] with its "
        "pattern to the config (ask.code_patterns still works) to teach ask what your codes look like.",
        "term": '"{text}" is understood as {what}; this term does not narrow the search.',
        "term_filtered": '"{text}" is understood as {what}, filtered by {query}.',
        "terms_together": "These {n} terms narrow the search together: only lines that match all of them count.",
        "unclear_term": '"{text}" could be {names}; write it with its accents or say more.',
    },
    "vi": {
        "understood": "Đã hiểu: {parts}. Nếu chưa đúng ý, hãy nói lại.",
        "errors_any": "mọi dòng",
        "errors_technical": "lỗi kỹ thuật",
        "errors_business": "lỗi nghiệp vụ",
        "errors_both": "lỗi kỹ thuật và nghiệp vụ",
        "time_read": '"{words}" ({start} đến {end})',
        "no_business_errors": "Instance này chưa định nghĩa lỗi nghiệp vụ (business_error_query, hoặc một field mã "
        "HTTP), nên chỉ tính lỗi kỹ thuật.",
        "no_trace_around": "Lỗi này không có trace id ({fields}) nên không lần theo được đường đi qua các dịch vụ. "
        "'around' là các dòng cùng nguồn trong vòng {seconds} giây quanh lỗi: gần nhau về thời gian, không phải một "
        "request.",
        "last_hour": "1 giờ qua",
        "today": "hôm nay",
        "yesterday": "hôm qua",
        "last_7_days": "7 ngày qua",
        "unspecified": "24 giờ qua",
        "clarify": "Tôi chưa rõ ý bạn. Hãy nêu lỗi gặp phải, môi trường và thời điểm, "
        'ví dụ: "thanh toán bị timeout trên prod trong 1 giờ qua".',
        "no_match": "Không có lỗi nào trong khoảng thời gian này khớp với mô tả của bạn (điểm cao nhất {score:.2f}).",
        "ambiguous": "Có nhiều lỗi khớp gần như ngang nhau; hãy cho biết bạn muốn nói lỗi nào:",
        "match": "{n} lần, {first} đến {last}: {sample}",
        "no_trace": "Lỗi này không có trace id ({fields}) nên không lần theo được đường đi qua các dịch vụ. "
        "Hãy xem quanh thời điểm lỗi bằng search_logs, hoặc get_message với context_seconds.",
        "no_flow": "Không tìm thấy trace id nào của lỗi này trong log của khoảng thời gian này.",
        "flow": 'Lỗi "{sample}" ({n} lần) đi qua {services}.',
        "first_error": "Lỗi đầu tiên xuất hiện ở {service}.",
        "no_code": "Không tìm thấy mã đơn hàng hay mã giao dịch trong câu hỏi. Khai báo [glossary.codes.<tên>] kèm "
        "pattern trong cấu hình (ask.code_patterns vẫn dùng được) để ask nhận ra dạng mã của bạn.",
        "term": 'Đã hiểu "{text}" là {what}; thuật ngữ này không thu hẹp phép tìm.',
        "term_filtered": 'Đã hiểu "{text}" là {what}, lọc theo {query}.',
        "terms_together": "{n} thuật ngữ này cùng thu hẹp phép tìm: chỉ tính các dòng khớp tất cả.",
        "unclear_term": '"{text}" có thể là {names}; hãy gõ có dấu hoặc nói rõ hơn.',
    },
}


def say(language: str, key: str, **values: Any) -> str:
    return TEXT[language][key].format(**values)


def understood(language: str, instance: str, time: str, errors: str) -> str:
    """The first line of every answer. ``time`` is already in words; ``errors`` is one of understood.ERRORS."""
    return say(language, "understood", parts=", ".join((instance, time, say(language, f"errors_{errors}"))))


def time_read(language: str, words: str, start: datetime, end: datetime) -> str:
    """A range read from the question: the words, then the range they were taken as."""
    last = end.strftime("%H:%M" if end.date() == start.date() else "%Y-%m-%d %H:%M")
    return say(language, "time_read", words=words, start=start.strftime("%Y-%m-%d %H:%M"), end=last)
