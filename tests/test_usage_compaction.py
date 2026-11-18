"""Keeping the usage store small: past days lose their detail, then become one line per conversation, and the
folder stays under its size limit. Every test compacts at a fixed time."""

from __future__ import annotations

import json
import logging
import multiprocessing
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

from graylog_mcp import usage
from graylog_mcp.usage_compaction import Limits, limits, span_summary
from graylog_mcp.usage_store import hashed, store
from graylog_mcp.usage_store_jsonl import JsonlStore

DAY = 86400
NOW = datetime(2026, 11, 18, 12, tzinfo=UTC).timestamp()


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def _jsonl(root: Path) -> JsonlStore:
    return JsonlStore(root / "usage", legacy=root / "usage.jsonl")


def _call(s, ts: float, call_id: str, conversation: str = "aaaaaaaaaaaa", **finished) -> None:
    common = {"call_id": call_id, "ts": ts, "conversation": conversation, "tool": "search_logs", "instance": "main"}
    s.append({"event": "started", **common})
    s.append({"event": "finished", **common, "ok": True, "status": "ok", "ms": 40, "preview": "fake-result",
              **finished})  # fmt: skip


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ----------------------------------------------------------------------------- warm


def test_a_past_day_keeps_each_call_without_its_detail(tmp_path):
    s = _jsonl(tmp_path)
    spans = [{"path": "search/messages", "ms": 30, "wait_ms": 5, "status": 200},
             {"path": "system", "ms": 120, "wait_ms": 0, "status": 503}]  # fmt: skip
    _call(s, NOW - 3 * DAY, "c1", spans=spans, spans_dropped=2, preview_truncated=True)
    s.append({"event": "started", "call_id": "c2", "ts": NOW - 3 * DAY + 5, "tool": "root_cause"})  # never ended
    _call(s, NOW - DAY, "c3")
    _call(s, NOW - 60, "c4")
    s.compact(NOW)

    assert s._live_days() == [_day(NOW - DAY), _day(NOW)]  # today and yesterday as they were
    first, unended = _rows(tmp_path / "usage" / "days" / f"{_day(NOW - 3 * DAY)}.jsonl")
    assert first["call_id"] == "c1" and first["status"] == "ok" and "event" not in first
    assert not {"preview", "preview_truncated", "spans", "spans_dropped"} & first.keys()
    assert first["span_summary"] == {"n": 4, "total_ms": 150, "wait_ms": 5, "failed": 1,
                                     "slowest": {"path": "system", "ms": 120}, "dropped": 2}  # fmt: skip
    assert unended["status"] == "interrupted" and unended["ok"] is False
    got = {e["call_id"]: e for e in s.read(NOW - 4 * DAY, NOW, detail=True)}
    assert list(got) == ["c1", "c2", "c3", "c4"]
    assert "preview" not in got["c1"] and got["c3"]["preview"] == got["c4"]["preview"] == "fake-result"


def test_the_hook_of_a_call_is_kept_with_it_and_its_file_deleted(tmp_path):
    s = _jsonl(tmp_path)
    for ts in (NOW - 3 * DAY, NOW - 2 * DAY):
        s.append_hook({"ts": ts - 1, "kind": "tool", "session": hashed("fake-session"), "tool_use": hashed(str(ts))})
    _call(s, NOW - 3 * DAY, "c1", tool_call_id=hashed(str(NOW - 3 * DAY)))
    s.compact(NOW)
    (call,) = _rows(tmp_path / "usage" / "days" / f"{_day(NOW - 3 * DAY)}.jsonl")
    assert call["conversation"] == hashed("fake-session")
    # the hooks of the day before the first live day stay: a call of that live day can have its hook there
    assert sorted(p.stem for p in (tmp_path / "usage" / "hooks").glob("*.jsonl")) == [_day(NOW - 2 * DAY)]


