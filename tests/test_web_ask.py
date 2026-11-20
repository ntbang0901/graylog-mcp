"""The admin page's ask box, its Providers screen and the Playground's form."""

from __future__ import annotations

import tomllib
from datetime import datetime

import httpx
import pytest

from graylog_mcp import operations
from graylog_mcp.admin import forms
from graylog_mcp.admin.app import AdminState, build_app
from graylog_mcp.setup import tomlwrite
from tests.fake_graylog import FakeGraylog
from tests.fake_jev import FakeJev
from tests.fake_openai import FakeOpenAI
from tests.test_trace import _add

PAYMENT = {"aliases": ["thanh toán"], "query": "service:payment", "description": "payment service"}


def server(name: str) -> dict:
    return {"url": f"https://{name}.test", "token_env": "TEST_GRAYLOG_TOKEN"}


@pytest.fixture(autouse=True)
def no_key(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)


@pytest.fixture
def page(tmp_path):
    """An admin page on a config file; ``providers``: the transport every provider is reached through."""
    clients = []

    def factory(config: dict, providers: httpx.MockTransport | None = None, name: str = "config.toml"):
        fake = FakeGraylog(dataset="basic")
        path = tmp_path / name
        path.write_text(tomlwrite.dumps(config), encoding="utf-8")
        state = AdminState(tmp_path, path, "tok", transport=fake.transport, allowed_hosts=("testserver",))
        state.provider_transport = providers
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(state)), base_url="http://testserver",
                                   headers={"X-Admin-Token": "tok"})  # fmt: skip
        clients.append(client)
        return client, fake, path

    yield factory


BASE = {
    "timezone": "Asia/Ho_Chi_Minh",
    "default_instance": "prod",
    "instances": {"prod": server("prod"), "dev": server("dev")},
    "glossary": {"terms": {"payment": PAYMENT}},
}


async def test_the_acceptance_question_on_the_web(page, monkeypatch):
    """A key is saved, every provider is turned off: the page reads the question as ask does, by rule alone."""
    monkeypatch.setenv("TYPESAFE_API_KEY", "fake-typesafe-key")
    jev = FakeJev()
    client, fake, _ = page(BASE | {"ask": {"provider_order": []}}, jev.transport)
    _add(fake, 300, service="payment", http_status=500, level=3, message="bank call failed")
    _add(fake, 200, service="payment", http_status=422, message="amount refused")

    out = (await client.post("/api/ask", json={"question": "thanh toán lỗi trong 15 phút qua ở prod"})).json()

    view = out["view"]
    assert view["instance"] == "prod" and view["terms"] == ["payment"] and view["errors"] == "both"
    assert view["query"].endswith("AND (service:payment)") and view["query"] == out["result"]["ran"]
    first, last = (datetime.fromisoformat(view[k]) for k in ("from", "to"))
    assert (last - first).total_seconds() == 900 and view["range"] == ""
    assert '"thanh toán" is understood as payment service' in out["result"]["summary"]
    assert out["result"]["by_kind"]["business"] == 1 and out["result"]["by_kind"]["technical"] > 1
    assert out["result"]["cost"]["provider_requests"] == 0
    assert out["sent"] == [] and jev.bodies == []


async def test_editing_the_reading_rebuilds_the_query(page):
    client, _, _ = page(BASE)
    technical = (await client.post("/api/ask/filters", json={"instance": "prod", "errors": "technical"})).json()
    assert "service" not in technical["query"] and technical["notes"] == []
    both = await client.post("/api/ask/filters", json={"instance": "prod", "errors": "any", "terms": ["payment"]})
    assert both.json()["query"] == "service:payment"
    assert (await client.post("/api/ask/filters", json={"instance": "prod", "terms": ["nope"]})).status_code == 400
    assert (await client.post("/api/ask/filters", json={"instance": "prod", "errors": "x"})).status_code == 400
    assert (await client.post("/api/ask/filters", json={"instance": "nowhere"})).status_code == 400


async def test_what_was_handed_to_a_provider_is_shown_redacted(page, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "fake-typesafe-key")
    jev = FakeJev()
    client, _, _ = page(BASE, jev.transport)  # no provider_order: the saved key makes Jev the provider
    out = (await client.post("/api/ask", json={"question": "anything odd for someone@example.com"})).json()
    assert {s["provider"] for s in out["sent"]} == {"jev"} and out["sent"][0]["task"] == "understand"
    assert len(out["sent"]) == len(jev.bodies)
    shown = str(out["sent"])
    assert "someone@example.com" not in shown and "[EMAIL]" in shown
    assert out["result"]["cost"]["provider_requests"] == len(jev.bodies) > 0


async def test_a_provider_without_its_key_is_not_listed_as_sent(page):
    jev = FakeJev()
    declared = {"providers": {"jev": {"kind": "jev"}}, "provider_order": ["jev"]}
    client, _, _ = page({**BASE, "ask": declared}, jev.transport)
    out = (await client.post("/api/ask", json={"question": "anything odd"})).json()
    assert jev.bodies == [] and out.get("sent") == [], out


async def test_a_question_that_names_no_environment_opens_no_view(page):
    two = {"groups": {g: {"environments": {"prod": server(g)}} for g in ("a", "b")}, "glossary": BASE["glossary"]}
    client, _, _ = page(two)
    out = (await client.post("/api/ask", json={"question": "lỗi trên prod hôm nay"})).json()
    assert out["view"] is None and out["result"]["options"] == ["a/prod", "b/prod"]
    assert (await client.post("/api/ask", json={"question": "  "})).status_code == 400


