"""In-memory Graylog emulator for contract tests (httpx.MockTransport).

It reproduces the response *shapes* of the endpoints the server uses, per
version family:

* 4.x   universal search, views pivots with ``"field": "x"``
* 5.0   same, pivots with ``"fields": [...]``; no scripting API
* 5.2+  adds the Scripting API (/search/messages, /search/aggregate)
* 7.x   universal search removed (emulated: real 7.0 still has it), to exercise the fallback

A tiny Lucene subset is evaluated so that results are meaningful: ``*``,
``field:value``, ``field:"phrase"``, ``field:<=N`` / ``>=`` / ``<`` / ``>``,
``field:[a TO b]``, ``_exists_:f``, bare terms/phrases (full text on message),
AND / OR / NOT and parentheses.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

# --------------------------------------------------------------------------- lucene subset

_TOKEN = re.compile(
    r"\s*(?:(?P<lp>\()|(?P<rp>\))|(?P<op>AND|OR|NOT)(?=[\s()]|$)|"
    r'(?P<term>(?:[^\s()"\\:]|\\.)+:(?:"(?:[^"\\]|\\.)*"|\[[^\]]*\]|[^\s()]+)|"(?:[^"\\]|\\.)*"|[^\s()]+))'
)


class LuceneError(ValueError):
    pass


def _tokens(q: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    q = q.strip()
    while pos < len(q):
        m = _TOKEN.match(q, pos)
        if not m or m.end() == pos:
            raise LuceneError(f"Cannot parse '{q}': Encountered \" <EOF> \" at line 1, column {pos}.")
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
        pos = m.end()
        while pos < len(q) and q[pos].isspace():
            pos += 1
    return out


def _unescape(s: str) -> str:
    return re.sub(r"\\(.)", r"\1", s)


def _term_pred(term: str):
    if term == "*":
        return lambda m: True
    field = None
    value = term
    m = re.match(r'^((?:[^\s()"\\:]|\\.)+):(.*)$', term)
    if m:
        field, value = _unescape(m.group(1)), m.group(2)
    if field == "_exists_":
        return lambda msg: msg.get(value) not in (None, "")
    if value.startswith('"') and value.endswith('"') and len(value) >= 2:
        text = _unescape(value[1:-1])
        if field is None:
            return lambda msg: text.lower() in str(msg.get("message", "")).lower()
        return lambda msg: _eq(msg.get(field), text, phrase=True)
    rng = re.match(r"^\[(\S+) TO (\S+)\]$", value)
    if rng and field:
        lo, hi = float(rng.group(1)), float(rng.group(2))
        return lambda msg: _num(msg.get(field)) is not None and lo <= _num(msg.get(field)) <= hi
    cmp = re.match(r"^(<=|>=|<|>)(-?\d+(?:\.\d+)?)$", value)
    if cmp and field:
        op, n = cmp.group(1), float(cmp.group(2))
        ops = {"<=": float.__le__, ">=": float.__ge__, "<": float.__lt__, ">": float.__gt__}
        return lambda msg: _num(msg.get(field)) is not None and ops[op](_num(msg.get(field)), n)
    if field is None:
        word = _unescape(value).lower()
        return lambda msg: word in str(msg.get("message", "")).lower()
    if value.endswith("*"):
        prefix = _unescape(value[:-1]).lower()
        return lambda msg: str(msg.get(field, "")).lower().startswith(prefix)
    val = _unescape(value)
    return lambda msg: _eq(msg.get(field), val)


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _eq(actual: Any, expected: str, phrase: bool = False) -> bool:
    if actual is None:
        return False
    if isinstance(actual, list):
        return any(_eq(a, expected, phrase) for a in actual)
    a = str(actual)
    if a == expected or a.lower() == expected.lower():
        return True
    return phrase and expected.lower() in a.lower()


_FIELD_GROUP = re.compile(r'((?:[^\s()"\\:]|\\.)+):\(([^()]*)\)')


def _expand_field_groups(q: str) -> str:
    """field:(a OR "b c") -> (field:a OR field:"b c")"""

    def expand(m: re.Match[str]) -> str:
        field, inner = m.group(1), m.group(2)
        parts = re.findall(r'"(?:[^"\\]|\\.)*"|\S+', inner)
        return "(" + " ".join(p if p in ("AND", "OR", "NOT") else f"{field}:{p}" for p in parts) + ")"

    return _FIELD_GROUP.sub(expand, q)


def compile_query(q: str):
    q = _expand_field_groups(q or "*")
    toks = _tokens(q) or [("term", "*")]
    pos = 0

    def peek():
        return toks[pos] if pos < len(toks) else (None, None)

    def take():
        nonlocal pos
        pos += 1
        return toks[pos - 1]

    def parse_or():
        left = parse_and()
        while peek() == ("op", "OR"):
            take()
            right = parse_and()
            left = (lambda a, b: lambda m: a(m) or b(m))(left, right)
        return left

    def parse_and():
        left = parse_not()
        while True:
            kind, val = peek()
            if kind == "op" and val == "AND":
                take()
            elif kind in ("term", "lp") or (kind == "op" and val == "NOT"):
                pass  # implicit AND (Graylog default operator)
            else:
                return left
            right = parse_not()
            left = (lambda a, b: lambda m: a(m) and b(m))(left, right)

    def parse_not():
        if peek() == ("op", "NOT"):
            take()
            inner = parse_atom()
            return lambda m: not inner(m)
        return parse_atom()

    def parse_atom():
        kind, val = peek()
        if kind == "lp":
            take()
            inner = parse_or()
            if peek()[0] != "rp":
                raise LuceneError(f"Cannot parse '{q}': Encountered \"<EOF>\" at line 1, column {len(q)}.")
            take()
            return inner
        if kind == "term":
            take()
            return _term_pred(val)
        raise LuceneError(f"Cannot parse '{q}': unexpected {val!r} at line 1, column {pos}.")

    pred = parse_or()
    if pos != len(toks):
        raise LuceneError(f"Cannot parse '{q}': unexpected input at line 1, column {pos}.")
    return pred


# --------------------------------------------------------------------------- dataset

STREAMS = [
    {"id": "000000000000000000000001", "title": "All messages", "description": "default", "disabled": False},
    {"id": "5f0c0a1b2c3d4e5f60718293", "title": "Payments", "description": "payment services", "disabled": False},
    {"id": "5f0c0a1b2c3d4e5f60718294", "title": "Old", "description": "", "disabled": True},
]
PAY = "5f0c0a1b2c3d4e5f60718293"
ALL = "000000000000000000000001"

JAVA_TRACE = "\n".join(
    [
        "java.lang.IllegalStateException: card declined for user alice@example.com",
        "\tat com.acme.pay.PaymentService.charge(PaymentService.java:42)",
        *[f"\tat org.springframework.aop.Proxy{i}.invoke(Proxy.java:{i})" for i in range(30)],
        "\tat com.acme.pay.PaymentController.post(PaymentController.java:17)",
        *[f"\tat org.apache.catalina.Valve{i}.invoke(Valve.java:{i})" for i in range(20)],
        "Caused by: java.net.SocketTimeoutException: Read timed out",
        "\tat java.net.SocketInputStream.read(SocketInputStream.java:150)",
        "\t... 52 more",
    ]
)


def make_dataset(now: datetime) -> list[dict[str, Any]]:
    """~120 messages over the last 2 hours with a trace, an error spike and sensitive data."""
    msgs: list[dict[str, Any]] = []

    def add(seconds_ago: float, **fields: Any) -> None:
        ts = now - timedelta(seconds=seconds_ago)
        mid = str(uuid.uuid5(uuid.NAMESPACE_OID, f"{seconds_ago}-{len(msgs)}"))
        msg = {
            "_id": mid,
            "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z",
            "streams": fields.pop("streams", [ALL]),
            "gl2_source_input": "abc",
            "gl2_message_id": "01HX" + mid[:8],
        }
        msg.update(fields)
        msgs.append(msg)

    # steady info traffic, 1/minute, two hours
    for i in range(120):
        add(
            i * 60 + 5,
            source="web-1" if i % 2 else "web-2",
            level=6,
            service="gateway",
            message=f"GET /api/orders/{1000 + i} 200 in {10 + i % 7}ms",
            http_status=200,
        )
    # a trace across three services, 4 minutes ago
    tid = "req-7f3a9c"
    add(240.000, source="web-1", level=6, service="gateway", trace_id=tid, message="POST /api/pay received")
    add(239.950, source="pay-1", level=6, service="payment", trace_id=tid, message="charging card 4111 1111 1111 1111")
    add(
        239.700,
        source="pay-1",
        level=6,
        service="payment",
        trace_id=tid,
        message="calling bank api token=sk_live_abc123",
    )
    add(
        239.200,
        source="pay-1",
        level=3,
        service="payment",
        trace_id=tid,
        message="payment failed",
        full_message=JAVA_TRACE,
        exception_class="java.lang.IllegalStateException",
        logger_name="com.acme.pay.PaymentService",
        streams=[ALL, PAY],
    )
    add(239.100, source="notify-1", level=4, service="notify", trace_id=tid, message="sms to 0912345678 queued")
    add(239.000, source="web-1", level=6, service="gateway", trace_id=tid, message="POST /api/pay 502 in 1000ms")
    # error spike in the last 10 minutes: timeouts (new), plus an old steady DB error
    for i in range(25):
        add(
            30 + i * 20,
            source="pay-1" if i % 3 else "pay-2",
            level=3,
            service="payment",
            message=f"upstream timeout after {3000 + i}ms calling bank for order {5000 + i}",
            exception_class="java.net.SocketTimeoutException",
            logger_name="com.acme.pay.BankClient",
            password="hunter2",
            user_email=f"user{i}@example.com",
            streams=[ALL, PAY],
        )
    for i in range(6):
        add(
            600 + i * 1000,
            source="db-1",
            level=3,
            service="orders",
            message=f"deadlock detected on table orders (tx {i})",
            exception_class="org.postgresql.util.PSQLException",
            logger_name="com.acme.orders.Repo",
        )
    msgs.sort(key=lambda m: m["timestamp"], reverse=True)
    return msgs


INCIDENT = {
    "deploy_before_end": timedelta(minutes=15),
    "onset_before_end": timedelta(minutes=12),
    "length": timedelta(minutes=150),
}


def make_incident_dataset(end: datetime) -> list[dict[str, Any]]:
    """2.5 hours of traffic ending at ``end`` with a bad deploy of 'payment'.

    * gateway -> payment -> bank-adapter (checkout, every 10s) and
      gateway -> orders -> postgres (orders, every 30s), each request a trace;
    * payment 1.3.9 on pay-1/pay-2 is replaced by 1.4.0 on pay-3/pay-4 15 minutes before the end
      (shutdown and Spring Boot start lines included);
    * 3 minutes later every checkout fails in payment ("connection refused bank-v2.internal"),
      gateway answers 502 about 50ms later, and bank-adapter stops receiving requests;
    * orders/postgres stay healthy apart from a steady trickle of deadlocks.
    """
    msgs: list[dict[str, Any]] = []
    start = end - INCIDENT["length"]
    deploy = end - INCIDENT["deploy_before_end"]
    onset = end - INCIDENT["onset_before_end"]

    def add(ts: datetime, service: str, source: str, message: str, level: int = 6, **fields: Any) -> None:
        if ts >= end:
            return
        mid = str(uuid.uuid5(uuid.NAMESPACE_OID, f"inc-{ts.isoformat()}-{source}-{len(msgs)}"))
        msgs.append(
            {
                "_id": mid,
                "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z",
                "streams": [ALL],
                "service": service,
                "source": source,
                "level": level,
                "message": message,
                "scenario": "incident",
                **fields,
            }
        )

    ms = lambda n: timedelta(milliseconds=n)  # noqa: E731
    n = 0
    t = start
    while t < end:
        new = t >= deploy
        pay = f"pay-{3 + n % 2}" if new else f"pay-{1 + n % 2}"
        ver = "1.4.0" if new else "1.3.9"
        tid = f"co-{n}"
        add(t, "gateway", "gw-1", "POST /api/checkout received", trace_id=tid, app_version="5.2.0")
        add(t + ms(20), "payment", pay, f"charging order {n}", trace_id=tid, app_version=ver)
        if t < onset:
            add(t + ms(120), "bank-adapter", "bank-1", "authorize ok", trace_id=tid, app_version="2.0.1", took_ms=90)
            add(t + ms(300), "payment", pay, "charge ok", trace_id=tid, app_version=ver, took_ms=280)
            add(
                t + ms(350), "gateway", "gw-1", "POST /api/checkout 200", trace_id=tid, app_version="5.2.0", took_ms=350
            )
        else:
            add(
                t + ms(2020),
                "payment",
                pay,
                "bank call failed: connection refused bank-v2.internal:443",
                level=3,
                trace_id=tid,
                app_version=ver,
                exception_class="java.net.ConnectException",
                took_ms=2000,
            )
            add(t + ms(2070), "gateway", "gw-1", "POST /api/checkout 502", level=3, trace_id=tid,
                app_version="5.2.0", took_ms=2070)  # fmt: skip
        if n % 3 == 0:
            oid = f"or-{n}"
            add(t + ms(5000), "gateway", "gw-1", "GET /api/orders received", trace_id=oid, app_version="5.2.0")
            add(t + ms(5010), "orders", "ord-1", "loading orders", trace_id=oid, app_version="3.1.0")
            add(t + ms(5020), "postgres", "db-1", "query ok", trace_id=oid, took_ms=6)
            add(t + ms(5040), "orders", "ord-1", "orders loaded", trace_id=oid, app_version="3.1.0", took_ms=30)
            add(t + ms(5050), "gateway", "gw-1", "GET /api/orders 200", trace_id=oid, app_version="5.2.0", took_ms=50)
        if n % 120 == 60:
            add(t + ms(7000), "postgres", "db-1", "deadlock detected", level=3, exception_class="PSQLException")
        n += 1
        t += timedelta(seconds=10)
    for host in ("pay-1", "pay-2"):
        add(deploy - timedelta(seconds=5), "payment", host, "Graceful shutdown initiated, SIGTERM received",
            app_version="1.3.9")  # fmt: skip
    for host in ("pay-3", "pay-4"):
        add(deploy - ms(500), "payment", host, "Started PaymentApplication in 4.2 seconds (process running for 5.1)",
            app_version="1.4.0")  # fmt: skip
    msgs.sort(key=lambda m: m["timestamp"], reverse=True)
    return msgs


# --------------------------------------------------------------------------- emulator


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class FakeGraylog:
    def __init__(self, version: str = "5.0.13+083613e", now: datetime | None = None, dataset: str = "basic"):
        self.version = version
        self.major_minor = tuple(int(x) for x in version.split("+")[0].split(".")[:2])
        self.now = now or datetime.now(UTC)
        self.messages = make_incident_dataset(self.now) if dataset == "incident" else make_dataset(self.now)
        self.requests: list[httpx.Request] = []
        self.system_forbidden = False

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # ---------------------------------------------------------------- helpers

    @property
    def has_scripting(self) -> bool:
        return self.major_minor >= (5, 2)

    @property
    def has_universal(self) -> bool:
        return self.major_minor < (7, 0)

    @property
    def modern_pivot(self) -> bool:
        return self.major_minor >= (5, 0)

    def select(self, query: str, frm: datetime, to: datetime, streams: list[str] | None = None) -> list[dict]:
        pred = compile_query(query)
        out = []
        for m in self.messages:
            ts = _parse_ts(m["timestamp"])
            if not (frm <= ts <= to):
                continue
            if streams and not set(streams) & set(m.get("streams", [])):
                continue
            if pred(m):
                out.append(m)
        return out

    def _sort(self, msgs: list[dict], field: str, order: str) -> list[dict]:
        return sorted(msgs, key=lambda m: str(m.get(field, "")), reverse=order.lower() == "desc")

    @staticmethod
    def _index(m: dict) -> str:
        return "graylog_3"

    @staticmethod
    def json(status: int, body: Any) -> httpx.Response:
        return httpx.Response(status, content=json.dumps(body).encode(), headers={"content-type": "application/json"})

    # ---------------------------------------------------------------- routing

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = urlparse(str(request.url))
        path = url.path
        if not path.startswith("/api/"):
            return self.json(404, {"message": "not found"})
        path = path[len("/api/") :]
        qs = {k: v[0] for k, v in parse_qs(url.query).items()}
        if request.headers.get("authorization") is None:
            return self.json(401, {"message": "unauthorized"})
        try:
            if request.method == "GET":
                return self.get(path, qs)
            if request.method == "POST":
                if request.headers.get("x-requested-by") is None:
                    return self.json(400, {"message": "CSRF header missing"})
                return self.post(path, json.loads(request.content or b"{}"))
        except LuceneError as exc:
            return self.json(400, {"type": "ApiError", "message": str(exc)})
        return self.json(405, {"message": "method not allowed"})

    def get(self, path: str, qs: dict[str, str]) -> httpx.Response:
        if path == "":
            return self.json(200, {"cluster_id": "c", "node_id": "n", "version": self.version, "tagline": "logs"})
        if path == "system":
            if self.system_forbidden:
                return self.json(403, {"message": "forbidden"})
            return self.json(200, {"version": self.version, "hostname": "gl", "lifecycle": "running"})
        if path == "streams":
            return self.json(200, {"total": len(STREAMS), "streams": STREAMS})
        if path == "system/fields":
            names = sorted({k for m in self.messages for k in m})
            return self.json(200, {"fields": names})
        if path == "views/fields":
            names = sorted({k for m in self.messages for k in m})
            return self.json(
                200,
                [{"name": n, "type": {"type": self._field_type(n)}, "streams": []} for n in names],
            )
        if path.startswith("messages/"):
            _, index, mid = path.split("/", 2)
            for m in self.messages:
                if m["_id"] == mid:
                    return self.json(200, {"message": m, "index": index})
            return self.json(404, {"message": f"Message {mid} does not exist in index {index}"})
        if path == "search/universal/absolute" and self.has_universal:
            return self.universal(qs)
        if path in ("search/universal/terms", "search/universal/histogram", "search/universal/stats"):
            return self.json(404, {"message": "HTTP 404 Not Found"})
        return self.json(404, {"message": "HTTP 404 Not Found"})

    def _field_type(self, name: str) -> str:
        if name == "timestamp":
            return "date"
        value = next((m[name] for m in self.messages if name in m), None)
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, int):
            return "long"
        if isinstance(value, float):
            return "double"
        return "string"

    def post(self, path: str, body: dict) -> httpx.Response:
        if path == "views/search/sync":
            return self.views(body)
        if path == "search/messages" and self.has_scripting:
            return self.scripting_messages(body)
        if path == "search/aggregate" and self.has_scripting:
            return self.scripting_aggregate(body)
        if path == "search/validate" and self.major_minor >= (4, 3):
            return self.validate(body)
        return self.json(404, {"message": "HTTP 404 Not Found"})

    def validate(self, body: dict) -> httpx.Response:
        query = body.get("query", "")
        try:
            compile_query(query)
        except LuceneError as exc:
            exp = {"error_type": "QUERY_PARSING_ERROR", "begin_line": 1, "begin_column": 0, "end_line": 1,
                   "end_column": len(query), "error_title": "Query parsing error",
                   "error_message": f"Cannot parse query, cause: {exc}"}  # fmt: skip
            return self.json(200, {"status": "ERROR", "explanations": [exp]})
        known = {k for m in self.messages for k in m} | {"_exists_", "_id"}
        unknown = [f for f in re.findall(r"(?<![\w\\])([A-Za-z_][\w.]*):", query) if f not in known]
        exps = [
            {
                "error_type": "UNKNOWN_FIELD",
                "error_title": "Unknown field",
                "error_message": f"Query contains unknown field: {f}",
                "related_property": f,
            }
            for f in unknown
        ]
        return self.json(200, {"status": "WARNING" if exps else "OK", "explanations": exps})

    # ---------------------------------------------------------------- universal

    def universal(self, qs: dict[str, str]) -> httpx.Response:
        query = qs.get("query", "*")
        streams = None
        if qs.get("filter", "").startswith("streams:"):
            streams = [qs["filter"].split(":", 1)[1]]
        try:
            hits = self.select(query, _parse_ts(qs["from"]), _parse_ts(qs["to"]), streams)
        except LuceneError as exc:
            return self.json(
                400,
                {
                    "message": "Unable to execute search",
                    "details": [str(exc)],
                    "query": query,
                    "begin_line": 1,
                    "begin_column": 3,
                },
            )
        field, _, order = qs.get("sort", "timestamp:desc").partition(":")
        hits = self._sort(hits, field, order)
        offset, limit = int(qs.get("offset", 0)), int(qs.get("limit", 150))
        page = hits[offset : offset + limit]
        return self.json(
            200,
            {
                "query": query,
                "built_query": "{}",
                "used_indices": [],
                "messages": [
                    {"highlight_ranges": {}, "message": dict(m), "index": self._index(m), "decoration_stats": None}
                    for m in page
                ],
                "fields": [],
                "time": 3,
                "total_results": len(hits),
                "from": qs["from"],
                "to": qs["to"],
            },
        )

    # ---------------------------------------------------------------- views

    def views(self, body: dict) -> httpx.Response:
        results = {}
        for q in body["queries"]:
            tr = q["timerange"]
            frm, to = _parse_ts(tr["from"]), _parse_ts(tr["to"])
            streams = [f["id"] for f in (q.get("filter") or {}).get("filters", [])] or None
            qs = q["query"]["query_string"]
            try:
                hits = self.select(qs, frm, to, streams)
            except LuceneError as exc:
                results[q["id"]] = {
                    "query": q,
                    "search_types": {},
                    "errors": [{"description": str(exc), "type": "query", "query_id": q["id"]}],
                    "state": "FAILED",
                }
                continue
            sts = {}
            for st in q["search_types"]:
                if st["type"] == "messages":
                    sort = (st.get("sort") or [{"field": "timestamp", "order": "DESC"}])[0]
                    ordered = self._sort(hits, sort["field"], sort["order"])
                    off, lim = st.get("offset", 0), st.get("limit", 150)
                    sts[st["id"]] = {
                        "id": st["id"],
                        "type": "messages",
                        "messages": [{"message": dict(m), "index": self._index(m)} for m in ordered[off : off + lim]],
                        "total_results": len(hits),
                    }
                elif st["type"] == "pivot":
                    err = self._check_pivot(st)
                    if err:
                        return self.json(400, {"type": "ApiError", "message": err})
                    sts[st["id"]] = self.pivot(st, hits)
            results[q["id"]] = {"query": q, "search_types": sts, "errors": [], "state": "COMPLETED"}
        return self.json(
            200,
            {
                "id": body.get("id"),
                "search_id": body.get("id"),
                "owner": "admin",
                "execution": {"done": True, "cancelled": False, "completed_exceptionally": False},
                "results": results,
            },
        )

    def _check_pivot(self, st: dict) -> str | None:
        for g in st.get("row_groups", []):
            if self.modern_pivot and "fields" not in g:
                return 'Unrecognized field "field"; pivots use "fields" since 5.0'
            if not self.modern_pivot and "field" not in g:
                return 'Unrecognized field "fields" (4.x pivots take "field")'
        return None

    def _group_keys(self, st: dict) -> list[tuple[str, Any]]:
        keys = []
        for g in st.get("row_groups", []):
            fields = g["fields"] if self.modern_pivot else [g["field"]]
            for f in fields:
                keys.append((g["type"], f, g))
        return keys

    @staticmethod
    def _metric(series: dict, msgs: list[dict]) -> Any:
        t = series["type"]
        if t == "count":
            return len(msgs)
        vals = [m.get(series["field"]) for m in msgs if m.get(series["field"]) is not None]
        if series["field"] == "timestamp":
            vals = [_parse_ts(v).timestamp() * 1000 for v in vals]
        if not vals:
            return None
        if t == "min":
            return min(vals)
        if t == "max":
            return max(vals)
        if t == "latest":
            return vals[0]
        if t == "avg":
            nums = [float(v) for v in vals if isinstance(v, int | float)]
            return sum(nums) / len(nums) if nums else None
        raise ValueError(t)

    def pivot(self, st: dict, hits: list[dict]) -> dict:
        groups = self._group_keys(st)
        buckets: dict[tuple, list[dict]] = {}
        for m in hits:
            key = []
            for gtype, field, g in groups:
                if gtype == "time":
                    unit = g["interval"]["timeunit"]
                    n, u = int(unit[:-1]), unit[-1]
                    secs = n * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[u]
                    epoch = int(_parse_ts(m["timestamp"]).timestamp())
                    b = datetime.fromtimestamp(epoch - epoch % secs, UTC)
                    key.append(b.strftime("%Y-%m-%dT%H:%M:%S.000Z"))
                else:
                    val = m.get(field)
                    if val is None:
                        if not self.modern_pivot:
                            key = None
                            break
                        val = "(Empty Value)"  # 5.x+ return a bucket for documents without the field
                    key.append(str(val))
            if key is None:
                continue
            buckets.setdefault(tuple(key), []).append(m)
        items = list(buckets.items())
        values_groups = [g for g in st.get("row_groups", []) if g["type"] == "values"]
        has_time = any(g["type"] == "time" for g in st.get("row_groups", []))
        if values_groups and has_time:
            items.sort(key=lambda kv: kv[0])  # per-bucket terms: keep every bucket
        elif values_groups:
            items.sort(key=lambda kv: -len(kv[1]))
            items = items[: values_groups[0].get("limit", 10)]
        else:
            items.sort(key=lambda kv: kv[0])
        rows = []
        for key, msgs in items:
            rows.append(
                {
                    "key": list(key),
                    "values": [
                        {"key": [s["id"]], "value": self._metric(s, msgs), "rollup": True, "source": "row-leaf"}
                        for s in st["series"]
                    ],
                    "source": "leaf",
                }
            )
        if groups:
            rows.append(
                {
                    "key": [],
                    "values": [
                        {"key": [s["id"]], "value": self._metric(s, hits), "rollup": True, "source": "row-inner"}
                        for s in st["series"]
                    ],
                    "source": "non-leaf",
                }
            )
        else:
            rows = [
                {
                    "key": [],
                    "values": [
                        {"key": [s["id"]], "value": self._metric(s, hits), "rollup": True, "source": "row-leaf"}
                        for s in st["series"]
                    ],
                    "source": "leaf",
                }
            ]
        return {"id": st["id"], "type": "pivot", "rows": rows, "total": len(hits)}

    # ---------------------------------------------------------------- scripting

    def scripting_messages(self, body: dict) -> httpx.Response:
        tr = body["timerange"]
        hits = self.select(body.get("query", "*"), _parse_ts(tr["from"]), _parse_ts(tr["to"]), body.get("streams"))
        hits = self._sort(hits, body.get("sort", "timestamp"), body.get("sort_order", "desc"))
        page = hits[body.get("from", 0) : body.get("from", 0) + body.get("size", 10)]
        fields = body.get("fields") or ["timestamp", "source", "message"]
        return self.json(
            200,
            {
                "schema": [
                    {"column_type": "field", "type": "string", "field": f, "name": f"field: {f}"} for f in fields
                ],
                "datarows": [[m.get(f, "-") for f in fields] for m in page],  # "-" marks a missing field
                "metadata": {"effective_timerange": tr},
            },
        )

    def scripting_aggregate(self, body: dict) -> httpx.Response:
        tr = body["timerange"]
        hits = self.select(body.get("query", "*"), _parse_ts(tr["from"]), _parse_ts(tr["to"]), body.get("streams"))
        groups = body["group_by"]
        buckets: dict[tuple, list[dict]] = {}
        for m in hits:
            key = tuple(m.get(g["field"], "(Empty Value)") for g in groups)
            buckets.setdefault(tuple(str(k) for k in key), []).append(m)
        items = sorted(buckets.items(), key=lambda kv: -len(kv[1]))[: groups[0].get("limit", 10) if groups else None]
        metrics = body["metrics"]
        schema = [
            {"column_type": "grouping", "type": "string", "field": g["field"], "name": f"grouping: {g['field']}"}
            for g in groups
        ] + [
            {
                "column_type": "metric",
                "type": "numeric",
                "function": mt["function"],
                **({"field": mt["field"]} if mt.get("field") else {}),
                "name": f"metric: {mt['function']}({mt.get('field', '')})",
            }
            for mt in metrics
        ]
        rows = []
        for key, msgs in items:
            rows.append(
                list(key) + [self._metric({"type": mt["function"], "field": mt.get("field")}, msgs) for mt in metrics]
            )
        return self.json(200, {"schema": schema, "datarows": rows, "metadata": {"effective_timerange": tr}})
