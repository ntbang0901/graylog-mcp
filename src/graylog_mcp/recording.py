"""One path for every run of an operation, whether the model or the admin Playground asks for it.

``run_recorded`` writes a ``started`` record before the operation runs and a ``finished`` record after it, however
it ends (ok, error, refused, cancelled). Both carry who called: the conversation, the tool call id the client
gave, the client. The finished record adds the Graylog requests the call sent and a redacted preview of its result.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from graylog_mcp import usage
from graylog_mcp.client import GraylogError, SpanLog, requests_sent, spans_recorded
from graylog_mcp.config import ConfigError
from graylog_mcp.operations import Operation
from graylog_mcp.shaping import dumps
from graylog_mcp.tools import ToolInputError
from graylog_mcp.usage_store import hashed

PROCESS = uuid.uuid4().hex  # with stdio one process serves one client session
MAX_CONVERSATIONS = 256  # conversations whose call count is kept, least recently used dropped
PLAYGROUND = "playground"
EXPLORER = "explorer"  # the admin page's log explorer

_seq: OrderedDict[str, int] = OrderedDict()


def _next_seq(conversation: str) -> int:
    n = _seq.pop(conversation, 0) + 1
    _seq[conversation] = n
    while len(_seq) > MAX_CONVERSATIONS:
        _seq.popitem(last=False)
    return n


@dataclass
class RecordContext:
    source: str = "mcp"  # "mcp", "playground" or "explorer"
    tool_name: str | None = None  # the tool the model called; one tool runs several operations
    tool_call_id: str | None = None  # _meta["claudecode/toolUseId"]
    codex_turn: dict[str, Any] | None = None  # _meta["x-codex-turn-metadata"]
    client: str | None = None  # clientInfo, "name/version"
    http_session: str | None = None  # the mcp-session-id header
    investigation_id: str | None = None  # reserved for investigations, not set yet
    step_id: str | None = None  # reserved for investigations, not set yet

    @classmethod
    def from_mcp(cls, ctx: Any, tool_name: str) -> RecordContext:
        """What the client sent with the call. A call made without a request (``MCPServer.call_tool`` in tests)
        has none of it."""
        meta: Any = {}
        headers: Any = None
        with contextlib.suppress(Exception):
            meta = ctx.request_context.meta or {}
            headers = ctx.headers
        if not isinstance(meta, dict):
            meta = {}
        info = meta.get("io.modelcontextprotocol/clientInfo")
        if not isinstance(info, dict):  # clients of earlier protocol versions send it once, when they connect
            with contextlib.suppress(Exception):
                info = ctx.request_context.session.client_params.client_info.model_dump()
        client = None
        if isinstance(info, dict) and info.get("name"):
            client = f"{info['name']}/{info['version']}" if info.get("version") else str(info["name"])
        turn = meta.get("x-codex-turn-metadata")
        tool_call_id = meta.get("claudecode/toolUseId")
        session = headers.get("mcp-session-id") if headers is not None else None
        return cls(
            tool_name=tool_name,
            tool_call_id=str(tool_call_id) if tool_call_id else None,
            codex_turn=turn if isinstance(turn, dict) else None,
            client=client[:100] if client else None,
            http_session=str(session) if session else None,
        )

    def identity(self) -> dict[str, Any]:
        """The fields both records of one call carry, with a new ``call_id``."""
        from graylog_mcp.shared import current_repo

        turn = self.codex_turn or {}
        codex_session = turn.get("session_id") or turn.get("sessionId")
        codex_turn = turn.get("turn_id") or turn.get("turnId")
        if self.source in (PLAYGROUND, EXPLORER):  # the admin's own runs, apart from any model's conversation
            conversation = self.source
        elif codex_session:
            conversation = hashed(str(codex_session))
        elif self.http_session:
            conversation = hashed(self.http_session)
        else:  # one HTTP server without sessions serves several clients and repositories
            conversation = hashed(f"{PROCESS}|{self.client}|{current_repo.get()}")
        out: dict[str, Any] = {
            "call_id": uuid.uuid4().hex[:12],
            "ts": round(time.time(), 3),
            "source": self.source,
            "conversation": conversation,
            "seq": _next_seq(conversation),
        }
        if self.tool_name:
            out["tool_name"] = self.tool_name
        if self.tool_call_id:
            out["tool_call_id"] = hashed(self.tool_call_id)
        if codex_turn:
            out["turn"] = hashed(str(codex_turn))
        if self.client:
            out["client"] = self.client
        if self.http_session:
            out["http_session"] = hashed(self.http_session)
        return out


async def run_recorded(
    op: Operation,
    get_app: Callable[[], Any],
    ctx: RecordContext,
    kwargs: dict[str, Any],
    refused: str | None = None,
) -> tuple[Any, str]:
    """Run ``op`` and record it; returns the result and its JSON text. With ``refused``, record the refusal
    instead of running it. Exceptions propagate unchanged: each caller turns them into its own error."""
    identity = ctx.identity()
    started = time.monotonic()
    sent, spans = [0], SpanLog()
    sent_token, spans_token = requests_sent.set(sent), spans_recorded.set(spans)
    app: Any = None
    result: Any = None
    text, error, status = "", None, "ok"
    try:
        app = get_app()
        if inspect.isawaitable(app):
            app = await app
        usage.track_start(op.name, app, kwargs, identity)
        if refused:
            status = "refused"
            raise ToolInputError(refused)
        result = op.fn(app, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        text = dumps(result)
        return result, text
    except asyncio.CancelledError:
        status, error = "cancelled", "cancelled"
        raise
    except (GraylogError, ConfigError, ValueError) as exc:
        status, error = ("refused" if status == "refused" else "error"), str(exc)
        raise
    except Exception as exc:
        status, error = "error", f"{type(exc).__name__}: {exc}"
        raise
    finally:
        # synchronous, so a cancelled call is still recorded: nothing here can be interrupted by a second cancel
        usage.track(
            op.name, app, kwargs, result, error, started, len(text), sent[0],
            identity=identity, status=status, spans=spans, text=text,
        )  # fmt: skip
        requests_sent.reset(sent_token)
        spans_recorded.reset(spans_token)
