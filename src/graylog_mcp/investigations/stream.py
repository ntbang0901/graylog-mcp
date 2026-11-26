"""The live feed of one investigation for the admin page: Server-Sent Events replayed from a sequence number.

The page learns of changes from the events table, not from the process that wrote them, so a step recorded by a
stdio server shows up through the shared admin server. The feed says only that the stream is alive (a comment line
every ``HEARTBEAT`` seconds); whether an agent is alive is the snapshot's ``lease``, never inferred from here.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from graylog_mcp import investigations
from graylog_mcp.investigations.model import ANY_REPO

POLL_SECONDS = 1.0
HEARTBEAT_SECONDS = 15.0
BATCH = 200


def frame(event: str, data: Any, seq: int | None = None) -> str:
    head = f"id: {seq}\n" if seq is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


async def feed(
    investigation_id: str,
    after: int = 0,
    *,
    follow: bool = True,
    gone: Callable[[], Awaitable[bool]] | None = None,
    poll: float = POLL_SECONDS,
    heartbeat: float = HEARTBEAT_SECONDS,
) -> AsyncIterator[str]:
    """Replay the events after ``after``, then (with ``follow``) every new one. ``resync`` means ``after`` is older
    than the events kept: the page must fetch the snapshot again. ``gone`` says the client has left."""
    svc = investigations.service()
    quiet = 0.0
    while True:
        got = await investigations.run(
            svc.events, repo=ANY_REPO, investigation_id=investigation_id, after=after, limit=BATCH
        )
        if got["resync"]:
            yield frame("resync", {"after": after})
            return
        for event in got["events"]:
            after = event["seq"]
            quiet = 0.0
            yield frame("event", event, event["seq"])
        if len(got["events"]) == BATCH:
            continue
        if not follow:
            yield frame("end", {"cursor": after})
            return
        if gone is not None and await gone():
            return
        await asyncio.sleep(poll)
        quiet += poll
        if quiet >= heartbeat:
            quiet = 0.0
            yield ": keep-alive\n\n"
