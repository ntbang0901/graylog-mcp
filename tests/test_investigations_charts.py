"""The series kept with evidence so the page can draw it."""

from __future__ import annotations

import json
from datetime import UTC, timedelta

import pytest

from graylog_mcp import tools
from graylog_mcp.investigations import charts, evidence
from graylog_mcp.timerange import parse_time


def charts_of(op: str, result: dict) -> list[dict]:
    (draft,) = evidence.build(op, result, lambda v: v)
    return draft["summary"].get("charts", [])


def line(t: str, service: str, level: int = 6, message: str = "m") -> dict:
    return {"t": t, "service": service, "level": level, "message": message}


def test_a_histogram_keeps_its_buckets_peak_and_onset():
    (chart,) = charts_of(
        "log_histogram",
        {
            "interval": "5m",
            "total": 12,
            "buckets": [["02:25", 4], ["02:30", 8]],
            "peak": {"at": "02:30", "count": 8},
            "onset": {"at": "02:30", "count": 8, "median": 4},
        },
    )
    assert chart["type"] == "histogram" and chart["points"] == [["02:25", 4], ["02:30", 8]]
    assert chart["peak"] == {"at": "02:30", "count": 8} and chart["onset"]["at"] == "02:30"
    assert "source_buckets" not in chart


def test_a_long_histogram_keeps_the_full_window_and_a_late_peak():
    buckets = [[f"{i // 60:02d}:{i % 60:02d}", 1] for i in range(361)]
    buckets[300][1] = 99
    buckets[-1][1] = 2
    (draft,) = evidence.build(
        "log_histogram",
        {"interval": "1m", "buckets": buckets, "peak": {"at": "05:00", "count": 99}},
        lambda v: v,
    )
    (chart,) = draft["summary"]["charts"]
    assert chart["source_buckets"] == 361 and len(chart["points"]) <= charts.MAX_POINTS
    assert chart["extent"] == ["00:00", "06:00"]
    assert ["05:00", 99] in chart["points"]
    assert chart["points"][-1] == buckets[-1]
    assert not draft["truncated"]  # grouping keeps the whole time window and is disclosed on the series


def test_histogram_charts_keep_exact_instants_when_source_clocks_repeat():
    (chart,) = charts_of(
        "log_histogram",
        {
            "interval": "1h",
            "bucket_start": "2026-11-01T05:00:00.000Z",
            "timezone": "America/New_York (-05:00)",
            "buckets": [["01:00", 4], ["01:00", 8], ["02:00", 2]],
            "peak": {"at": "01:00", "count": 8},
            "onset": {"at": "01:00", "count": 8},
        },
    )
    assert chart["points"] == [
        ["2026-11-01T05:00:00.000Z", 4],
        ["2026-11-01T06:00:00.000Z", 8],
        ["2026-11-01T07:00:00.000Z", 2],
    ]
    assert chart["peak"]["at"] == chart["onset"]["at"] == "2026-11-01T06:00:00.000Z"


@pytest.mark.parametrize("timezone", ["America/New_York", "Asia/Ho_Chi_Minh"])
async def test_histogram_time_axis_matches_the_tools_original_source_labels(make_app, timezone):
    app, _ = make_app("6.1.2", timezone=timezone)
    result = await tools.log_histogram(app, range="2h", interval="5m")
    start = parse_time(result["bucket_start"], UTC)
    for index, (label, _) in enumerate(result["buckets"]):
        assert label == (start + timedelta(minutes=5 * index)).astimezone(app.gl(None).cfg.tz).strftime("%H:%M")
    (chart,) = charts_of("log_histogram", result)
    assert chart["points"][0][0] == result["bucket_start"]


def test_legacy_histograms_keep_their_source_timezone_and_labels():
    (chart,) = charts_of(
        "log_histogram",
        {"interval": "5m", "timezone": "Asia/Ho_Chi_Minh (+07:00)", "buckets": [["02:25", 4], ["02:30", 8]]},
    )
    assert chart["timezone"] == "Asia/Ho_Chi_Minh (+07:00)"
    assert chart["points"] == [["02:25", 4], ["02:30", 8]]


def test_histogram_grouping_also_obeys_the_size_budget_for_long_labels():
    buckets = [[f"{i:03d} " + "x" * 150, i] for i in range(120)]
    (chart,) = charts_of("log_histogram", {"buckets": buckets})
    assert chart["source_buckets"] == 120
    assert chart["extent"] == [buckets[0][0], buckets[-1][0]]
    assert buckets[-1] in chart["points"]
    assert len(json.dumps(chart, ensure_ascii=False, separators=(",", ":"))) <= charts.MAX_BYTES


