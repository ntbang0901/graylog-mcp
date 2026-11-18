"""The usage store contract, checked against every backend, plus what is specific to the JSONL files."""

from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from graylog_mcp.usage_store import UsageStore
from graylog_mcp.usage_store_jsonl import JsonlStore

DAY = 86400


def _jsonl(tmp_path: Path) -> UsageStore:
    return JsonlStore(tmp_path / "usage", legacy=tmp_path / "usage.jsonl")


BACKENDS = [_jsonl]


@pytest.fixture(params=BACKENDS, ids=lambda make: make.__name__.lstrip("_"))
def store(request, tmp_path) -> UsageStore:
    return request.param(tmp_path)


def _call(ts: float, **extra) -> dict:
    return {"ts": ts, "tool": "search_logs", "ok": True, "ms": 5, **extra}


# ----------------------------------------------------------------------------- contract


def test_what_is_appended_is_read_back_in_time_order(store):
    now = time.time()
    store.append(_call(now - 10, n=2))
    store.append(_call(now - 20, n=1, custom={"kept": True}))
    got = store.read(now - 60)
    assert [e["n"] for e in got] == [1, 2]
    assert got[0]["custom"] == {"kept": True}  # unknown keys survive


def test_read_is_bounded_by_the_window_and_spans_days(store):
    now = time.time()
    store.append(_call(now - 3 * DAY, n="old"))
    store.append(_call(now - 1.5 * DAY, n="yesterday"))
    store.append(_call(now - 60, n="now"))
    assert [e["n"] for e in store.read(now - 2 * DAY)] == ["yesterday", "now"]
    assert [e["n"] for e in store.read(now - 4 * DAY, now - DAY)] == ["old", "yesterday"]


def test_detail_is_only_returned_when_asked(store):
    now = time.time()
    store.append(_call(now, preview='{"total":1}', spans=[{"path": "search/messages", "ms": 3}]))
    (brief,) = store.read(now - 1)
    (full,) = store.read(now - 1, detail=True)
    assert "preview" not in brief and "spans" not in brief
    assert full["preview"] == '{"total":1}' and full["spans"][0]["path"] == "search/messages"


def test_calls_of_one_conversation(store):
    now = time.time()
    store.append(_call(now - 3, conversation="aaaaaaaaaaaa", n=1, preview="x"))
    store.append(_call(now - 2, conversation="bbbbbbbbbbbb", n=2))
    store.append(_call(now - 1, conversation="aaaaaaaaaaaa", n=3))
    got = store.calls_of("aaaaaaaaaaaa", now - 60, now)
    assert [e["n"] for e in got] == [1, 3] and got[0]["preview"] == "x"


def test_a_started_call_is_joined_with_its_end(store):
    now = time.time()
    store.append({"event": "started", "call_id": "c1", "ts": now - 2, "tool": "search_logs"})
    store.append({"event": "finished", "call_id": "c1", "ts": now - 1, "ok": True, "status": "ok", "ms": 900})
    (call,) = store.read(now - 60)
    assert call["tool"] == "search_logs" and call["status"] == "ok" and call["ms"] == 900 and "event" not in call


def test_an_unfinished_call_is_running_while_its_process_lives(store):
    now = time.time()
    store.append({"event": "started", "call_id": "c2", "ts": now, "tool": "root_cause"})
    (call,) = store.read(now - 60)
    assert call["status"] == "running" and call["tool"] == "root_cause"


def test_turned_off_writes_and_creates_nothing(store, tmp_path, monkeypatch):
    monkeypatch.setenv("GRAYLOG_MCP_USAGE", "off")
    store.append(_call(time.time()))
    assert store.read(0) == []
    assert not (tmp_path / "usage").exists()


def test_an_unwritable_folder_never_raises(tmp_path):
    (tmp_path / "usage").write_text("a file where the folder should be", encoding="utf-8")
    store = JsonlStore(tmp_path / "usage", legacy=tmp_path / "usage.jsonl")
    store.append(_call(time.time()))
    store.append(_call(time.time()))  # the failure is remembered, not retried on every call
    assert store.read(0) == []


# ----------------------------------------------------------------------------- JSONL files


