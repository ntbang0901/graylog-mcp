import httpx
import pytest

from graylog_mcp.__main__ import BearerAuth, _is_loopback, main, run_http
from graylog_mcp.config import ConfigError
from tests.conftest import make_config


async def ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def test_bearer_auth():
    app = BearerAuth(ok_app, "s3cret")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/mcp")).status_code == 401
        assert (await c.get("/mcp", headers={"Authorization": "Bearer nope"})).status_code == 401
        assert (await c.get("/mcp", headers={"Authorization": "Bearer s3cret"})).status_code == 200
        assert (await c.get("/healthz")).status_code == 200


def test_loopback():
    assert _is_loopback("127.0.0.1") and _is_loopback("::1") and _is_loopback("localhost")
    assert not _is_loopback("0.0.0.0")


def test_refuses_public_bind_without_token():
    with pytest.raises(ConfigError, match="without authentication"):
        run_http(make_config(), host="0.0.0.0", port=8000, path="/mcp", allow_no_auth=False)


def test_cli_config_error(capsys):
    assert main(["--check"]) == 2
    assert "configuration error" in capsys.readouterr().err
