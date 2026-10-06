"""Views search (``POST /views/search/sync``), available 4.x and later.

The sync endpoint builds the search in memory and executes it; nothing is
saved. It provides exact aggregations through the ``pivot`` search type and
message lists through the ``messages`` search type.

Shape differences handled here:
  * 4.x pivots group by ``"field": "x"``; 5.0+ use ``"fields": ["x", ...]``.
"""

from __future__ import annotations

import secrets
import uuid
from typing import Any

from graylog_mcp.backends.base import (
    COUNT,
    AggResult,
    AggRow,
    MessagePage,
    MessageQuery,
    Metric,
    RawMessage,
    normalize_key,
)
from graylog_mcp.client import GraylogClient, QueryError, describe_query_error
from graylog_mcp.timerange import TimeRange


def _ids() -> tuple[str, str, str]:
    # search ids are Mongo ObjectIds; query and search type ids are free-form
    return secrets.token_hex(12), str(uuid.uuid4()), str(uuid.uuid4())


def build_search(query: str, tr: TimeRange, streams: tuple[str, ...], search_type: dict[str, Any]) -> dict:
    search_id, query_id, _ = _ids()
    q: dict[str, Any] = {
        "id": query_id,
        "query": {"type": "elasticsearch", "query_string": query or "*"},
        "timerange": tr.views(),
        "search_types": [search_type],
    }
    if streams:
        q["filter"] = {"type": "or", "filters": [{"type": "stream", "id": s} for s in streams]}
    return {"id": search_id, "queries": [q], "parameters": []}


def _series(m: Metric) -> dict[str, Any]:
    out: dict[str, Any] = {"type": m.function, "id": m.name}
    if m.field:
        out["field"] = m.field
    return out


def build_pivot(
    version: tuple[int, int, int],
    row_fields: list[str],
    limit: int,
    metrics: list[Metric],
    interval: str | None = None,
) -> dict[str, Any]:
    """Pivot search type. ``interval`` turns the first row group into a date histogram on timestamp."""
    modern = version >= (5, 0, 0)
    row_groups: list[dict[str, Any]] = []
    if interval:
        group: dict[str, Any] = {"type": "time", "interval": {"type": "timeunit", "timeunit": interval}}
        if modern:
            group["fields"] = ["timestamp"]
        else:
            group["field"] = "timestamp"
        row_groups.append(group)
    if row_fields:
        if modern:
            row_groups.append({"type": "values", "fields": list(row_fields), "limit": limit})
        else:
            row_groups.extend({"type": "values", "field": f, "limit": limit} for f in row_fields)
    return {
        "id": str(uuid.uuid4()),
        "type": "pivot",
        "row_groups": row_groups,
        "column_groups": [],
        "series": [_series(m) for m in metrics],
        "sort": [],
        "rollup": True,
    }


def _result(data: dict[str, Any], search_type_id: str) -> dict[str, Any]:
    errors = list(data.get("errors") or [])
    results = data.get("results") or {}
    for res in results.values():
        errors.extend(res.get("errors") or [])
        st = (res.get("search_types") or {}).get(search_type_id)
        if st is not None and not errors:
            return st
    if errors:
        raise QueryError(f"Graylog rejected the query: {describe_query_error(errors, 'search failed')}")
    raise QueryError("Graylog returned no result for the search (empty response)")


def parse_pivot(st: dict[str, Any]) -> AggResult:
    rows: list[AggRow] = []
    total = st.get("total")
    for row in st.get("rows") or []:
        values = {}
        for v in row.get("values") or []:
            key = v.get("key") or []
            if key:
                values[str(key[-1])] = v.get("value")
        if row.get("source") == "leaf":
            rows.append(AggRow(keys=[normalize_key(k) for k in row.get("key") or []], values=values))
        elif not row.get("key") and COUNT.name in values and isinstance(values[COUNT.name], int | float):
            total = int(values[COUNT.name])  # rollup row of the whole result
    return AggResult(rows=rows, total=total if isinstance(total, int) else None, api="views")


class ViewsBackend:
    name = "views"

    def __init__(self, client: GraylogClient, version: tuple[int, int, int]):
        self.client = client
        self.version = version

    async def _execute(self, body: dict[str, Any], st_id: str) -> dict[str, Any]:
        timeout_ms = int(self.client.cfg.timeout * 1000)
        data = await self.client.post("views/search/sync", body, params={"timeout": timeout_ms})
        return _result(data or {}, st_id)

    async def search(self, mq: MessageQuery) -> MessagePage:
        st = {
            "id": str(uuid.uuid4()),
            "type": "messages",
            "limit": mq.limit,
            "offset": mq.offset,
            "sort": [{"field": mq.sort_field, "order": mq.sort_order.upper()}],
        }
        result = await self._execute(build_search(mq.query, mq.timerange, mq.streams, st), st["id"])
        messages = []
        for item in result.get("messages") or []:
            fields = dict(item.get("message") or {})
            messages.append(RawMessage(fields=fields, index=item.get("index"), id=fields.get("_id")))
        total = result.get("total_results", result.get("total"))
        return MessagePage(messages=messages, total=total if isinstance(total, int) else None, api=self.name)

    async def aggregate(
        self,
        query: str,
        tr: TimeRange,
        streams: tuple[str, ...],
        group_by: list[str],
        limit: int,
        metrics: list[Metric],
    ) -> AggResult:
        st = build_pivot(self.version, group_by, limit, metrics)
        return parse_pivot(await self._execute(build_search(query, tr, streams, st), st["id"]))

    async def histogram(self, query: str, tr: TimeRange, streams: tuple[str, ...], interval: str) -> AggResult:
        st = build_pivot(self.version, [], 0, [COUNT], interval=interval)
        return parse_pivot(await self._execute(build_search(query, tr, streams, st), st["id"]))

    async def count(self, query: str, tr: TimeRange, streams: tuple[str, ...]) -> int:
        st = build_pivot(self.version, [], 0, [COUNT])
        result = parse_pivot(await self._execute(build_search(query, tr, streams, st), st["id"]))
        if result.total is not None:
            return result.total
        return int(sum(r.values.get(COUNT.name, 0) or 0 for r in result.rows))
