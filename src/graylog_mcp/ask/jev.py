"""Client of TypeSafe's System One API (Jev), used by ``ask`` only to choose between options and score relevance.

Never shares code with ``client.py``: Graylog stays reachable through GET and the read-only POSTs alone.
Callers pass text that has already been redacted and size-capped.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from graylog_mcp import __version__
from graylog_mcp.client import GraylogError, spans_recorded
from graylog_mcp.config import AskConfig

TIMEOUT_SECONDS = 30.0
TRANSPORT: httpx.AsyncBaseTransport | None = None  # tests of the CLI emulate TypeSafe here


class JevError(GraylogError):
    pass


class JevClient:
    def __init__(self, cfg: AskConfig, transport: httpx.AsyncBaseTransport | None = None):
        assert cfg.api_key, "JevClient needs an API key"
        self.cfg = cfg
        self.requests = 0
        self._http = httpx.AsyncClient(
            base_url=cfg.base_url,
            headers={"Authorization": f"Bearer {cfg.api_key}", "User-Agent": f"graylog-mcp/{__version__}"},
            timeout=TIMEOUT_SECONDS,
            transport=transport or TRANSPORT,
        )

    async def answers(self, state: Any, questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Answers keyed by question name. One retry on a 5xx or a dropped connection."""
        body = {"model": self.cfg.model, "state": state, "questions": questions}
        spans, began = spans_recorded.get(), time.monotonic()
        span: dict[str, Any] = {"service": "jev", "method": "POST", "path": "v1/systemone", "wait_ms": 0}
        try:
            for attempt in (1, 2):
                self.requests += 1
                span["attempts"] = attempt
                try:
                    resp = await self._http.post("/v1/systemone", json=body)
                except httpx.HTTPError as exc:
                    span["status"] = "connect"
                    if attempt == 1:
                        await asyncio.sleep(0.5)
                        continue
                    raise JevError(f"cannot reach TypeSafe ({type(exc).__name__}); try again later") from None
                span["status"] = resp.status_code
                if resp.status_code >= 500 and attempt == 1:
                    await asyncio.sleep(0.5)
                    continue
                break
        except asyncio.CancelledError:
            span["status"] = "cancelled"
            raise
        finally:
            if spans is not None:
                span.update(start_ms=spans.ms_since_start(began), ms=int((time.monotonic() - began) * 1000))
                spans.add(span)
        if resp.status_code in (401, 403):
            raise JevError(f"TypeSafe refused the API key in {self.cfg.api_key_env}; check or replace it", 401)
        if resp.status_code != 200:
            raise JevError(f"TypeSafe answered HTTP {resp.status_code}; ask could not choose", resp.status_code)
        try:
            answers = resp.json()["answers"]
        except (ValueError, KeyError, TypeError):
            raise JevError("TypeSafe returned an answer ask cannot read") from None
        if (
            not isinstance(answers, dict)
            or not set(questions) <= set(answers)
            or not all(isinstance(answers[name], dict) for name in questions)
        ):
            raise JevError("TypeSafe returned an incomplete answer")
        return answers

    async def aclose(self) -> None:
        await self._http.aclose()
