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
    normalize_value,
)
from graylog_mcp.client import GraylogClient
from graylog_mcp.timerange import TimeRange

DEFAULT_FIELDS = ("timestamp", "source", "message", "level", "_id")
MISSING = "-"  # placeholder for fields absent from a message


def _columns(data: dict[str, Any]) -> list[dict[str, Any]]:
    return list(data.get("schema") or [])


def _metric(m: Metric) -> dict[str, Any]:
    out: dict[str, Any] = {"function": m.function}
    if m.field:
        out["field"] = m.field
    if m.percentile is not None:
        out["configuration"] = {"percentile": m.percentile}
    if m.sort:
        out["sort"] = m.sort
    return out


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
            "metrics": [_metric(m) for m in metrics],
        }
        if streams:
            body["streams"] = list(streams)
        data = await self.client.post("search/aggregate", body)
        cols = _columns(data)
        metric_cols = [c for c in cols if c.get("column_type") == "metric"]
        # Every percentile of a field gets the same column name ("percentile(took_ms)"), so the columns are matched
        # to the metrics asked for by position; Graylog keeps their order.
        if len(metric_cols) == len(metrics):
            names = iter([m.name for m in metrics])
        else:
            names = iter([Metric(str(c.get("function", "")).lower(), c.get("field") or None).name for c in metric_cols])
        kinds = [("metric", next(names)) if c.get("column_type") == "metric" else ("group", "") for c in cols]
        rows = []
        for raw in data.get("datarows") or []:
            row = AggRow(keys=[])
            for (kind, name), value in zip(kinds, raw, strict=False):
                if kind == "group":
                    row.keys.append(normalize_key(value))
                else:
                    row.values[name] = normalize_value(value)
            rows.append(row)
        return AggResult(rows=rows, total=None, api=self.name)
