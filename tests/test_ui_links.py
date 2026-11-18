"""Links from tool results to the admin page's log explorer, while one runs on this machine."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from graylog_mcp import rca, tools, ui_links
from graylog_mcp.config import ConfigError
from graylog_mcp.timerange import TimeRange

TR = TimeRange(datetime(2026, 1, 1, 10, tzinfo=UTC), datetime(2026, 1, 1, 11, tzinfo=UTC), "test")


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    monkeypatch.setattr(ui_links, "CACHE_SECONDS", 0.0)


def _params(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def _write(**data) -> None:
    ui_links.path().parent.mkdir(parents=True, exist_ok=True)
    ui_links.path().write_text(json.dumps(data), encoding="utf-8")


def _ended_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


async def test_without_a_running_admin_results_have_no_link(make_app):
    app, _ = make_app()
    assert "ui" not in await tools.search_logs(app, query="*", range="2h")
    _write(host="127.0.0.1", port=8765, base="/", pid=_ended_pid(), started=0)
    assert ui_links.current() is None
    assert "ui" not in await tools.search_logs(app, query="*", range="2h")
    _write(host="127.0.0.1", port="8765", base="/", pid=os.getpid())  # not ours to trust
    assert ui_links.current() is None


async def test_search_logs_links_its_query_and_absolute_range(make_app):
    app, _ = make_app()
    ui_links.record("127.0.0.1", 8765, "/")
    res = await tools.search_logs(app, query="level:<=3", range="2h", streams=["Payments"])
    url = urlsplit(res["ui"])
    assert (url.scheme, url.netloc, url.path) == ("http", "127.0.0.1:8765", "/logs/all")
    params = _params(res["ui"])
    assert params["instance"] == "main" and "level:<=3" in params["q"] and params["streams"] == "Payments"
    start = datetime.fromisoformat(params["from"].replace("Z", "+00:00"))
    assert datetime.fromisoformat(params["to"].replace("Z", "+00:00")) - start == timedelta(hours=2)
    assert "token" not in res["ui"]


async def test_trace_request_links_the_shared_admin_with_its_trace(make_app):
    app, _ = make_app()
    ui_links.record("127.0.0.1", 18742, "/admin/")
    res = await tools.trace_request(app, "req-7f3a9c", range="1h")
    assert res["ui"].startswith("http://127.0.0.1:18742/admin/logs/traces?")
    assert _params(res["ui"])["trace"] == "req-7f3a9c" and "q" not in _params(res["ui"])


async def test_root_cause_links_the_dashboard(make_app):
    app, _ = make_app("6.1.2", dataset="incident")
    ui_links.record("localhost", 8765, "/")
    res = await rca.root_cause(app, range="1h", query="source:acme-api")
    assert res["ui"].startswith("http://localhost:8765/logs/dashboard?")
    assert _params(res["ui"])["q"] == "source:acme-api"


def test_redacted_values_stay_out_of_the_link(make_app):
    app, _ = make_app()
    ui_links.record("127.0.0.1", 8765, "/")
    url = ui_links.link(app, "main", "traces", TR, query="password=fake-secret", trace="4111 1111 1111 1111")
    assert url and "fake-secret" not in url and "4111" not in url and "trace" not in _params(url)
    assert ui_links.link(app, "main", "all", TR, query="*").endswith(  # type: ignore[union-attr]
        "?instance=main&from=2026-01-01T10%3A00%3A00.000Z&to=2026-01-01T11%3A00%3A00.000Z"
    )
    with pytest.raises(ValueError, match="tab"):
        ui_links.link(app, "main", "nope", TR)


async def test_links_can_be_turned_off(make_app):
    app, _ = make_app(ui_links="off")
    ui_links.record("127.0.0.1", 8765, "/")
    assert "ui" not in await tools.search_logs(app, query="*", range="2h")
    with pytest.raises(ConfigError, match="ui_links"):
        make_app(ui_links="always")


def test_only_the_writer_removes_the_file_and_reads_are_cached(monkeypatch):
    other = _ended_pid()
    _write(host="127.0.0.1", port=8765, base="/", pid=other)
    ui_links.forget()
    assert json.loads(ui_links.path().read_text(encoding="utf-8"))["pid"] == other
    ui_links.record("127.0.0.1", 8765, "/")
    monkeypatch.setattr(ui_links, "CACHE_SECONDS", 30.0)
    assert ui_links.current() is not None
    ui_links.path().unlink()
    assert ui_links.current() is not None  # the cached read
    ui_links.forget()
    assert ui_links.current() is None


def test_ui_keeps_its_port(tmp_path, capsys):
    from graylog_mcp.admin.app import serve

    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        with pytest.raises(ConfigError, match="--port"):
            serve(tmp_path, None, "127.0.0.1", port, open_browser=False)
        ui_links.record("127.0.0.1", port, "/")  # the program on it is our admin page
        assert serve(tmp_path, None, "127.0.0.1", port, open_browser=False) == 0
    assert f"already runs on http://127.0.0.1:{port}/" in capsys.readouterr().out
