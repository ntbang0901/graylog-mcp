"""What an operation's result becomes as evidence: only a short page of log lines is "sampled"."""

from __future__ import annotations

from graylog_mcp.investigations import evidence


def build(op: str, result: dict) -> dict:
    (draft,) = evidence.build(op, result, lambda v: v)
    return draft


def test_a_page_of_lines_short_of_the_total_is_sampled():
    got = build("search_logs", {"total": 56, "returned": 20, "messages": [{"message": "m"}] * 20, "next_offset": 20})
    assert got["sampled"] and "20 of 56 matching lines" in " ".join(got["coverage"])


def test_every_line_returned_is_not_sampled():
    got = build("search_logs", {"total": 3, "messages": [{"message": "m"}] * 3})
    assert not got["sampled"] and got["coverage"] == []


def test_histogram_buckets_are_not_matching_lines():
    got = build("log_histogram", {"total": 56, "buckets": [["02:25", 4], ["02:30", 3]], "peak": {"at": "02:25"}})
    assert got["exact_count"] == 56 and got["returned"] == 2
    assert not got["sampled"] and got["coverage"] == []


def test_groups_and_findings_are_not_matching_lines_either():
    assert not build("top_values", {"total": 374, "top": [{"value": "a", "count": 55}] * 4})["sampled"]
    assert not build("scan", {"count": 900, "findings": [{"rule": "r"}]})["sampled"]


def test_a_grouping_graylog_did_on_a_sample_says_so():
    got = build("top_values", {"total": 90, "method": "sampled", "sampled": 15, "top": [{"value": "a", "count": 3}]})
    assert got["sampled"]


def test_a_cut_answer_is_still_marked_cut_whatever_the_list():
    got = build("log_histogram", {"total": 56, "buckets": [["02:25", 4]], "truncated": True})
    assert got["truncated"] and not got["sampled"]