def test_a_line_written_after_its_day_was_compacted_completes_its_call(tmp_path):
    s = _jsonl(tmp_path)
    s.append({"event": "started", "call_id": "c1", "ts": NOW - 3 * DAY, "tool": "root_cause"})
    s.compact(NOW)
    s.append({"event": "finished", "call_id": "c1", "ts": NOW - 3 * DAY, "tool": "root_cause", "ok": True,
              "status": "ok", "ms": 9000})  # fmt: skip
    (call,) = s.read(NOW - 4 * DAY, NOW)
    assert call["status"] == "ok" and call["ms"] == 9000
    s.compact(NOW)
    assert [c["status"] for c in _rows(tmp_path / "usage" / "days" / f"{_day(NOW - 3 * DAY)}.jsonl")] == ["ok"]


def test_a_warm_week_still_compares_with_the_week_before():
    s = store()
    _call(s, NOW - 10 * DAY, "c1")
    _call(s, NOW - 3600, "c2")
    s.compact(NOW)
    summary = usage.summarize(hours=168, bucket_hours=24, now=NOW)
    assert summary["totals"]["calls"] == 1 and summary["previous"]["calls"] == 1


# ----------------------------------------------------------------------------- cold


def test_old_days_become_one_line_per_conversation(tmp_path):
    s = _jsonl(tmp_path)
    _call(s, NOW - 16 * DAY, "c1")
    _call(s, NOW - 16 * DAY + 60, "c2", ok=False, status="error", error="boom")
    _call(s, NOW - 16 * DAY + 120, "c3", results=0, agent=hashed("fake-agent"), repo="acme")
    _call(s, NOW - 17 * DAY, "c4", conversation="bbbbbbbbbbbb", tool="get_message")
    _call(s, NOW - DAY, "c5")
    old = [{"ts": NOW - 20 * DAY, "tool": "search_logs", "ok": True}, {"ts": NOW - 100 * DAY, "tool": "x", "ok": True}]
    (tmp_path / "usage.jsonl").write_text("".join(json.dumps(r) + "\n" for r in old), encoding="utf-8")
    s.compact(NOW)

    assert [p.stem for p in s._day_files()] == []  # the 16, 17 and 20 days old are gone
    rows = {r["conversation"]: r for r in s.sessions(0)}
    assert set(rows) == {"aaaaaaaaaaaa", "bbbbbbbbbbbb", f"legacy-{_day(NOW - 20 * DAY)}"}  # 100 days: expired
    a = rows["aaaaaaaaaaaa"]
    assert (a["calls"], a["errors"], a["misses"]) == (3, 1, 1)
    assert a["tools"] == {"search_logs": 3} and a["instances"] == ["main"] and a["agents"] == [hashed("fake-agent")]
    assert a["first_ts"] == NOW - 16 * DAY and a["last_ts"] == NOW - 16 * DAY + 120 and a["repo"] == "acme"
    assert "days" not in a
    assert [e["call_id"] for e in s.read(0, NOW)] == ["c5"]


def test_compacting_again_after_a_run_stopped_half_way_counts_nothing_twice(tmp_path):
    s, usage_dir = _jsonl(tmp_path), tmp_path / "usage"
    _call(s, NOW - 16 * DAY, "c1")
    _call(s, NOW - 16 * DAY + 60, "c2")
    live = usage_dir / "live" / _day(NOW - 16 * DAY)
    shutil.copytree(live, tmp_path / "live-copy")
    s.compact(NOW - 10 * DAY)  # 6 days old then: warm
    day_file = usage_dir / "days" / f"{_day(NOW - 16 * DAY)}.jsonl"
    shutil.copy(day_file, tmp_path / "day-copy")
    s.compact(NOW)

    # stopped after writing the new files, before deleting the old ones, and with a temporary file left behind
    shutil.copytree(tmp_path / "live-copy", live)
    shutil.copy(tmp_path / "day-copy", day_file)
    (usage_dir / "sessions.jsonl.tmp").write_text("{half", encoding="utf-8")
    s.compact(NOW)
    (row,) = s.sessions(0)
    assert row["calls"] == 2
    assert not list(usage_dir.rglob("*.tmp")) and s._live_days() == [] and s._day_files() == []


