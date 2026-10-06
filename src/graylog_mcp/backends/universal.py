"""Legacy universal search (``/search/universal/*``), available 4.x through 6.x.

Only message retrieval is used: on 5.0 the ``/terms``, ``/histogram`` and
``/stats`` sub-resources are gone, so aggregations go through views or the
Scripting API instead.
"""

from __future__ import annotations

from graylog_mcp.backends.base import MessagePage, MessageQuery, RawMessage, stream_query
from graylog_mcp.client import GraylogClient


class UniversalBackend:
    name = "universal"

    def __init__(self, client: GraylogClient):
        self.client = client

    async def search(self, mq: MessageQuery) -> MessagePage:
        params: dict[str, object] = {
            "from": mq.timerange.graylog_from(),
            "to": mq.timerange.graylog_to(),
            "limit": mq.limit,
            "offset": mq.offset,
            "sort": f"{mq.sort_field}:{mq.sort_order}",
            "decorate": "false",
        }
        if len(mq.streams) == 1:
            params["query"] = mq.query or "*"
            params["filter"] = f"streams:{mq.streams[0]}"
        else:
            params["query"] = stream_query(mq.query, mq.streams)
        data = await self.client.get("search/universal/absolute", params)
        messages = []
        for item in data.get("messages", []) or []:
            fields = dict(item.get("message") or {})
            messages.append(RawMessage(fields=fields, index=item.get("index"), id=fields.get("_id")))
        total = data.get("total_results")
        return MessagePage(messages=messages, total=total if isinstance(total, int) else None, api=self.name)