@pytest.mark.parametrize("kind", ["root_cause", "scan", "search_logs"])
def test_other_evidence_never_turns_event_timelines_or_counts_into_request_charts(kind):
    assert (
        charts_of(
            kind,
            {
                "timeline": [{"t": "-1.5s", "service": "payment", "event": "deployment"}],
                "buckets": [["02:25", 4], ["02:30", 8]],
            },
        )
        == []
    )


def test_presets_use_the_underlying_tool_kind_for_charts():
    assert charts_of("run_preset", {"tool": "log_histogram", "buckets": [["02:25", 4]]})[0]["type"] == "histogram"
    assert (
        charts_of("run_preset", {"tool": "root_cause", "timeline": [{"t": "+0ms", "service": "a", "event": "deploy"}]})
        == []
    )


def test_a_histogram_without_numeric_buckets_has_no_chart():
    assert charts_of("log_histogram", {"buckets": [["02:25", None]]}) == []


def test_a_trace_keeps_first_last_and_error_lines_of_each_service_in_order_of_first_line():
    timeline = [
        line("+0ms", "gateway"),
        line("+12ms", "checkout"),
        line("+35ms", "payment"),
        line("+100ms", "payment"),
        line("+270ms", "payment", 3, "Connection pool exhausted"),
        line("+290ms", "checkout", 3, "Payment request failed"),
        line("+300ms", "gateway"),
    ]
    (chart,) = charts_of(
        "trace_request", {"duration_ms": 300, "timeline": timeline, "first_error": timeline[4], "total": 7}
    )
    assert chart["type"] == "trace" and chart["duration_ms"] == 300 and chart["truncated"] is False
    assert [s["service"] for s in chart["services"]] == ["gateway", "checkout", "payment"]
    payment = chart["services"][2]
    assert (payment["first_ms"], payment["last_ms"], payment["lines"], payment["errors"]) == (35, 270, 3, 1)
    # the middle line is not kept: only the first, the last and the error lines
    assert payment["observations"] == [{"ms": 35, "level": "info"}, {"ms": 270, "level": "error"}]
    assert chart["first_error"] == {"service": "payment", "ms": 270, "message": "Connection pool exhausted"}


def test_a_service_seen_once_has_one_observation_and_errors_are_capped_at_three_besides_the_ends():
    timeline = [line("+5ms", "solo")] + [line(f"+{10 + i}ms", "busy", 3) for i in range(6)]
    (chart,) = charts_of("trace_request", {"timeline": timeline})
    solo, busy = chart["services"]
    assert solo["observations"] == [{"ms": 5, "level": "info"}]
    assert busy["errors"] == 6 and len(busy["observations"]) == 4  # first, last, and the first three errors
    assert "first_error" in chart and chart["first_error"]["service"] == "busy"


def test_without_a_level_only_the_line_the_tool_named_is_an_error():
    timeline = [
        {"t": "+0ms", "service": "a", "ref": "i/1"},
        {"t": "+9ms", "service": "a", "ref": "i/2"},
    ]
    (chart,) = charts_of("trace_request", {"timeline": timeline, "first_error": timeline[1]})
    assert chart["services"][0]["errors"] == 1 and chart["first_error"]["ms"] == 9


def test_services_over_the_limit_are_cut_with_the_ones_that_failed_kept_first():
    timeline = [line(f"+{i}ms", f"svc-{i:02d}") for i in range(50)] + [line("+99ms", "late-failure", 3)]
    (chart,) = charts_of("trace_request", {"timeline": timeline})
    names = [s["service"] for s in chart["services"]]
    assert chart["truncated"] is True and len(names) <= charts.MAX_SERVICES
    assert "late-failure" in names and "svc-00" in names


def test_lines_with_no_parsable_offset_leave_no_trace_chart():
    assert charts_of("trace_request", {"timeline": [{"t": "soon", "service": "a"}]}) == []
    assert charts_of("trace_request", {"timeline": []}) == []


def test_excerpts_keep_each_line_offset_so_the_page_can_show_it():
    (draft,) = evidence.build("trace_request", {"timeline": [line("+12ms", "a")]}, lambda v: v)
    assert draft["excerpts"][0]["t"] == "+12ms"


def test_a_result_that_is_only_text_has_no_series():
    assert charts_of("search_logs", {"total": 1, "messages": [{"message": "m"}]}) == []
    assert charts.of("trace_request", "not a result") == []
    assert charts.offset_ms("soon") is None
    assert charts.offset_ms("-1.5s") == -1500
