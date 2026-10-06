from zoneinfo import ZoneInfo

from graylog_mcp.config import StacktraceConfig
from graylog_mcp.redact import Redactor
from graylog_mcp.shaping import Budget, Shaper, dedup, normalize_template, truncate


def shaper(**kw):
    return Shaper(
        redactor=Redactor(),
        stacktrace=StacktraceConfig(),
        tz=ZoneInfo("Asia/Ho_Chi_Minh"),
        max_value_chars=kw.get("max_value_chars", 100),
        default_fields=("timestamp", "source", "level", "message"),
    )


def test_normalize_template():
    a = normalize_template("user 42 failed id=550e8400-e29b-41d4-a716-446655440000 at 2024-05-01T10:00:00Z ip 10.0.0.1")
    b = normalize_template("user 7 failed id=123e4567-e89b-12d3-a456-426614174000 at 2024-06-01T11:00:00Z ip 10.0.0.2")
    assert a == b
    assert normalize_template("hash deadbeef01 ok") == normalize_template("hash cafebabe99 ok")


def test_dedup_groups_and_keeps_singletons():
    msgs = [
        {"ts": "2024-05-01 10:00:03.000+07:00", "source": "a", "message": "timeout after 3001ms"},
        {"ts": "2024-05-01 10:00:02.000+07:00", "source": "b", "message": "timeout after 3002ms"},
        {"ts": "2024-05-01 10:00:01.000+07:00", "source": "a", "message": "something else"},
        {"ts": "2024-05-01 10:00:00.000+07:00", "source": "a", "message": "timeout after 3000ms"},
    ]
    out = dedup(msgs)
    assert len(out) == 2
    assert out[0]["count"] == 3
    assert out[0]["first"] == "2024-05-01 10:00:00.000+07:00"
    assert out[0]["last"] == "2024-05-01 10:00:03.000+07:00"
    assert out[0]["sources"] == ["a", "b"]
    assert out[1]["message"] == "something else"


def test_message_shaping():
    s = shaper()
    out = s.message(
        {
            "_id": "abc",
            "timestamp": "2024-05-01T03:00:00.123Z",
            "source": "web",
            "message": "x" * 300,
            "password": "p",
            "gl2_source_input": "i",
        },
        index="graylog_1",
        select=["*"],
    )
    assert out["ts"] == "2024-05-01 10:00:00.123+07:00"
    assert out["ref"] == "graylog_1/abc"
    assert out["password"] == "[REDACTED]"
    assert "gl2_source_input" not in out
    assert out["message"].endswith("…[+200 chars]")


def test_default_fields_only():
    out = shaper().message({"timestamp": "2024-05-01T03:00:00Z", "message": "m", "other": "o"})
    assert "other" not in out and out["message"] == "m"


def test_budget():
    b = Budget(2000, reserve=0)
    items = b.fit([{"m": "x" * 300} for _ in range(20)])
    assert 4 <= len(items) < 20 and b.truncated


def test_truncate():
    assert truncate("abc", 5) == "abc"
    assert truncate("abcdef", 3) == "abc…[+3 chars]"
