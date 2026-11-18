"""Usage records as JSON lines: one file per process and UTC day, so two writers never share a file.

    usage/live/<day>/<pid>-<start>.jsonl   what one process recorded that day (hot)
    usage/days/<day>.jsonl                 a past day, one line per call, without previews and requests (warm), or
                                           a day imported from the old usage.jsonl
    usage/sessions.jsonl                   one line per conversation whose days are gone (cold)
    usage/hooks/<day>.jsonl                what the Claude Code hook saw (setup.claude_trace), joined when read
    usage/compact.lock                     held by the one process doing housekeeping
    usage/compact.last                     its modification time is when housekeeping last ended

A call is written twice, ``{"event": "started", "call_id": ...}`` before it runs and ``{"event": "finished", ...}``
after, both with the time it started, so both go to the file of the same day. They are read back as one record. A
call that never finished is ``running`` while its process lives, else ``interrupted``.

Housekeeping only rewrites the files of past days, which no process writes to any more, and only deletes a file it
has read whole, so a line written while it runs is kept for the next run.
"""

from __future__ import annotations

import bisect
import contextlib
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from graylog_mcp.usage_compaction import Limits, fold, limits, warm
from graylog_mcp.usage_store import enabled

log = logging.getLogger(__name__)

DETAIL = ("preview", "spans")
DAY_SECONDS = 86400
LOCK_STALE_SECONDS = 600  # a lock older than this was left by a process that died while holding it
RETRY_SECONDS = 60  # after a failed write (read-only folder, full disk), how long before trying again
SESSIONS = "sessions.jsonl"
LAST_RUN = "compact.last"
_DAY_NAME = re.compile(r"\d{4}-\d{2}-\d{2}")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _day(ts: float) -> str:
    # not datetime.fromtimestamp: on Windows it raises OSError for a time before 1970, and read(0, ...) asks for one
    return (_EPOCH + timedelta(seconds=ts)).strftime("%Y-%m-%d")


