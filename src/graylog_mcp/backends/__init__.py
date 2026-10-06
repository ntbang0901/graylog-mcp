"""Version detection and API selection.

On first use (eagerly at startup) the instance version is read from
``GET /api/system`` (or ``GET /api/`` when the token may not read system info),
then an ordered list of APIs is chosen:

==============  ==============================  ======================================
version         messages                        aggregations
==============  ==============================  ======================================
4.x - 5.1       universal -> views              views pivot
5.2 and later   universal -> views -> scripting  scripting aggregate -> views pivot
==============  ==============================  ======================================

An API answering 404/405/501 is dropped for the rest of the process and the
next one is used; the choice is shown by ``list_instances``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import difflib
import logging
import re
import time
from typing import Any

import httpx

from graylog_mcp.backends.base import (
    COUNT,
    AggResult,
    MessagePage,
    MessageQuery,
    Metric,
    RawMessage,
)
from graylog_mcp.backends.scripting import ScriptingBackend
from graylog_mcp.backends.universal import UniversalBackend
from graylog_mcp.backends.views import ViewsBackend
from graylog_mcp.client import (
    AuthError,
    GraylogClient,
    GraylogError,
    NotFound,
    PermissionDenied,
    QueryError,
    UnsupportedVersion,
)
from graylog_mcp.config import InstanceConfig
from graylog_mcp.timerange import TimeRange

log = logging.getLogger(__name__)

MIN_VERSION = (4, 0, 0)
STREAM_CACHE_SECONDS = 300
_OBJECT_ID = re.compile(r"^[0-9a-f]{24}$")


def parse_version(raw: str) -> tuple[int, int, int]:
    m = re.match(r"\s*v?(\d+)\.(\d+)(?:\.(\d+))?", raw or "")
    if not m:
        raise UnsupportedVersion(f"cannot parse Graylog version {raw!r}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)


def _query_failure(exc: GraylogError) -> bool:
    """Errors a malformed query may produce: 400, or a 500 from the search backend
    ('all shards failed' on 6.x+, 'Missing search type result!' on 4.x-5.x universal search)."""
    return isinstance(exc, QueryError) or exc.status == 500


def _looks_like_syntax(exc: GraylogError) -> bool:
    text = str(exc)
    return any(s in text for s in ("shards failed", "search_phase_execution", "parse", "Missing search type result"))


def _gone(exc: GraylogError) -> bool:
    """The endpoint does not exist on this version."""
    return isinstance(exc, NotFound) or exc.status in (405, 501)


class Graylog:
    def __init__(self, cfg: InstanceConfig, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self.client = GraylogClient(cfg, transport)
        self.version: tuple[int, int, int] | None = None
        self.version_raw: str | None = None
        self.detect_error: str | None = None
        self.message_apis: list[str] = []
        self.aggregation_apis: list[str] = []
        self._lock = asyncio.Lock()
        self._backends: dict[str, Any] = {}
        self._streams: list[dict[str, Any]] | None = None
        self._streams_at = 0.0

    # ------------------------------------------------------------------ detection

    async def detect(self) -> None:
        info: dict[str, Any] | None = None
        try:
            info = await self.client.get("system")
        except (NotFound, PermissionDenied):
            info = None
        if not info or "version" not in info:
            info = await self.client.get("")
        raw = str((info or {}).get("version", ""))
        version = parse_version(raw)
        if version < MIN_VERSION:
            raise UnsupportedVersion(
                f"Graylog {raw} on instance '{self.cfg.name}' is not supported (need 4.0 or later)"
            )
        self.version, self.version_raw = version, raw
        modern = version >= (5, 2, 0)
        msg = ["universal", "views", "scripting"] if modern else ["universal", "views"]
        agg = ["scripting", "views"] if modern else ["views"]
        if self.cfg.message_api != "auto":
            msg = [self.cfg.message_api]
        if self.cfg.aggregation_api != "auto":
            agg = [self.cfg.aggregation_api]
        self.message_apis, self.aggregation_apis = msg, agg
        self._backends = {
            "universal": UniversalBackend(self.client),
            "views": ViewsBackend(self.client, version),
            "scripting": ScriptingBackend(self.client),
        }
        self.detect_error = None
        log.info("instance %s: Graylog %s, messages via %s, aggregations via %s", self.cfg.name, raw, msg, agg)

    async def ensure(self) -> None:
        if self.version is not None:
            return
        async with self._lock:
            if self.version is not None:
                return
            try:
                await self.detect()
            except GraylogError as exc:
                self.detect_error = str(exc)
                raise

    async def aclose(self) -> None:
        await self.client.aclose()

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.cfg.name,
            "url": self.cfg.url,
            "auth": self.cfg.auth,
            "timezone": self.cfg.timezone,
        }
        if self.version is not None:
            out["version"] = self.version_raw
            out["message_api"] = self.message_apis[0] if self.message_apis else "none available"
            out["aggregation_api"] = self.aggregation_apis[0] if self.aggregation_apis else "none available"
            out["fallbacks"] = {"messages": self.message_apis[1:], "aggregations": self.aggregation_apis[1:]}
            out["status"] = "ok"
        else:
            out["status"] = f"not connected: {self.detect_error}" if self.detect_error else "not checked yet"
        return out

    # ------------------------------------------------------------------ query validation

    async def validate(self, query: str, tr: TimeRange, streams: tuple[str, ...] = ()) -> list[str]:
        """Problems Graylog finds in a query (syntax errors, unknown fields). Empty if none or unavailable."""
        if not query or query.strip() == "*" or self.version is None or self.version < (4, 3, 0):
            return []
        body = {"query": query, "timerange": tr.views(), "streams": list(streams)}
        try:
            data = await self.client.post("search/validate", body)
        except GraylogError:
            return []
        out = []
        for exp in (data or {}).get("explanations") or []:
            msg = str(exp.get("error_message") or exp.get("error_title") or "").strip()
            if not msg:
                continue
            if exp.get("begin_column") is not None and exp.get("error_type") == "QUERY_PARSING_ERROR":
                msg += f" (columns {exp.get('begin_column')}-{exp.get('end_column')})"
            out.append(msg)
        return out

    async def _explain(self, exc: GraylogError, query: str, tr: TimeRange, streams: tuple[str, ...]) -> None:
        if _query_failure(exc):
            problems = await self.validate(query, tr, streams)
            if problems:
                raise QueryError(f"invalid query {query!r}: {'; '.join(problems)}", exc.status) from None
            if not isinstance(exc, QueryError) and _looks_like_syntax(exc):
                raise QueryError(
                    f"Graylog could not run query {query!r} ({exc}). Check Lucene syntax: balanced parentheses "
                    "and quotes, upper-case AND/OR/NOT, quote values with special characters",
                    exc.status,
                ) from None

    # ------------------------------------------------------------------ messages

    async def search(self, mq: MessageQuery) -> MessagePage:
        await self.ensure()
        try:
            return await self._search(mq)
        except GraylogError as exc:
            await self._explain(exc, mq.query, mq.timerange, mq.streams)
            raise

    async def _search(self, mq: MessageQuery) -> MessagePage:
        last: GraylogError | None = None
        for name in list(self.message_apis):
            query = mq
            if name == "scripting" and mq.fields is None:
                # the Scripting API only returns the fields it is asked for
                query = dataclasses.replace(mq, fields=self.wanted_fields)
            try:
                return await self._backends[name].search(query)
            except GraylogError as exc:
                if not _gone(exc):
                    raise
                log.warning("instance %s: %s message API unavailable (%s); falling back", self.cfg.name, name, exc)
                self.message_apis.remove(name)
                last = exc
        raise GraylogError(f"no message search API is available on instance '{self.cfg.name}': {last}")

    @property
    def wanted_fields(self) -> tuple[str, ...]:
        cfg = self.cfg
        names = [
            *cfg.default_fields,
            *cfg.service_fields,
            *cfg.trace_fields,
            *cfg.group_fields.values(),
            "full_message",
            "streams",
        ]
        return tuple(dict.fromkeys(names))

    async def find_message(self, msg_id: str, tr: TimeRange) -> RawMessage | None:
        """Locate a message by id. Prefers the views search, which also reports the index."""
        await self.ensure()
        mq = MessageQuery(query=f'_id:"{msg_id}"', timerange=tr, limit=1)
        page = None
        try:
            page = await self._backends["views"].search(mq)
        except (AuthError, NotFound):
            raise
        except GraylogError:
            page = None
        if page is None or not page.messages:
            page = await self.search(mq)
        return page.messages[0] if page.messages else None

    async def get_message(self, index: str, msg_id: str) -> RawMessage:
        await self.ensure()
        data = await self.client.get(f"messages/{index}/{msg_id}")
        fields = dict((data or {}).get("message") or {})
        return RawMessage(fields=fields, index=data.get("index", index), id=fields.get("_id", msg_id))

    # ------------------------------------------------------------------ aggregations

    async def aggregate(
        self,
        query: str,
        tr: TimeRange,
        streams: tuple[str, ...],
        group_by: list[str],
        limit: int,
        metrics: list[Metric],
    ) -> AggResult:
        await self.ensure()
        try:
            return await self._aggregate(query, tr, streams, group_by, limit, metrics)
        except GraylogError as exc:
            await self._explain(exc, query, tr, streams)
            raise

    async def _aggregate(
        self,
        query: str,
        tr: TimeRange,
        streams: tuple[str, ...],
        group_by: list[str],
        limit: int,
        metrics: list[Metric],
    ) -> AggResult:
        last: GraylogError | None = None
        for name in list(self.aggregation_apis):
            try:
                return await self._backends[name].aggregate(query, tr, streams, group_by, limit, metrics)
            except (AuthError, PermissionDenied):
                raise
            except GraylogError as exc:
                if _gone(exc):
                    self.aggregation_apis.remove(name)
                    log.warning("instance %s: %s aggregation API unavailable; falling back", self.cfg.name, name)
                elif name != "views" and "views" in self.aggregation_apis:
                    log.info("instance %s: %s aggregation failed (%s); trying views pivot", self.cfg.name, name, exc)
                last = exc
        raise last or GraylogError(f"no aggregation API is available on instance '{self.cfg.name}'")

    async def histogram(self, query: str, tr: TimeRange, streams: tuple[str, ...], interval: str) -> AggResult:
        await self.ensure()
        try:
            return await self._backends["views"].histogram(query, tr, streams, interval)
        except GraylogError as exc:
            await self._explain(exc, query, tr, streams)
            raise

    async def count(self, query: str, tr: TimeRange, streams: tuple[str, ...]) -> int:
        await self.ensure()
        try:
            return await self._backends["views"].count(query, tr, streams)
        except AuthError:
            raise
        except GraylogError as exc:
            if not (_gone(exc) or isinstance(exc, PermissionDenied)):
                await self._explain(exc, query, tr, streams)
                raise
        page = await self.search(MessageQuery(query=query, timerange=tr, streams=streams, limit=1))
        if page.total is None:
            raise GraylogError("this instance cannot report exact counts (no views search or universal search)")
        return page.total

    async def count_by(self, query: str, tr: TimeRange, streams: tuple[str, ...], field: str, limit: int):
        return await self.aggregate(query, tr, streams, [field], limit, [COUNT])

    # ------------------------------------------------------------------ discovery

    async def streams(self, refresh: bool = False) -> list[dict[str, Any]]:
        await self.ensure()
        if refresh or self._streams is None or time.monotonic() - self._streams_at > STREAM_CACHE_SECONDS:
            data = await self.client.get("streams")
            self._streams = list((data or {}).get("streams") or [])
            self._streams_at = time.monotonic()
        return self._streams

    async def resolve_streams(self, names: list[str] | None) -> tuple[str, ...]:
        """Accept stream titles (case-insensitive) or ids; return ids."""
        if not names:
            return ()
        streams = await self.streams()
        by_id = {s.get("id"): s for s in streams}
        by_title = {str(s.get("title", "")).lower(): s for s in streams}
        out = []
        for name in names:
            key = name.strip()
            if key in by_id:
                out.append(key)
            elif key.lower() in by_title:
                out.append(by_title[key.lower()]["id"])
            elif _OBJECT_ID.match(key):
                out.append(key)  # not visible in the list (permissions) but may still be searchable
            else:
                titles = [str(s.get("title", "")) for s in streams]
                close = difflib.get_close_matches(key, titles, n=3, cutoff=0.5)
                hint = f" Did you mean: {', '.join(close)}?" if close else " Use list_streams to see stream names."
                raise GraylogError(f"unknown stream {name!r} on instance '{self.cfg.name}'.{hint}")
        return tuple(dict.fromkeys(out))

    async def fields(self) -> list[dict[str, Any]]:
        await self.ensure()
        try:
            data = await self.client.get("views/fields")
            out = []
            for item in data or []:
                ftype = item.get("type")
                if isinstance(ftype, dict):
                    ftype = ftype.get("type")
                out.append({"name": item.get("name"), "type": ftype})
            if out:
                return out
        except AuthError:
            raise
        except GraylogError:
            pass
        data = await self.client.get("system/fields")
        return [{"name": f, "type": None} for f in (data or {}).get("fields", [])]
