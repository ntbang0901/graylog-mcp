"""Prepare a fresh Graylog for the integration tests.

Creates a GELF HTTP input and a "Payments" stream, then ships the same sample
dataset the contract tests use (including planted sensitive values), and waits
until everything is searchable. This script writes to Graylog on purpose; the
MCP server itself never does.

Usage: GRAYLOG_IT_URL=http://127.0.0.1:9050 [GRAYLOG_IT_GELF_PORT=12250] python tests/integration/seed.py
"""

from __future__ import annotations

import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.fake_graylog import make_dataset

URL = os.environ.get("GRAYLOG_IT_URL", "http://127.0.0.1:9050").rstrip("/")
GELF_PORT = int(os.environ.get("GRAYLOG_IT_GELF_PORT", "1" + "22" + URL.rsplit(":", 1)[1][-2:]))
AUTH = (os.environ.get("GRAYLOG_IT_USER", "admin"), os.environ.get("GRAYLOG_IT_PASSWORD", "admin"))
HEADERS = {"X-Requested-By": "graylog-mcp-it", "Accept": "application/json"}
PAYMENTS = "5f0c0a1b2c3d4e5f60718293"


def api(method: str, path: str, **kw) -> httpx.Response:
    return httpx.request(method, f"{URL}/api/{path}", auth=AUTH, headers=HEADERS, timeout=30, **kw)


def wait_alive(timeout: float = 600) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = api("GET", "system")
            if r.status_code == 200 and r.json().get("lifecycle") in ("running", None):
                return r.json()["version"]
        except httpx.HTTPError:
            pass
        time.sleep(5)
    raise SystemExit(f"Graylog at {URL} did not come up in {timeout}s")


def ensure_input() -> None:
    inputs = api("GET", "system/inputs").json().get("inputs", [])
    if any(i.get("title") == "it-gelf-http" for i in inputs):
        return
    r = api(
        "POST",
        "system/inputs",
        json={
            "title": "it-gelf-http",
            "type": "org.graylog2.inputs.gelf.http.GELFHttpInput",
            "global": True,
            "configuration": {
                "bind_address": "0.0.0.0",
                "port": 12201,
                "recv_buffer_size": 1048576,
                "number_worker_threads": 2,
                "enable_cors": True,
                "max_chunk_size": 65536,
                "idle_writer_timeout": 60,
                "override_source": None,
                "decompress_size_limit": 8388608,
            },
        },
    )
    r.raise_for_status()


def ensure_stream() -> str:
    streams = api("GET", "streams").json().get("streams", [])
    for s in streams:
        if s["title"] == "Payments":
            return s["id"]
    index_set = next(i["id"] for i in api("GET", "system/indices/index_sets").json()["index_sets"] if i["default"])
    stream = {
        "title": "Payments",
        "description": "payment services",
        "index_set_id": index_set,
        "matching_type": "AND",
        "remove_matches_from_default_stream": False,
        "rules": [{"field": "service", "type": 1, "value": "payment", "inverted": False}],
    }
    r = api("POST", "streams", json=stream)
    if r.status_code == 400 and "entity" in r.text:  # 7.x wraps new entities in a CreateEntityRequest
        r = api("POST", "streams", json={"entity": stream})
    r.raise_for_status()
    sid = r.json()["stream_id"]
    api("POST", f"streams/{sid}/resume").raise_for_status()
    return sid


def ship(now: datetime) -> int:
    msgs = make_dataset(now)
    gelf_url = URL.rsplit(":", 1)[0] + f":{GELF_PORT}/gelf"
    for m in msgs:
        ts = datetime.fromisoformat(m["timestamp"].replace("Z", "+00:00")).timestamp()
        doc = {"version": "1.1", "host": m["source"], "short_message": m["message"], "timestamp": ts}
        doc["level"] = m.get("level", 6)
        if m.get("full_message"):
            doc["full_message"] = m["full_message"]
        for key, val in m.items():
            if key in {"_id", "timestamp", "streams", "source", "message", "full_message", "level"}:
                continue
            if key.startswith("gl2_"):
                continue
            doc[f"_{key}"] = val
        for attempt in range(30):
            try:
                httpx.post(gelf_url, json=doc, timeout=10).raise_for_status()
                break
            except httpx.HTTPError:
                if attempt == 29:
                    raise
                time.sleep(2)
    return len(msgs)


def wait_indexed(expected: int, timeout: float = 300) -> None:
    deadline = time.time() + timeout
    seen = 0
    while time.time() < deadline:
        r = api("GET", "search/universal/relative", params={"query": "*", "range": 86400, "limit": 1})
        if r.status_code == 200:
            seen = r.json().get("total_results", 0)
        else:  # universal search removed: ask a views search instead
            body = {
                "queries": [
                    {
                        "id": "q",
                        "query": {"type": "elasticsearch", "query_string": "*"},
                        "timerange": {"type": "relative", "range": 86400},
                        "search_types": [{"id": "m", "type": "messages", "limit": 1}],
                    }
                ]
            }
            r = api("POST", "views/search/sync", json=body)
            if r.status_code == 200:
                seen = r.json()["results"]["q"]["search_types"]["m"].get("total_results", 0)
        if seen >= expected:
            return
        time.sleep(3)
    raise SystemExit(f"only {seen}/{expected} messages searchable after {timeout}s")


def main() -> None:
    version = wait_alive()
    print(f"Graylog {version} at {URL}")
    ensure_input()
    ensure_stream()
    time.sleep(5)
    now = datetime.now(UTC)
    n = ship(now)
    wait_indexed(n)
    Path(os.environ.get("GRAYLOG_IT_SEEDED_AT_FILE", "/tmp/graylog-it-seeded-at")).write_text(now.isoformat())
    print(f"seeded {n} messages at {now.isoformat()}")


if __name__ == "__main__":
    main()
