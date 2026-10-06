"""Async HTTP client for the Graylog REST API.

Only read requests leave this module: GET anywhere, POST only to the search
endpoints that execute a query without persisting anything. Any other method or
path is refused before a request is built.
"""

from __future__ import annotations

import logging
import re
import ssl
from typing import Any

import httpx

from graylog_mcp import __version__
from graylog_mcp.config import InstanceConfig

log = logging.getLogger(__name__)

# POST endpoints that run a search without storing it.
#  - views/search/sync: builds the search with toSearch() and executes it; no saveForUser.
#  - search/messages, search/aggregate: the 5.2+ Scripting API, stateless by design.
#  - search/validate: query validation used for precise syntax errors and unknown-field hints.
READ_ONLY_POSTS = frozenset({"views/search/sync", "search/messages", "search/aggregate", "search/validate"})


class GraylogError(Exception):
    """An error with a message written for the model reading it."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class AuthError(GraylogError):
    pass


class PermissionDenied(GraylogError):
    pass


class QueryError(GraylogError):
    pass


class NotFound(GraylogError):
    pass


class UnsupportedVersion(GraylogError):
    pass


_POS_RE = re.compile(r"line (\d+), column (\d+)")


def describe_query_error(payload: Any, fallback: str) -> str:
    """Pull the useful part (message + position) out of Graylog's various error shapes."""
    parts: list[str] = []
    position = None

    def visit(obj: Any) -> None:
        nonlocal position
        if isinstance(obj, dict):
            for key in ("description", "message", "reason", "error"):
                val = obj.get(key)
                if isinstance(val, str) and val and val not in parts:
                    parts.append(val)
            line = obj.get("line", obj.get("begin_line"))
            col = obj.get("column", obj.get("begin_column"))
            if isinstance(line, int) and isinstance(col, int) and position is None:
                position = (line, col)
            for key in ("details", "errors", "caused_by", "root_cause"):
                if key in obj:
                    visit(obj[key])
        elif isinstance(obj, list):
            for item in obj[:5]:
                visit(item)
        elif isinstance(obj, str) and obj not in parts:
            parts.append(obj)

    visit(payload)
    text = "; ".join(p.strip() for p in parts if p.strip())[:800] or fallback
    if position is None:
        m = _POS_RE.search(text)
        if m:
            position = (int(m.group(1)), int(m.group(2)))
    if position:
        text += f" (at line {position[0]}, column {position[1]})"
    return text


class GraylogClient:
    def __init__(self, cfg: InstanceConfig, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    def _build(self) -> httpx.AsyncClient:
        cfg = self.cfg
        if cfg.auth == "token":
            auth = httpx.BasicAuth(cfg.token or "", "token")
        else:
            auth = httpx.BasicAuth(cfg.username or "", cfg.password or "")
        verify: ssl.SSLContext | bool
        if not cfg.verify_tls:
            verify = False
        elif cfg.ca_bundle:
            verify = ssl.create_default_context(cafile=cfg.ca_bundle)
        else:
            verify = True
        kwargs: dict[str, Any] = {}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        elif cfg.proxy:
            kwargs["proxy"] = cfg.proxy
        return httpx.AsyncClient(
            base_url=cfg.api_base,
            auth=auth,
            verify=verify,
            timeout=httpx.Timeout(cfg.timeout, connect=min(cfg.timeout, 10.0)),
            headers={
                "Accept": "application/json",
                "X-Requested-By": "graylog-mcp",
                "User-Agent": f"graylog-mcp/{__version__}",
            },
            follow_redirects=False,
            **kwargs,
        )

    @property
    def http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = self._build()
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, body: dict[str, Any], params: dict[str, Any] | None = None) -> Any:
        return await self.request("POST", path, params=params, json=body)

    async def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        path = path.lstrip("/")
        if method == "POST":
            if path not in READ_ONLY_POSTS:
                raise GraylogError(f"refusing POST {path}: not a read-only search endpoint")
        elif method != "GET":
            raise GraylogError(f"refusing {method} {path}: this server only reads from Graylog")
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            resp = await self.http.request(method, path, params=clean, json=json)
        except httpx.TimeoutException:
            raise GraylogError(
                f"timeout after {self.cfg.timeout:.0f}s calling {path} on instance '{self.cfg.name}'; "
                "narrow the time range or query, or raise 'timeout' for this instance"
            ) from None
        except httpx.ConnectError as exc:
            hint = ""
            if "CERTIFICATE" in str(exc).upper() or "SSL" in str(exc).upper():
                hint = " (TLS verification failed: set ca_bundle, or verify_tls = false for testing)"
            raise GraylogError(f"cannot connect to {self.cfg.url}: {exc}{hint}") from None
        except httpx.HTTPError as exc:
            raise GraylogError(f"HTTP error calling {path}: {exc}") from None
        return self._handle(resp, path)

    def _handle(self, resp: httpx.Response, path: str) -> Any:
        status = resp.status_code
        if 200 <= status < 300:
            if not resp.content:
                return None
            try:
                return resp.json()
            except ValueError:
                raise GraylogError(f"{path}: Graylog returned non-JSON content", status) from None
        try:
            payload: Any = resp.json()
        except ValueError:
            payload = resp.text[:500]
        name = self.cfg.name
        if status == 401:
            raise AuthError(
                f"authentication failed for instance '{name}' (401): check the access token or credentials",
                status,
            )
        if status == 403:
            raise PermissionDenied(
                f"permission denied (403) for {path} on instance '{name}': the Graylog user behind the token "
                f"lacks the permission for this endpoint. {describe_query_error(payload, '')}".strip(),
                status,
            )
        if status == 404:
            raise NotFound(f"not found (404): {path}", status)
        if status in (400, 422):
            raise QueryError(f"Graylog rejected the request: {describe_query_error(payload, resp.text[:300])}", status)
        if 300 <= status < 400:
            raise GraylogError(
                f"unexpected redirect ({status}) from {path}; check that the url points at the Graylog web/API root",
                status,
            )
        raise GraylogError(f"Graylog error {status} on {path}: {describe_query_error(payload, '')}", status)
