"""Retries and the per-instance concurrency limit of the Graylog client."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from graylog_mcp import client as client_mod
from graylog_mcp.client import GraylogClient, GraylogError, QueryError, _retry_after
from graylog_mcp.config import parse_config


def instance(**extra):
    data = {"url": "https://graylog.test", "token_env": "TEST_GRAYLOG_TOKEN", **extra}
    return parse_config({"instances": {"main": data}}, source="test").instances["main"]


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    monkeypatch.setattr(client_mod, "RETRY_BASE_SECONDS", 0.0)
    monkeypatch.setattr(client_mod, "RETRY_MAX_SECONDS", 0.0)


def answers(*responses):
    """A transport answering with each response (an exception is raised) in turn; the last one repeats."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        item = responses[min(len(calls), len(responses)) - 1]
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.MockTransport(handler), calls


async def test_retries_transient_statuses_then_succeeds():
    transport, calls = answers(httpx.Response(503), httpx.Response(429), httpx.Response(200, json={"ok": 1}))
    gc = GraylogClient(instance(), transport)
    assert await gc.get("system") == {"ok": 1}
    assert len(calls) == 3


async def test_gives_up_after_the_configured_retries():
    transport, calls = answers(httpx.Response(502, json={"message": "bad gateway"}))
    gc = GraylogClient(instance(retries=1), transport)
    with pytest.raises(GraylogError, match="502"):
        await gc.get("system")
    assert len(calls) == 2


async def test_retries_dropped_connections():
    transport, calls = answers(httpx.ConnectError("refused"), httpx.Response(200, json={}))
    gc = GraylogClient(instance(), transport)
    assert await gc.get("streams") == {}
    assert len(calls) == 2


async def test_does_not_retry_errors_that_will_not_change():
    transport, calls = answers(httpx.Response(400, json={"message": "bad query"}))
    with pytest.raises(QueryError):
        await GraylogClient(instance(), transport).post("views/search/sync", {})
    assert len(calls) == 1
    transport, calls = answers(httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]"))
    with pytest.raises(GraylogError, match="TLS verification"):
        await GraylogClient(instance(), transport).get("system")
    assert len(calls) == 1


async def test_retries_zero_disables_retrying():
    transport, calls = answers(httpx.Response(503))
    with pytest.raises(GraylogError):
        await GraylogClient(instance(retries=0), transport).get("system")
    assert len(calls) == 1


def test_retry_after_header(monkeypatch):
    monkeypatch.setattr(client_mod, "RETRY_MAX_SECONDS", 10.0)
    assert _retry_after(httpx.Response(429, headers={"Retry-After": "3"})) == 3.0
    assert _retry_after(httpx.Response(429, headers={"Retry-After": "600"})) == 10.0
    assert _retry_after(httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})) == 0.0
    assert _retry_after(httpx.Response(429)) is None


async def test_concurrency_is_limited_per_instance():
    running = peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return httpx.Response(200, json={})

    gc = GraylogClient(instance(max_concurrency=3), httpx.MockTransport(handler))
    await asyncio.gather(*(gc.get("streams") for _ in range(12)))
    assert peak == 3


@pytest.mark.parametrize(
    ("extra", "message"),
    [({"max_concurrency": 0}, "max_concurrency"), ({"retries": -1}, "retries"), ({"retries": True}, "retries")],
)
def test_invalid_settings(extra, message):
    from graylog_mcp.config import ConfigError

    with pytest.raises(ConfigError, match=message):
        instance(**extra)


def test_admin_page_writes_the_settings_as_numbers():
    from graylog_mcp.setup.configfile import write_scope

    data = write_scope({}, "group:payment", {"max_concurrency": "8", "retries": "0"})
    assert data["groups"]["payment"] == {"max_concurrency": 8, "retries": 0}
    assert write_scope(data, "group:payment", {"retries": ""})["groups"]["payment"] == {"max_concurrency": 8}