async def test_the_journey_of_a_pasted_code(page):
    client, fake, _ = page(BASE)
    _add(fake, 300, trace_id="req-aaa111", service="gateway", level=6, message="POST /orders ORD-1001 received")
    _add(fake, 299, trace_id="req-aaa111", service="payment", level=3, message="charge refused")
    out = (await client.post("/api/ask/journey", json={"code": "req-aaa111"})).json()
    services = {m.get("service") for m in out["result"]["timeline"]}
    assert {"gateway", "payment"} <= services
    assert (await client.post("/api/ask/journey", json={"code": ""})).status_code == 400


async def test_the_vocabulary_of_the_page(page):
    client, _, _ = page(BASE | {"presets": {"pay": {"tool": "search_logs", "args": {"query": "service:payment"}}}})
    out = (await client.get("/api/ask/vocabulary")).json()
    assert out["terms"][0]["name"] == "payment" and out["terms"][0]["aliases"] == ["thanh toán"]
    assert out["presets"] == [
        {"name": "pay", "tool": "search_logs", "description": "", "args": {"query": "service:payment"}}
    ]
    assert out["errors"] == ["any", "technical", "business", "both"]


# ------------------------------------------------------------------ providers


GATEWAY = {"kind": "openai", "base_url": "http://127.0.0.1:4000/v1", "model": "acme-default",
           "api_key_env": "ACME_GATEWAY_KEY", "can": ["understand"]}  # fmt: skip


async def test_a_provider_is_added_given_a_key_tested_and_removed(page):
    gateway = FakeOpenAI()
    client, _, path = page(BASE, gateway.transport)
    added = await client.post(
        "/api/providers", json={"name": "gw", "fields": GATEWAY | {"api_key": "fake-gateway-key"}}
    )
    assert added.json() == {"ok": True, "order": ["gw"]}
    written = tomllib.loads(path.read_text())
    assert written["ask"]["providers"]["gw"] == GATEWAY  # the key never goes to the config file
    assert "fake-gateway-key" not in path.read_text()

    untested = (await client.post("/api/providers/test", json={"name": "gw"})).json()
    assert untested == {"ok": False, "error": "no key saved in ACME_GATEWAY_KEY yet"}
    assert (await client.post("/api/secrets", json={"name": "ACME_GATEWAY_KEY", "value": "fake-gateway-key"})).json()[
        "ok"
    ]
    listing = (await client.get("/api/providers")).json()
    assert listing["active"] == ["gw"] and listing["declared"][0]["key_source"] == "saved"

    tried = (await client.post("/api/providers/test", json={"name": "gw"})).json()
    assert tried["ok"] and tried["answer"]["intent"]["choice"]
    assert "production" in gateway.bodies[-1]["messages"][-1]["content"]  # the fixed sample, nothing of the user's

    assert (await client.post("/api/providers/order", json={"order": []})).json()["ok"]
    assert (await client.get("/api/providers")).json()["active"] == []
    assert (await client.post("/api/providers/remove", json={"name": "gw"})).json()["ok"]
    assert "gw" not in tomllib.loads(path.read_text())["ask"].get("providers", {})


async def test_adding_a_provider_keeps_the_key_of_login_ask_in_use(page, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "fake-typesafe-key")
    client, _, path = page(BASE)
    assert (await client.post("/api/providers", json={"name": "gw", "fields": GATEWAY})).json()["order"] == [
        "jev",
        "gw",
    ]
    assert tomllib.loads(path.read_text())["ask"]["providers"]["jev"] == {"kind": "jev"}


async def test_a_bad_provider_is_refused_and_nothing_written(page):
    client, _, path = page(BASE)
    before = path.read_text()
    bad = await client.post(
        "/api/providers", json={"name": "gw", "fields": {"kind": "openai", "base_url": "http://llm.example.com"}}
    )
    assert bad.status_code == 400 and "https" in bad.json()["error"]
    assert (await client.post("/api/providers", json={"name": "rules", "fields": GATEWAY})).status_code == 400
    assert path.read_text() == before


async def test_a_repository_file_may_not_choose_providers(page):
    client, _, path = page(BASE, name=".graylog-mcp.toml")
    before = path.read_text()
    refused = await client.post("/api/providers", json={"name": "gw", "fields": GATEWAY})
    assert refused.status_code == 400 and "repository" in refused.json()["error"]
    assert (await client.get("/api/providers")).json()["editable"] is False
    assert path.read_text() == before


# ------------------------------------------------------------------ the Playground's form


def test_the_form_follows_each_operation():
    trace = {f["name"]: f for f in forms.arguments(operations.OPERATIONS["trace_request"])}
    assert trace["trace_id"]["required"] and trace["trace_id"]["control"] == "string"
    assert "business code" in trace["trace_id"]["description"]
    assert trace["streams"]["control"] == "list" and trace["limit"]["control"] == "integer"
    assert trace["range"]["default"] == "24h"
    controls = {f["control"] for op in operations.playground().values() for f in forms.arguments(op)}
    assert "choice" in controls  # a Literal argument offers its values


async def test_the_page_gets_the_form_of_every_playground_tool(page):
    client, _, _ = page(BASE)
    state = (await client.get("/api/state")).json()
    assert set(state["tool_fields"]) == set(state["tools"])
