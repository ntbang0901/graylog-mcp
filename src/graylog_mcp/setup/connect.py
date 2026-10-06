"""Test a Graylog connection before it is saved."""

from __future__ import annotations

import dataclasses
from typing import Any

import httpx

from graylog_mcp.backends import Graylog
from graylog_mcp.client import GraylogError
from graylog_mcp.config import ConfigError, InstanceConfig, parse_config
from graylog_mcp.setup.doctor import _fix_for
from graylog_mcp.timerange import resolve_range

# Tests and the admin UI can route every setup connection through a custom transport.
TRANSPORT: httpx.AsyncBaseTransport | None = None


def build_instance(
    name: str, fields: dict[str, Any], token: str | None = None, password: str | None = None
) -> InstanceConfig:
    """Validate instance fields; a token typed in for a test is used in memory only."""
    clean = {k: v for k, v in fields.items() if v not in (None, "")}
    cfg = parse_config({"instances": {name: clean}}, source="test", require_usable=False).instance(name)
    if token:
        cfg = dataclasses.replace(cfg, token=token, unavailable=None, auth="token")
    if password:
        cfg = dataclasses.replace(cfg, password=password, unavailable=None)
    return cfg


async def test_connection(cfg: InstanceConfig, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    if cfg.unavailable:
        return {"ok": False, "error": cfg.unavailable, "fix": "enter the token/password to test (and save it)"}
    gl = Graylog(cfg, transport or TRANSPORT)
    try:
        await gl.ensure()
        status = gl.status()
        streams = [s for s in await gl.streams() if not s.get("disabled")]
        total = await gl.count("*", resolve_range("24h", None, None, cfg.tz), ())
        return {
            "ok": True,
            "version": status["version"],
            "message_api": status["message_api"],
            "aggregation_api": status["aggregation_api"],
            "streams": len(streams),
            "messages_24h": total,
        }
    except GraylogError as exc:
        return {"ok": False, "error": str(exc), "fix": _fix_for(exc, gl)}
    except ConfigError as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        await gl.aclose()
