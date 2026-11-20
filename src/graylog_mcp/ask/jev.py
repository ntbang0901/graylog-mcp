"""Client of TypeSafe's System One API (Jev): one of the providers ``ask`` may ask to choose between options
and to score relevance.

Never shares code with ``client.py``: Graylog stays reachable through GET and the read-only POSTs alone.
Callers pass text that has already been redacted and size-capped.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from graylog_mcp import __version__
from graylog_mcp.ask.providers import ProviderError, recorded
from graylog_mcp.config import ProviderConfig

TRANSPORT: httpx.AsyncBaseTransport | None = None  # tests of the CLI emulate TypeSafe here


class JevError(ProviderError):
    pass


class JevClient:
    words = False  # System One chooses and scores; it does not write

    def __init__(self, cfg: ProviderConfig, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self.requests = self.bytes_sent = 0
        self._http = httpx.AsyncClient(
            base_url=cfg.base_url,
            headers={"Authorization": f"Bearer {cfg.api_key}", "User-Agent": f"graylog-mcp/{__version__}"},
            timeout=cfg.timeout,
            transport=transport or TRANSPORT,
        )

    async def answers(self, state: Any, questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Answers keyed by question name. One retry on a 5xx or a dropped connection."""
        if not self.cfg.api_key:
            raise JevError(f"no API key in {self.cfg.api_key_env}; run graylog-mcp login --ask")
        body = json.dumps({"model": self.cfg.model, "state": state, "questions": questions}).encode()
        with recorded(self.cfg.name, "POST", "v1/systemone") as span:
            for attempt in (1, 2):
                self.requests += 1
                self.bytes_sent += len(body)
                span["attempts"], span["bytes_sent"] = attempt, len(body) * attempt
                try:
                    resp = await self._http.post(
                        "/v1/systemone", content=body, headers={"Content-Type": "application/json"}
                    )
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
