"""Client of TypeSafe's System One API (Jev), used by ``ask`` only to choose between options and score relevance.

Never shares code with ``client.py``: Graylog stays reachable through GET and the read-only POSTs alone.
Callers pass text that has already been redacted and size-capped.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from graylog_mcp import __version__
from graylog_mcp.client import GraylogError
from graylog_mcp.config import AskConfig

TIMEOUT_SECONDS = 30.0


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
            transport=transport,
        )

    async def answers(self, state: Any, questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Answers keyed by question name. One retry on a 5xx or a dropped connection."""
        body = {"model": self.cfg.model, "state": state, "questions": questions}
        for attempt in (1, 2):
            self.requests += 1
            try:
                resp = await self._http.post("/v1/systemone", json=body)
            except httpx.HTTPError as exc:
                if attempt == 1:
                    await asyncio.sleep(0.5)
                    continue
                raise JevError(f"cannot reach TypeSafe ({type(exc).__name__}); try again later") from None
            if resp.status_code >= 500 and attempt == 1:
                await asyncio.sleep(0.5)
                continue
            break
        if resp.status_code in (401, 403):
            raise JevError(f"TypeSafe refused the API key in {self.cfg.api_key_env}; check or replace it", 401)
        if resp.status_code != 200:
            raise JevError(f"TypeSafe answered HTTP {resp.status_code}; ask could not choose", resp.status_code)
        try:
            answers = resp.json()["answers"]
        except (ValueError, KeyError, TypeError):
            raise JevError("TypeSafe returned an answer ask cannot read") from None
        if not isinstance(answers, dict) or not set(questions) <= set(answers):
            raise JevError("TypeSafe returned an incomplete answer")
        return answers

    async def aclose(self) -> None:
        await self._http.aclose()
