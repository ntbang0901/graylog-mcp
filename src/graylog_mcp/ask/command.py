"""A program on this machine as a provider of ``ask``, for instance a command-line client of a model the user
is signed in to.

The program is started for each request, without a shell. It reads one JSON document on stdin,
``{"version": 1, "state": ..., "questions": {...}}``, and writes ``{"answers": {...}}`` on stdout; its stderr is
dropped. Callers pass text that has already been redacted and size-capped.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from asyncio.subprocess import DEVNULL, PIPE, Process
from pathlib import Path
from typing import Any

from graylog_mcp.ask.providers import ProviderError, read_reply, recorded
from graylog_mcp.config import ProviderConfig

MAX_REPLY_BYTES = 1024 * 1024
REAP_SECONDS = 2.0
_GROUPS = hasattr(os, "killpg")  # not on Windows: there only the program itself is stopped


class _TooLong(Exception):
    pass


class CommandClient:
    words = True

    def __init__(self, cfg: ProviderConfig):
        self.cfg = cfg
        self.requests = self.bytes_sent = 0
        self.last_exit: int | None = None  # exit code of the last run; None before the first

    async def answers(self, state: Any, questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        program = Path(self.cfg.command[0]).name
        sent = json.dumps({"version": 1, "state": state, "questions": questions}, ensure_ascii=False).encode()
        with recorded(self.cfg.name, "RUN", program) as span:
            self.requests += 1
            self.bytes_sent += len(sent)
            span["bytes_sent"] = len(sent)
            try:
                # its own process group where there are such: a wrapper script's children are stopped with it
                proc = await asyncio.create_subprocess_exec(
                    *self.cfg.command, stdin=PIPE, stdout=PIPE, stderr=DEVNULL, start_new_session=_GROUPS
                )
            except OSError as exc:
                span["status"] = "missing"
                raise ProviderError(f"cannot run {program} ({type(exc).__name__}); is it installed?") from None
            try:
                async with asyncio.timeout(self.cfg.timeout):
                    written = await _exchange(proc, sent)
            except TimeoutError:
                span["status"] = "timeout"
                raise ProviderError(f"{program} did not answer within {self.cfg.timeout:g}s") from None
            except _TooLong:
                span["status"] = "too long"
                raise ProviderError(f"{program} wrote more than {MAX_REPLY_BYTES} bytes") from None
            finally:
                # whatever ended the exchange, a cancelled call too, no process is left behind
                if proc.returncode is None:
                    await _stop(proc)
                self.last_exit = proc.returncode
            span["status"] = proc.returncode
        if proc.returncode != 0:
            raise ProviderError(f"{program} exited with code {proc.returncode}")
        try:
            return read_reply(written.decode("utf-8", "replace"))
        except (ValueError, AttributeError):
            raise ProviderError(f"{program} wrote no JSON object ask can read") from None

    async def aclose(self) -> None:
        pass


async def _stop(proc: Process) -> None:
    """Kill the program, with whatever it started, and reap it. asyncio reports the exit only once its output
    was read to the end, so what it still wrote is read and dropped; a process that left the group and keeps the
    pipe open is not waited for."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        if _GROUPS:
            os.killpg(proc.pid, signal.SIGKILL)  # the session it leads has its own pid as group id
        else:
            proc.kill()

    async def reap() -> None:
        assert proc.stdout is not None
        while await proc.stdout.read(65536):
            pass
        await proc.wait()

    with contextlib.suppress(TimeoutError, OSError):
        await asyncio.wait_for(reap(), REAP_SECONDS)


async def _exchange(proc: Process, sent: bytes) -> bytes:
    """Write ``sent`` and read the reply at the same time: a program that answers before it has read everything
    must not block on a full pipe."""
    assert proc.stdin is not None and proc.stdout is not None
    stdin, stdout = proc.stdin, proc.stdout

    async def write() -> None:
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):  # it exited early: its code says why
            stdin.write(sent)
            await stdin.drain()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            stdin.close()

    async def read() -> bytes:
        chunks, size = [], 0
        while chunk := await stdout.read(65536):
            size += len(chunk)
            if size > MAX_REPLY_BYTES:
                raise _TooLong
            chunks.append(chunk)
        return b"".join(chunks)

    _, written = await asyncio.gather(write(), read())
    await proc.wait()
    return written
