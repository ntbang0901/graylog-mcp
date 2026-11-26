"""A lock that serialises opening a shared sqlite file in WAL mode, across threads and processes.

Switching a file from the rollback journal to WAL needs the file to be idle. When another connection is opening
the same new file, sqlite answers "database is locked" at once for that switch, whatever the busy timeout says
(seen on concurrent first opens). So each open runs its WAL switch and schema setup inside ``wal_lock``, a lock on
a sidecar ``<file>.lock``. The OS releases it when its holder exits, so no stale lock is left behind.
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


@contextlib.contextmanager
def wal_lock(path: Path) -> Iterator[None]:
    """Hold the lock for ``path`` for the block; waits while another thread or process holds it."""
    fd = os.open(path.with_name(path.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        _acquire(fd)
        try:
            yield
        finally:
            if sys.platform == "win32":
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(fd)  # closing the descriptor releases the flock


def _acquire(fd: int) -> None:
    if sys.platform == "win32":
        while True:  # msvcrt gives up after about ten seconds, so keep asking
            try:
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                return
            except OSError:
                time.sleep(0.05)
    else:
        fcntl.flock(fd, fcntl.LOCK_EX)
