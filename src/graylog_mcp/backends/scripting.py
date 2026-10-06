"""Scripting API (``/search/messages``, ``/search/aggregate``), Graylog 5.2+.

Results are tabular (``schema`` + ``datarows``). The messages endpoint returns
only the requested fields, ``"-"`` for fields a message does not have, the message
id but not its index, and no total, so it is the last choice for message retrieval.
"""

from __future__ import annotations

from typing import Any

from graylog_mcp.backends.base import (
    AggResult,
    AggRow,
    MessagePage,
    MessageQuery,
    Metric,
    RawMessage,
    normalize_key,
)
from graylog_mcp.client import GraylogClient
from graylog_mcp.timerange import TimeRange

DEFAULT_FIELDS = ("timestamp", "source", "message", "level", "_id")
MISSING = "-"  # placeholder for fields absent from a message


def _columns(data: dict[str, Any]) -> list[dict[str, Any]]:
    return list(data.get("schema") or [])


class ScriptingBackend:
    name = "scripting"

    def __init__(self, client: GraylogClient):
        self.client = client

    async def search(self, mq: MessageQuery) -> MessagePage:
        fields = list(dict.fromkeys(("timestamp", "_id", *(mq.fields or DEFAULT_FIELDS))))
        body: dict[str, Any] = {
            "query": mq.query or "*",
            "timerange": mq.timerange.views(),
            "fields": fields,
            "from": mq.offset,
            "size": mq.limit,
            "sort": mq.sort_field,
            "sort_order": mq.sort_order,
        }
        if mq.streams:
            body["streams"] = list(mq.streams)
        data = await self.client.post("search/messages", body)
        cols = _columns(data)
        names = [c.get("field") or str(c.get("name", "")).removeprefix("field: ") for c in cols]
        messages = []
        for row in data.get("datarows") or []:
            fields_map = {n: v for n, v in zip(names, row, strict=False) if v is not None and v != MISSING}
            messages.append(RawMessage(fields=fields_map, index=None, id=fields_map.get("_id")))
        return MessagePage(messages=messages, total=None, api=self.name)

    async def aggregate(
        self,
        query: str,
        tr: TimeRange,
        streams: tuple[str, ...],
        group_by: list[str],
        limit: int,
        metrics: list[Metric],
    ) -> AggResult:
        body: dict[str, Any] = {
            "query": query or "*",
            "timerange": tr.views(),
            "group_by": [{"field": f, "limit": limit} for f in group_by],
            "metrics": [{"function": m.function, **({"field": m.field} if m.field else {})} for m in metrics],
        }
        if streams:
            body["streams"] = list(streams)
        data = await self.client.post("search/aggregate", body)
        cols = _columns(data)
        kinds: list[tuple[str, Any]] = []
        for col in cols:
            if col.get("column_type") == "metric":
                fn = str(col.get("function", "")).lower()
                kinds.append(("metric", Metric(fn, col.get("field") or None).name))
            else:
                kinds.append(("group", col.get("field")))
        rows = []
        for raw in data.get("datarows") or []:
            row = AggRow(keys=[])
            for (kind, name), value in zip(kinds, raw, strict=False):
                if kind == "group":
                    row.keys.append(normalize_key(value))
                else:
                    row.values[name] = value
            rows.append(row)
        return AggResult(rows=rows, total=None, api=self.name)
