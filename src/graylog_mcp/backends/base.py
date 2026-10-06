"""Backend-neutral request and result types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from graylog_mcp.timerange import TimeRange


@dataclass(frozen=True)
class MessageQuery:
    query: str
    timerange: TimeRange
    streams: tuple[str, ...] = ()
    fields: tuple[str, ...] | None = None  # None: all fields
    sort_field: str = "timestamp"
    sort_order: str = "desc"  # "asc" | "desc"
    limit: int = 50
    offset: int = 0


@dataclass
class RawMessage:
    fields: dict[str, Any]
    index: str | None = None
    id: str | None = None


@dataclass
class MessagePage:
    messages: list[RawMessage]
    total: int | None  # None when the API does not report it (Scripting API)
    api: str = ""


@dataclass(frozen=True)
class Metric:
    function: str  # count | min | max | avg | sum | card
    field: str | None = None

    @property
    def name(self) -> str:
        return f"{self.function}({self.field or ''})"


COUNT = Metric("count")


@dataclass
class AggRow:
    keys: list[Any]
    values: dict[str, Any] = field(default_factory=dict)


# Bucket Graylog returns for documents without the grouped field (5.x+ pivots).
MISSING_MARKERS = frozenset({"(Empty Value)"})


def normalize_key(value: Any) -> Any:
    return None if value in MISSING_MARKERS else value


@dataclass
class AggResult:
    rows: list[AggRow]
    total: int | None = None  # all matching documents, grouped or not
    api: str = ""

    def split_missing(self, metric: str = "count()") -> tuple[list[AggRow], int]:
        """Rows sorted by count, without the bucket of documents lacking the field; plus that bucket's count."""
        present, missing = [], 0
        for row in self.rows:
            if not row.keys or any(k is None for k in row.keys):
                missing += int(row.values.get(metric) or 0)
            else:
                present.append(row)
        present.sort(key=lambda r: -(r.values.get(metric) or 0))
        return present, missing


def stream_query(query: str, streams: tuple[str, ...]) -> str:
    """Restrict a Lucene query to streams (used where the API takes a single filter)."""
    if not streams:
        return query
    clause = " OR ".join(f"streams:{s}" for s in streams)
    base = query.strip() or "*"
    return f"({base}) AND ({clause})" if base != "*" else f"({clause})"
