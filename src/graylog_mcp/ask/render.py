"""Fixed sentences of the answer in English and Vietnamese. No generated prose: Jev only chooses."""

from __future__ import annotations

from typing import Any

TEXT: dict[str, dict[str, str]] = {
    "en": {
        "understood": "Understood: {parts}. Say so if this is not what you meant.",
        "errors_only": "errors only",
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
        "Search around its time with search_logs or context_around.",
        "no_flow": "None of its trace ids was found in the logs of this period.",
        "flow": 'The error "{sample}" ({n}x) went through {services}.',
        "first_error": "The first error came from {service}.",
        "no_code": "No order or transaction code was found in the question. Set ask.code_patterns in the config "
        "to teach ask what your codes look like.",
    },
    "vi": {
        "understood": "Đã hiểu: {parts}. Nếu chưa đúng ý, hãy nói lại.",
        "errors_only": "chỉ lỗi",
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
        "Hãy xem quanh thời điểm lỗi bằng search_logs hoặc context_around.",
        "no_flow": "Không tìm thấy trace id nào của lỗi này trong log của khoảng thời gian này.",
        "flow": 'Lỗi "{sample}" ({n} lần) đi qua {services}.',
        "first_error": "Lỗi đầu tiên xuất hiện ở {service}.",
        "no_code": "Không tìm thấy mã đơn hàng hay mã giao dịch trong câu hỏi. Khai báo ask.code_patterns trong "
        "cấu hình để ask nhận ra dạng mã của bạn.",
    },
}


def say(language: str, key: str, **values: Any) -> str:
    return TEXT[language][key].format(**values)


def understood(language: str, instance: str | None, time: str, errors_only: bool) -> str:
    parts = [p for p in (instance, say(language, time), say(language, "errors_only") if errors_only else None) if p]
    return say(language, "understood", parts=", ".join(parts))