def test_two_processes_compacting_at_once_do_not_duplicate(tmp_path):
    s = _jsonl(tmp_path)
    for i in range(300):
        _call(s, NOW - 3 * DAY + i, f"c{i}")
    with multiprocessing.get_context("spawn").Pool(2) as pool:
        pool.map(_compact_in_another_process, [str(tmp_path)] * 2)
    got = [e["call_id"] for e in _jsonl(tmp_path).read(NOW - 4 * DAY, NOW)]
    assert sorted(got) == sorted(f"c{i}" for i in range(300))


def _compact_in_another_process(home: str) -> None:
    _jsonl(Path(home)).compact(NOW)


# ----------------------------------------------------------------------------- limits


def test_the_folder_stays_under_its_size_limit_and_today_is_kept(tmp_path, monkeypatch):
    s = _jsonl(tmp_path)
    for day in (20, 10, 3, 1):
        for i in range(20):
            _call(s, NOW - day * DAY + i, f"d{day}-{i}", conversation=hashed(str(day)), preview="x" * 2000)
    for i in range(5):
        _call(s, NOW - 600 + i, f"today-{i}", preview="y" * 2000)
    today = sum(p.stat().st_size for p in (tmp_path / "usage" / "live" / _day(NOW)).iterdir())
    monkeypatch.setenv("GRAYLOG_MCP_USAGE_MAX_MB", str((today + 4096) / 1024 / 1024))
    assert s._size() > 10 * limits().max_bytes
    s.compact(NOW)

    assert s._size() <= limits().max_bytes
    got = s.read(NOW - DAY / 2, NOW, detail=True)
    assert [e["call_id"] for e in got] == [f"today-{i}" for i in range(5)]
    assert all(e["preview"] == "y" * 2000 for e in got)


def test_wrong_limits_fall_back_and_the_days_kept_are_two_weeks_at_least(monkeypatch, caplog):
    monkeypatch.setenv("GRAYLOG_MCP_USAGE_HOT_DAYS", "two")
    monkeypatch.setenv("GRAYLOG_MCP_USAGE_DAYS", "3")
    monkeypatch.setenv("GRAYLOG_MCP_USAGE_SESSION_DAYS", "-1")
    monkeypatch.setenv("GRAYLOG_MCP_USAGE_MAX_MB", "nan")
    with caplog.at_level(logging.WARNING):
        assert limits() == Limits(hot_days=2, days=15, session_days=90, max_mb=50)
    assert len(caplog.records) == 4
    monkeypatch.setenv("GRAYLOG_MCP_USAGE_HOT_DAYS", "3")
    monkeypatch.setenv("GRAYLOG_MCP_USAGE_DAYS", "30")
    assert (limits().hot_days, limits().days) == (3, 30)


def test_span_summary_of_no_requests():
    assert span_summary([]) == {"n": 0, "total_ms": 0, "wait_ms": 0, "failed": 0}


# ----------------------------------------------------------------------------- when it runs


def test_a_run_ended_less_than_the_interval_ago_is_not_repeated(tmp_path):
    s = _jsonl(tmp_path)
    s.compact(NOW)
    _call(s, NOW - 3 * DAY, "c1")
    s.compact(NOW + 60, every=600)
    assert _day(NOW - 3 * DAY) in s._live_days()
    s.compact(NOW + 700, every=600)
    assert _day(NOW - 3 * DAY) not in s._live_days()


def test_compaction_runs_in_the_background():
    s, now = store(), time.time()
    _call(s, now - 3 * DAY, "c1")
    usage.start_compaction()
    assert usage._compacting.acquire(timeout=10)  # released when the run ends
    usage._compacting.release()
    assert s._live_days() == []  # type: ignore[attr-defined]
    assert [e["call_id"] for e in s.read(now - 4 * DAY)] == ["c1"]


def test_nothing_is_written_when_usage_is_off(tmp_path, monkeypatch):
    s = _jsonl(tmp_path)
    _call(s, NOW - 3 * DAY, "c1")
    monkeypatch.setenv("GRAYLOG_MCP_USAGE", "off")
    s.compact(NOW)
    usage.start_compaction()
    assert s._live_days() == [_day(NOW - 3 * DAY)] and not (tmp_path / "usage" / "compact.last").exists()
