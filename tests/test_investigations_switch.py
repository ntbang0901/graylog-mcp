"""Investigations are off until turned on: by the admin page's switch (kept in a file) or by the environment."""

from __future__ import annotations

import json

import httpx
import pytest

from graylog_mcp import investigations
from graylog_mcp.admin.app import AdminState, build_app
from graylog_mcp.investigations import model
from graylog_mcp.server import build_server
from tests.test_model_surface import SNAPSHOT


@pytest.fixture(autouse=True)
def _unset(monkeypatch):
    monkeypatch.delenv("GRAYLOG_MCP_INVESTIGATIONS")  # the shared test environment turns them on


def test_off_by_default_then_the_saved_switch_then_the_environment(monkeypatch):
    assert model.switch() == (False, "default") and not investigations.enabled()
    model.set_enabled(True)
    assert model.switch() == (True, "setting")
    model.set_enabled(False)
    assert model.switch() == (False, "setting")
    monkeypatch.setenv("GRAYLOG_MCP_INVESTIGATIONS", "on")
    assert model.switch() == (True, "env")  # the environment decides, whatever was saved
    monkeypatch.setenv("GRAYLOG_MCP_INVESTIGATIONS", "garbage")
    assert model.switch() == (False, "setting")  # a word that means neither is ignored


def test_a_damaged_switch_file_means_off(monkeypatch):
    model.setting_path().write_text("maybe", encoding="utf-8")
    assert model.switch() == (False, "default")


async def test_the_default_gives_the_model_exactly_what_it_saw_before(make_app, monkeypatch):
    monkeypatch.delenv("GRAYLOG_MCP_INVESTIGATIONS", raising=False)
    app, _ = make_app()
    server = build_server(app)
    declared = [t.model_dump(mode="json", exclude_none=True) for t in await server.list_tools()]
    offered = [p.model_dump(mode="json", exclude_none=True) for p in await server.list_prompts()]
    surface = {"instructions": server.instructions, "tools": declared, "prompts": offered}
    text = json.dumps(surface, indent=2, ensure_ascii=False) + "\n"
    assert text == SNAPSHOT.read_text(encoding="utf-8")


@pytest.fixture
async def page(tmp_path):
    state = AdminState(tmp_path, None, "tok", allowed_hosts=("testserver",))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_app(state)),
        base_url="http://testserver",
        headers={"X-Admin-Token": "tok"},
    ) as client:
        yield client


async def test_the_page_says_off_and_turns_them_on_and_off(page):
    got = (await page.get("/api/investigations")).json()
    assert got == {"enabled": False, "source": "default", "investigations": []}
    on = await page.post("/api/investigations/switch", json={"enabled": True})
    assert on.json() == {"enabled": True, "source": "setting", "restart": True}
    assert (await page.get("/api/investigations")).json()["enabled"] is True
    off = await page.post("/api/investigations/switch", json={"enabled": False})
    assert off.json()["enabled"] is False and model.switch() == (False, "setting")


async def test_the_environment_wins_and_the_page_says_so(page, monkeypatch):
    monkeypatch.setenv("GRAYLOG_MCP_INVESTIGATIONS", "off")
    refused = await page.post("/api/investigations/switch", json={"enabled": True})
    assert refused.status_code == 409 and "GRAYLOG_MCP_INVESTIGATIONS" in refused.json()["error"]
    assert (await page.get("/api/investigations")).json()["source"] == "env"
    assert (await page.post("/api/investigations/switch", json={"enabled": "yes"})).status_code in (400, 409)


async def test_the_switch_needs_the_token(page):
    bare = httpx.AsyncClient(transport=page._transport, base_url="http://testserver")
    async with bare:
        assert (await bare.post("/api/investigations/switch", json={"enabled": True})).status_code == 401