def test_a_broken_line_is_skipped(tmp_path):
    store = JsonlStore(tmp_path / "usage", legacy=tmp_path / "usage.jsonl")
    now = time.time()
    store.append(_call(now - 2, n=1))
    live = next((tmp_path / "usage" / "live").rglob("*.jsonl"))
    with live.open("a", encoding="utf-8") as fh:
        fh.write('{"ts": half a line\n')
    store.append(_call(now - 1, n=2))
    assert [e["n"] for e in store.read(now - 60)] == [1, 2]


def test_each_process_writes_its_own_file_per_day(tmp_path):
    store = JsonlStore(tmp_path / "usage", legacy=tmp_path / "usage.jsonl")
    now = time.time()
    store.append(_call(now))
    store.append(_call(now - DAY))
    files = sorted((tmp_path / "usage" / "live").rglob("*.jsonl"))
    assert [f.parent.name for f in files] == [_day(now - DAY), _day(now)]
    assert all(f.name.startswith(f"{os.getpid()}-") for f in files)


def test_an_unfinished_call_of_a_dead_process_was_interrupted(tmp_path):
    root = tmp_path / "usage"
    now = time.time()
    dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    folder = root / "live" / _day(now)
    folder.mkdir(parents=True)
    line = {"event": "started", "call_id": "c3", "ts": now, "tool": "search_logs"}
    (folder / f"{dead.stdout.strip()}-1.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
    (call,) = JsonlStore(root, legacy=tmp_path / "usage.jsonl").read(now - 60)
    assert call["status"] == "interrupted" and call["ok"] is False


def test_the_old_usage_file_is_imported_once(tmp_path):
    now = time.time()
    legacy = tmp_path / "usage.jsonl"
    legacy.write_text(
        json.dumps(_call(now - DAY, n=1)) + "\nnot json\n" + json.dumps(_call(now - 60, n=2)) + "\n", encoding="utf-8"
    )
    store = JsonlStore(tmp_path / "usage", legacy=legacy)
    assert [e["n"] for e in store.read(now - 2 * DAY)] == [1, 2]
    assert not legacy.exists() and (tmp_path / "usage.jsonl.imported").exists()
    store.compact(now)
    assert [e["n"] for e in store.read(now - 2 * DAY)] == [1, 2]  # not twice


def test_an_old_file_written_again_after_the_import_is_imported_too(tmp_path):
    # a server of the previous version still running appends to usage.jsonl and creates it again
    now = time.time()
    legacy = tmp_path / "usage.jsonl"
    legacy.write_text(json.dumps(_call(now - 120, n=1)) + "\n", encoding="utf-8")
    store = JsonlStore(tmp_path / "usage", legacy=legacy)
    store.compact(now)
    legacy.write_text(json.dumps(_call(now - 60, n=2)) + "\n", encoding="utf-8")
    assert [e["n"] for e in store.read(now - DAY)] == [1, 2]


def test_two_processes_importing_at_once_do_not_duplicate(tmp_path):
    now = time.time()
    legacy = tmp_path / "usage.jsonl"
    legacy.write_text("".join(json.dumps(_call(now - 60 - i, n=i)) + "\n" for i in range(500)), encoding="utf-8")
    with multiprocessing.get_context("spawn").Pool(2) as pool:
        pool.map(_import_in_another_process, [str(tmp_path)] * 2)
    got = JsonlStore(tmp_path / "usage", legacy=legacy).read(now - DAY)
    assert sorted(e["n"] for e in got) == list(range(500))


def test_a_stale_lock_does_not_block_the_import_for_ever(tmp_path):
    now = time.time()
    legacy = tmp_path / "usage.jsonl"
    legacy.write_text(json.dumps(_call(now - 60, n=1)) + "\n", encoding="utf-8")
    lock = tmp_path / "usage" / "compact.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("", encoding="utf-8")
    os.utime(lock, (now - 3600, now - 3600))  # left behind by a process that died while compacting
    assert [e["n"] for e in JsonlStore(tmp_path / "usage", legacy=legacy).read(now - DAY)] == [1]


def _import_in_another_process(home: str) -> None:
    JsonlStore(Path(home) / "usage", legacy=Path(home) / "usage.jsonl").compact(time.time())


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")