def _day_before(day: str) -> str:
    return (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")


class JsonlStore:
    def __init__(self, root: Path, legacy: Path | None = None):
        self.root = root
        self.legacy = legacy  # the single usage.jsonl of earlier versions, imported on first use
        self._lock = threading.Lock()
        self._failed_at: float | None = None
        self._process: tuple[int, str] | None = None

    # ------------------------------------------------------------------ writing

    def _file_name(self) -> str:
        pid = os.getpid()
        if self._process is None or self._process[0] != pid:  # a forked child gets a file of its own
            self._process = (pid, f"{pid}-{int(time.time())}.jsonl")
        return self._process[1]

    def append(self, entry: dict[str, Any]) -> None:
        self._write(entry, lambda day: self.root / "live" / day / self._file_name())

    def append_hook(self, entry: dict[str, Any]) -> None:
        # one file per day for every hook process: each line is far below the size an append keeps whole
        self._write(entry, lambda day: self.root / "hooks" / f"{day}.jsonl")

    def _write(self, entry: dict[str, Any], target_of: Callable[[str], Path]) -> None:
        if not enabled():
            return
        if self._failed_at is not None and time.monotonic() - self._failed_at < RETRY_SECONDS:
            return
        with contextlib.suppress(Exception):
            line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
            target = target_of(_day(float(entry.get("ts") or time.time())))
            with self._lock:
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("a", encoding="utf-8") as fh:
                        fh.write(line)
                    self._failed_at = None
                except OSError:
                    self._failed_at = time.monotonic()

    # ------------------------------------------------------------------ reading

    def read(self, since: float, until: float | None = None, detail: bool = False) -> list[dict[str, Any]]:
        if not enabled():
            return []
        if self._legacy_waiting():
            with self._exclusive() as held, contextlib.suppress(OSError):
                if held:
                    self._import_legacy()
        first, last = _day(since), _day(until) if until is not None else None
        rows: list[tuple[dict[str, Any], int | None]] = []
        for path, pid in self._files():
            day = path.parent.name if pid is not None else path.stem
            if day < first or (last is not None and day > last):
                continue
            rows.extend((row, pid) for row in _lines(path))
        out = [
            e for e in _join(rows)
            if isinstance(e.get("ts"), (int, float)) and e["ts"] >= since and (until is None or e["ts"] <= until)
        ]  # fmt: skip
        out.sort(key=lambda e: e["ts"])  # several processes, several files: order by time, not by position
        self._attach_hooks(out, _day(since - DAY_SECONDS), last)
        if not detail:
            out = [{k: v for k, v in e.items() if k not in DETAIL} for e in out]
        return out

    def _attach_hooks(self, calls: list[dict[str, Any]], first: str, last: str | None) -> None:
        """Give each call a Claude Code hook saw that hook's session, agent and turn (the user's prompt before
        it). Read from a day earlier: a hook runs just before its call, maybe on the other side of midnight."""
        tools: dict[str, dict[str, Any]] = {}
        prompts: dict[str, list[tuple[float, str]]] = {}
        with contextlib.suppress(OSError):
            for path in sorted((self.root / "hooks").glob("*.jsonl")):
                if path.stem < first or (last is not None and path.stem > last):
                    continue
                for row in _lines(path):
                    if row.get("kind") == "tool" and row.get("tool_use"):
                        tools[row["tool_use"]] = row
                    elif row.get("kind") == "prompt" and row.get("session") and isinstance(row.get("ts"), (int, float)):
                        prompts.setdefault(row["session"], []).append((row["ts"], str(row.get("prompt"))))
        if not tools:
            return
        for found in prompts.values():
            found.sort()
        times = {session: [ts for ts, _ in found] for session, found in prompts.items()}
        for call in calls:
            hook = tools.get(str(call.get("tool_call_id")))
            if hook is None or not hook.get("session"):
                continue
            call["conversation"] = hook["session"]
            for key in ("agent", "agent_type"):
                if hook.get(key):
                    call[key] = hook[key]
            i = bisect.bisect_right(times.get(hook["session"], []), call["ts"])
            if i:
                call["turn"] = prompts[hook["session"]][i - 1][1]

    def calls_of(self, conversation: str, since: float, until: float) -> list[dict[str, Any]]:
        return [e for e in self.read(since, until, detail=True) if e.get("conversation") == conversation]

    def sessions(self, since: float) -> list[dict[str, Any]]:
        if not enabled():
            return []
        rows = [
            {k: v for k, v in row.items() if k != "days"} for row in _lines(self.root / SESSIONS)
            if isinstance(row.get("last_ts"), (int, float)) and row["last_ts"] >= since
        ]  # fmt: skip
        return sorted(rows, key=lambda r: r["last_ts"])

    def _files(self) -> Iterator[tuple[Path, int | None]]:
        """Every record file, with the pid that wrote it (None for a past day)."""
        for path in self._day_files():
            yield path, None
        with contextlib.suppress(OSError):
            for path in sorted((self.root / "live").glob("*/*.jsonl")):
                yield path, _pid_of(path)

    def _day_files(self) -> list[Path]:
        with contextlib.suppress(OSError):
            return sorted((self.root / "days").glob("*.jsonl"))
        return []

    def _live_days(self) -> list[str]:
        with contextlib.suppress(OSError):
            return sorted(p.name for p in (self.root / "live").iterdir() if p.is_dir() and _DAY_NAME.fullmatch(p.name))
        return []

    # ------------------------------------------------------------------ housekeeping

    def compact(self, now: float, every: float = 0) -> None:
        if not enabled():
            return
        last = self.root / LAST_RUN
        with contextlib.suppress(OSError):
            if every and now - last.stat().st_mtime < every:
                return
        try:
            with self._exclusive() as held:
                if not held:
                    return
                with contextlib.suppress(OSError):
                    self._import_legacy()
                self._housekeep(now, limits())
                last.touch()
                os.utime(last, (now, now))
        except Exception:  # housekeeping must not break the server that runs it
            log.warning("usage housekeeping in %s failed", self.root, exc_info=True)

    def _housekeep(self, now: float, lim: Limits) -> None:
        for leftover in self.root.rglob("*.tmp"):  # from a run that stopped half way: this one holds the lock
            leftover.unlink()
        first_hot = _day(now - (lim.hot_days - 1) * DAY_SECONDS)
        for day in self._live_days():
            if day < first_hot:
                self._warm(day)
        # a call's hook can be written the day before it: keep a day of hooks before the first live day
        hooks_needed = _day_before(first_hot)
        with contextlib.suppress(OSError):
            for path in (self.root / "hooks").glob("*.jsonl"):
                if path.stem < hooks_needed:
                    path.unlink()

        sessions = self._load_sessions()
        before = json.dumps(sessions, sort_keys=True)
        first_warm = _day(now - (lim.days - 1) * DAY_SECONDS)
        cold = [path for path in self._day_files() if path.stem < first_warm]
        for path in cold:
            fold(sessions, path.stem, list(_lines(path)))
        expired = now - lim.session_days * DAY_SECONDS
        sessions = {c: row for c, row in sessions.items() if row["last_ts"] >= expired}
        if json.dumps(sessions, sort_keys=True) != before:
            self._save_sessions(sessions)
        for path in cold:  # only once the sessions that count them are written
            path.unlink()
        self._shrink(_day(now), lim, sessions)

    def _shrink(self, today: str, lim: Limits, sessions: dict[str, dict[str, Any]]) -> None:
        """Delete the oldest data until the folder is under its limit: past days (counted into ``sessions``
        first), then the oldest conversations, then the detail of the days before today, never today's calls."""

        def over() -> int:
            return self._size() - lim.max_bytes

        for early in (False, True):
            if early and over() > 0:
                for day in self._live_days():
                    if day < today:
                        self._warm(day)
            for path in self._day_files():
                if over() <= 0:
                    return
                fold(sessions, path.stem, list(_lines(path)))
                self._save_sessions(sessions)
                path.unlink()
            excess = over()
            if excess > 0 and sessions:
                for conversation in sorted(sessions, key=lambda c: sessions[c]["last_ts"]):
                    excess -= len(_line(sessions.pop(conversation)).encode("utf-8"))
                    if excess <= 0:
                        break
                self._save_sessions(sessions)

    def _warm(self, day: str) -> None:
        """Turn the live files of a past day into ``days/<day>.jsonl``: one line per call, its hook joined, its
        preview and requests summed up."""
        folder, target = self.root / "live" / day, self.root / "days" / f"{day}.jsonl"
        read = [(path, path.stat().st_size) for path in sorted(folder.glob("*.jsonl"))]
        rows: list[tuple[dict[str, Any], int | None]] = [(row, None) for row in _lines(target)]
        for path, _ in read:
            rows.extend((row, _pid_of(path)) for row in _lines(path))
        calls = [warm(call) for call in _join(rows) if isinstance(call.get("ts"), (int, float))]
        calls.sort(key=lambda c: c["ts"])
        self._attach_hooks(calls, _day_before(day), day)
        _replace(target, calls)
        for path, size in read:
            if path.stat().st_size == size:  # else a line came in since it was read: the next run merges it
                path.unlink()
        with contextlib.suppress(OSError):
            folder.rmdir()

    def _load_sessions(self) -> dict[str, dict[str, Any]]:
        return {
            str(row["conversation"]): row for row in _lines(self.root / SESSIONS)
            if row.get("conversation") and isinstance(row.get("last_ts"), (int, float))
        }  # fmt: skip

    def _save_sessions(self, sessions: dict[str, dict[str, Any]]) -> None:
        path = self.root / SESSIONS
        if sessions:
            _replace(path, sorted(sessions.values(), key=lambda r: r["last_ts"]))
        else:
            path.unlink(missing_ok=True)

    def _size(self) -> int:
        total = 0
        with contextlib.suppress(OSError):
            for path in self.root.rglob("*"):
                with contextlib.suppress(OSError):
                    if path.is_file():
                        total += path.stat().st_size
        return total

    @contextlib.contextmanager
    def _exclusive(self) -> Iterator[bool]:
        """Hold ``compact.lock`` for the block; yields False when another process holds it."""
        lock = self.root / "compact.lock"
        fd = None
        with contextlib.suppress(OSError):
            self.root.mkdir(parents=True, exist_ok=True)
            for _ in range(2):
                try:
                    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    break
                except FileExistsError:
                    if time.time() - lock.stat().st_mtime < LOCK_STALE_SECONDS:
                        break
                    lock.unlink()
        if fd is None:
            yield False
            return
        try:
            os.close(fd)
            yield True
        finally:
            with contextlib.suppress(OSError):
                lock.unlink()

    def _legacy_paths(self) -> tuple[Path, Path, Path] | None:
        if self.legacy is None:
            return None
        name = self.legacy.name
        return self.legacy, self.legacy.with_name(name + ".importing"), self.legacy.with_name(name + ".imported")

    def _legacy_waiting(self) -> bool:
        paths = self._legacy_paths()
        return paths is not None and (paths[0].exists() or paths[1].exists())

    def _import_legacy(self) -> None:
        """Move the old usage.jsonl aside, then split it into days. Checked on every run: a server of an earlier
        version still running creates the file again."""
        paths = self._legacy_paths()
        if paths is None:
            return
        legacy, importing, imported = paths
        for _ in range(2):  # an import a crash interrupted, then the file written since
            if not importing.exists():
                if not legacy.exists():
                    return
                os.replace(legacy, importing)
            by_day: dict[str, list[str]] = {}
            for row in _lines(importing):
                if isinstance(row.get("ts"), (int, float)):
                    by_day.setdefault(_day(row["ts"]), []).append(json.dumps(row, ensure_ascii=False) + "\n")
            (self.root / "days").mkdir(parents=True, exist_ok=True)
            for day, lines in by_day.items():
                # appended, not merged: a crash between here and the rename below would import these lines twice
                with (self.root / "days" / f"{day}.jsonl").open("a", encoding="utf-8") as fh:
                    fh.writelines(lines)
            os.replace(importing, imported)


def _pid_of(path: Path) -> int:
    pid = path.stem.split("-", 1)[0]
    return int(pid) if pid.isdigit() else 0


def _line(row: dict[str, Any]) -> str:
    return json.dumps(row, ensure_ascii=False, default=str) + "\n"


def _replace(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    """Write ``path`` whole or not at all: a reader never sees half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.writelines(_line(row) for row in rows)
    os.replace(tmp, path)


def _lines(path: Path) -> Iterator[dict[str, Any]]:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in raw.splitlines():
        with contextlib.suppress(ValueError):
            row = json.loads(line)
            if isinstance(row, dict):
                yield row


def _join(rows: list[tuple[dict[str, Any], int | None]]) -> list[dict[str, Any]]:
    """One record per call: a ``started`` line completed by its ``finished`` line. A line of a past day (no
    ``event``) is a whole call; one of its lines read again, or written after the day was compacted, completes it
    rather than adding a call."""
    from graylog_mcp.setup.service import pid_alive

    out: list[dict[str, Any]] = []
    calls: dict[Any, tuple[dict[str, Any], int | None]] = {}
    finished: set[Any] = set()
    for row, pid in rows:
        event = row.pop("event", None)
        key = row.get("call_id")
        if event is None and key is None:  # recorded by an earlier version
            out.append(row)
            continue
        if key not in calls:
            calls[key] = ({}, pid)
            out.append(calls[key][0])
        call = calls[key][0]
        if event in ("finished", None):
            call.update(row)
            finished.add(key)
        else:
            for k, v in row.items():
                call.setdefault(k, v)
    alive: dict[int | None, bool] = {}
    for key, (call, pid) in calls.items():
        if key in finished:
            continue
        if pid not in alive:
            alive[pid] = bool(pid) and pid_alive(pid)  # type: ignore[arg-type]
        if alive[pid]:
            call["status"] = "running"
        else:
            call["status"], call["ok"] = "interrupted", False
    return out
