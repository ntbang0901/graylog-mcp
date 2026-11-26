"""The channel for ChatGPT web: OAuth with a pairing code, on its own port, over the remote_access environments."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import stat
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from graylog_mcp.__main__ import remote_channel_app
from graylog_mcp.config import CHATGPT_REDIRECT, ConfigError, load_config
from graylog_mcp.oauth_store import OAuthStore
from graylog_mcp.remote_channel import SCOPE, PairingProvider, auth_settings, build_remote_app, remote_config
from graylog_mcp.server import build_server
from graylog_mcp.tools import App
from tests.conftest import make_config
from tests.fake_graylog import FakeGraylog
from tests.test_shared import _free_port

PUBLIC = "https://logs.example.com"
DEV = {"url": "https://dev.test", "token_env": "TEST_GRAYLOG_TOKEN", "remote_access": True}
PROD = {"url": "https://prod.test", "token_env": "TEST_GRAYLOG_TOKEN"}


def remote(**oauth):
    return make_config(
        instances={"main": PROD, "dev": DEV},
        http={"oauth": {"enabled": True, "public_url": PUBLIC, **oauth}},
    )


def test_the_channel_is_off_and_no_environment_is_offered_by_default():
    cfg = make_config()
    assert cfg.http.oauth.enabled is False and cfg.instances["main"].remote_access is False
    with pytest.raises(ConfigError, match="no environment has remote_access"):
        remote_config(make_config(http={"oauth": {"enabled": True, "public_url": PUBLIC}}))


@pytest.mark.parametrize(
    ("oauth", "error"),
    [
        ({"enabled": True}, "public_url: required"),
        ({"enabled": True, "public_url": "http://logs.example.com"}, "https address alone"),
        ({"enabled": True, "public_url": "https://logs.example.com/mcp"}, "https address alone"),
        ({"allowed_redirects": ["http://chat.example.com/cb"]}, "not an https address"),
        ({"access_minutes": 5000}, "at most 1440"),
        ({"tunnel": "ngrok"}, "unknown key"),
    ],
)
def test_a_channel_set_wrongly_is_refused(oauth, error):
    with pytest.raises(ConfigError, match=error):
        make_config(http={"oauth": oauth})


def test_remote_access_is_decided_per_environment_never_for_a_whole_group():
    group = {"remote_access": True, "environments": {"dev": {"url": "https://dev.test"}}}
    with pytest.raises(ConfigError, match="unknown key"):
        make_config(instances={}, groups={"payment": group})
    per_env = {"environments": {"dev": {"url": "https://dev.test", "remote_access": True}}}
    cfg = make_config(groups={"payment": per_env})
    assert cfg.instances["payment/dev"].remote_access is True


@pytest.mark.parametrize(
    "text",
    [
        '[instances.dev]\nurl = "https://dev.test"\nremote_access = true\n',
        '[instances.dev]\nurl = "https://dev.test"\n[http.oauth]\nenabled = false\n',
    ],
)
def test_a_repository_file_may_not_open_the_channel(tmp_path, text):
    project = tmp_path / ".graylog-mcp.toml"
    project.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="not allowed in a repository's own config file"):
        load_config(project, require_usable=False)


def test_a_remote_client_sees_only_the_remote_environments():
    cfg = remote_config(remote())
    assert list(cfg.instances) == ["dev"] and cfg.default_instance == "dev" and cfg.ui_links is False
    with pytest.raises(ConfigError) as refused:
        cfg.instance("main")
    assert "configured instances: dev." in str(refused.value)


async def test_a_remote_client_is_given_no_investigations(tmp_path):
    # the switch is on (conftest); a record of an environment the client may not see is on this machine
    local = build_server(App.create(remote(), FakeGraylog().transport))
    started = {"action": "start", "title": "prod only", "question": "why?", "instance": "main"}
    await local.call_tool("investigation", started)
    app = App.create(remote_config(remote()), FakeGraylog().transport)
    server = build_server(app, auth=_auth(tmp_path))
    listed = {tool.name: tool for tool in await server.list_tools()}
    assert "investigation" not in listed and "get_investigation" not in listed
    assert "investigation_id" not in listed["search_logs"].input_schema["properties"]
    assert "Investigations" not in (server.instructions or "")


def _auth(tmp_path):
    cfg = remote()
    resource = cfg.http.oauth.public_url + cfg.http.path
    provider = PairingProvider(cfg.http.oauth, OAuthStore(tmp_path / "auth.json"), resource)
    return provider, auth_settings(cfg.http.oauth, cfg.http.path)


class Client:
    client_id, client_name = "c1", "ChatGPT"


def pending(provider: PairingProvider) -> str:
    from mcp.server.auth.provider import AuthorizationParams

    params = AuthorizationParams(
        state="s", scopes=[SCOPE], code_challenge="x" * 43, redirect_uri=CHATGPT_REDIRECT,
        redirect_uri_provided_explicitly=True,
    )  # fmt: skip
    url = asyncio.run(provider.authorize(Client(), params))  # type: ignore[arg-type]
    return parse_qs(urlsplit(url).query)["request"][0]


def test_five_wrong_codes_cancel_the_request_and_a_code_works_once(tmp_path):
    store = OAuthStore(tmp_path / "oauth.json")
    provider = PairingProvider(remote().http.oauth, store, PUBLIC + "/mcp")
    request = pending(provider)
    for _ in range(4):
        assert provider.pair(request, "AAAA-AAAA") == ("wrong", None)
    assert provider.pair(request, "AAAA-AAAA") == ("gone", None)
    code, _ = store.new_code()
    assert provider.pair(request, code) == ("gone", None)  # the request is gone, not the code
    status, location = provider.pair(pending(provider), code.lower()[:4] + " - " + code[4:])
    assert status == "ok" and location is not None and location.startswith(CHATGPT_REDIRECT + "?code=")
    assert provider.pair(pending(provider), code) == ("wrong", None)


def test_guessing_across_many_requests_voids_the_live_codes(tmp_path):
    store = OAuthStore(tmp_path / "oauth.json")
    provider = PairingProvider(remote().http.oauth, store, PUBLIC + "/mcp")
    code, _ = store.new_code()
    for _ in range(4):  # four requests, five wrong tries each: each request alone stays under its limit
        request = pending(provider)
        for _ in range(5):
            provider.pair(request, "AAAA-AAAA")
    assert provider.pair(pending(provider), code) == ("wrong", None)


def test_registration_is_refused_when_too_large_or_to_an_address_not_allowed(tmp_path):
    from mcp.server.auth.provider import RegistrationError
    from mcp.shared.auth import OAuthClientInformationFull

    store = OAuthStore(tmp_path / "oauth.json")
    provider = PairingProvider(remote(allowed_redirects=["https://chat.example.com"]).http.oauth, store, PUBLIC)

    def register(redirect: str = "https://chat.example.com/", **fields) -> str | None:
        """The error a registration gets, None when accepted (RegistrationError is frozen: no pytest.raises)."""
        info = OAuthClientInformationFull(client_id="c1", redirect_uris=[redirect], **fields)
        try:
            asyncio.run(provider.register_client(info))
        except RegistrationError as exc:
            return exc.error
        return None

    assert register(client_name="x" * 5000) == "invalid_client_metadata"
    assert register("https://chat.example.com/other") == "invalid_redirect_uri"
    assert store.client("c1") is None
    assert register(client_name="Chat\nGPT") is None  # the configured address had no "/", this one has
    assert store.client("c1") is not None


def test_a_client_name_is_shown_and_logged_on_one_line():
    from graylog_mcp.oauth_store import label

    assert label("Chat\nGPT\x1b[31m") == "Chat GPT [31m" and label("") == "a client" and len(label("x" * 500)) == 80


def test_revoking_a_refresh_token_revokes_its_access_token(tmp_path):
    store = OAuthStore(tmp_path / "oauth.json")
    store.add_client({"client_id": "c1"})
    access, refresh = store.issue("c1", [SCOPE], None, 3600, 86400)
    other, _ = store.issue("c1", [SCOPE], None, 3600, 86400)
    store.revoke(refresh)
    assert store.token(access, "access") is None and store.token(other, "access") is not None


def test_a_damaged_store_is_kept_aside_and_started_again(tmp_path, caplog):
    path = tmp_path / "oauth.json"
    path.write_text("{not json", encoding="utf-8")
    store = OAuthStore(path)
    assert store.clients() == [] and not path.exists()
    assert [p.read_text() for p in tmp_path.glob("oauth.json.bad-*")] == ["{not json"]
    assert "could not be read" in caplog.text
    store.new_code()
    assert path.exists()


def test_a_remote_client_is_not_told_the_names_of_other_environments():
    cfg = remote_config(
        make_config(
            instances={"main": PROD, "dev": DEV},
            presets={"prod_errors": {"tool": "search_logs", "args": {"instance": "main", "query": "level:3"}},
                     "dev_errors": {"tool": "search_logs", "args": {"instance": "dev", "query": "level:3"}}},
            glossary={"terms": {"pay": {"aliases": ["thanh toán"], "query": "service:payment",
                                        "instances": ["main", "dev"], "preset": "prod_errors"},
                                "refund": {"aliases": ["hoàn tiền"], "query": "service:refund", "instances": ["main"]}},
                      "codes": {"order": {"pattern": "ORD-[0-9]+", "instances": ["main"]}}},
        )
    )  # fmt: skip
    assert list(cfg.presets) == ["dev_errors"]
    assert list(cfg.glossary.terms) == ["pay"] and cfg.glossary.terms["pay"].instances == ("dev",)
    assert cfg.glossary.terms["pay"].preset is None and cfg.glossary.codes == {}


async def test_a_busy_remote_port_leaves_the_local_server_running():
    from graylog_mcp.__main__ import _serve_together

    class Local:
        should_exit = False
        stopped = False

        async def serve(self):
            while not self.should_exit:
                await asyncio.sleep(0.01)
            self.stopped = True

    class Busy:
        should_exit = False

        async def serve(self):
            raise SystemExit(3)

    local = Local()
    running = asyncio.create_task(_serve_together(local, Busy()))
    await asyncio.sleep(0.1)
    assert not running.done() and not local.stopped
    local.should_exit = True
    await asyncio.wait_for(running, 2)
    assert local.stopped


def test_a_pairing_code_expires(tmp_path):
    now = [1000.0]
    store = OAuthStore(tmp_path / "oauth.json", clock=lambda: now[0])
    code, expires = store.new_code()
    assert expires == 1600.0
    now[0] = 1601.0
    assert store.use_code(code) is False


@pytest.fixture
def channel(tmp_path):
    store = OAuthStore(tmp_path / "oauth.json")
    app = build_remote_app(remote(), transport=FakeGraylog().transport, store=store)
    import uvicorn

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", store
    server.should_exit = True
    thread.join(timeout=10)


async def _describe(base: str, token: str) -> dict:
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    async with (
        create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http,
        streamable_http_client(base + "/mcp", http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        described = await session.call_tool("describe", {})
        refused = await session.call_tool("search_logs", {"instance": "main", "query": "*"})
        return {"describe": json.loads(described.content[0].text), "refused": refused}  # type: ignore[union-attr]


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def test_chatgpt_pairs_with_a_code_and_reads_only_the_remote_environment(channel, tmp_path):
    base, store = channel
    c = httpx.Client(base_url=base)
    meta = c.get("/.well-known/oauth-authorization-server").json()
    assert "S256" in meta["code_challenge_methods_supported"] and meta["issuer"].rstrip("/") == PUBLIC
    resource = c.get("/.well-known/oauth-protected-resource/mcp").json()
    assert resource["resource"] == PUBLIC + "/mcp" and resource["scopes_supported"] == [SCOPE]
    unauthorized = c.post("/mcp", json={})
    assert unauthorized.status_code == 401 and "resource_metadata" in unauthorized.headers["www-authenticate"]

    assert c.post("/register", json={"redirect_uris": ["https://evil.example.com/cb"]}).status_code == 400
    reg = c.post("/register", json={"redirect_uris": [CHATGPT_REDIRECT], "client_name": "ChatGPT <b>"})
    assert reg.status_code == 201
    client = reg.json()

    verifier, challenge = _pkce()
    query = {
        "response_type": "code", "client_id": client["client_id"], "redirect_uri": CHATGPT_REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "st-1", "scope": SCOPE,
    }  # fmt: skip
    authorize = c.get("/authorize", params=query)
    assert authorize.status_code == 302 and authorize.headers["location"].startswith(PUBLIC + "/oauth/pair?")
    request = parse_qs(urlsplit(authorize.headers["location"]).query)["request"][0]

    page = c.get("/oauth/pair", params={"request": request})
    assert page.status_code == 200 and "ChatGPT &lt;b&gt;" in page.text and "<b>ChatGPT <b>" not in page.text
    policy = page.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in policy and "form-action 'self' https://chatgpt.com" in policy
    wrong = c.post("/oauth/pair", data={"request": request, "code": "AAAA-AAAA"})
    assert wrong.status_code == 403 and "4 tries left" in wrong.text
    code, _ = store.new_code()
    paired = c.post("/oauth/pair", data={"request": request, "code": code})
    assert paired.status_code == 302
    sent_back = parse_qs(urlsplit(paired.headers["location"]).query)
    assert paired.headers["location"].startswith(CHATGPT_REDIRECT) and sent_back["state"] == ["st-1"]

    exchange = {
        "grant_type": "authorization_code", "code": sent_back["code"][0], "redirect_uri": CHATGPT_REDIRECT,
        "client_id": client["client_id"], "client_secret": client.get("client_secret"), "code_verifier": verifier,
    }  # fmt: skip
    tokens = c.post("/token", data=exchange)
    assert tokens.status_code == 200, tokens.text
    assert c.post("/token", data=exchange).status_code == 400  # a code is used once
    access, refresh = tokens.json()["access_token"], tokens.json()["refresh_token"]

    seen = asyncio.run(_describe(base, access))
    assert [i["name"] for i in seen["describe"]["instances"]] == ["dev"]
    assert seen["refused"].is_error and "prod.test" not in str(seen["refused"].content)

    renew = {"grant_type": "refresh_token", "refresh_token": refresh, "client_id": client["client_id"],
             "client_secret": client.get("client_secret")}  # fmt: skip
    assert c.post("/token", data=renew).status_code == 200
    assert c.post("/token", data=renew).status_code == 400  # the old refresh token died at its first use

    saved = (tmp_path / "oauth.json").read_text(encoding="utf-8")
    assert access not in saved and refresh not in saved and code not in saved
    if sys.platform != "win32":
        assert stat.S_IMODE((tmp_path / "oauth.json").stat().st_mode) == 0o600
    assert [p["name"] for p in store.clients()] == ["ChatGPT <b>"]

    for path in ("/admin/", "/status", "/healthz"):
        assert c.get(path).status_code == 404
    store.remove_client(client["client_id"])
    assert c.post("/mcp", json={}, headers={"Authorization": f"Bearer {access}"}).status_code == 401


def test_a_channel_set_wrongly_leaves_the_local_server_running(caplog):
    assert remote_channel_app(remote(port=8000), 8000) is None
    assert "needs a port of its own" in caplog.text
    no_environment = make_config(http={"oauth": {"enabled": True, "public_url": PUBLIC}})
    assert remote_channel_app(no_environment, 8000) is None
    assert "no environment has remote_access" in caplog.text
    assert remote_channel_app(make_config(), 8000) is None
    assert remote_channel_app(remote(), 8000) is not None


async def test_the_admin_page_shows_the_channel_and_hands_out_codes(tmp_path):
    from graylog_mcp.admin.app import AdminState, build_app
    from graylog_mcp.setup import tomlwrite

    path = tmp_path / "config.toml"
    data = {"instances": {"main": PROD, "dev": DEV}, "http": {"oauth": {"enabled": True, "public_url": PUBLIC}}}
    path.write_text(tomlwrite.dumps(data), encoding="utf-8")
    state = AdminState(tmp_path, path, "tok", transport=FakeGraylog().transport, allowed_hosts=("testserver",))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_app(state)), base_url="http://testserver",
        headers={"X-Admin-Token": "tok"},
    ) as client:  # fmt: skip
        shown = (await client.get("/api/remote")).json()
        assert shown["enabled"] and shown["instances"] == ["dev"] and shown["mcp_url"] == PUBLIC + "/mcp"
        code = (await client.post("/api/remote/code", json={})).json()["code"]
        assert len(code) == 9 and code[4] == "-"
        assert OAuthStore().use_code(code) is True
        assert (await client.post("/api/remote/revoke", json={"client_id": "nobody"})).status_code == 404
        assert (await client.post("/api/remote/revoke", json={"all": True})).json() == {"removed": 0}


async def test_the_admin_page_turns_the_channel_on_and_off_and_restarts_its_server(tmp_path, monkeypatch):
    from graylog_mcp.admin.app import AdminState, build_app
    from graylog_mcp.setup import service, tomlwrite

    path = tmp_path / "config.toml"
    group = {"environments": {"staging": {"url": "https://staging.test", "token_env": "TEST_GRAYLOG_TOKEN"}}}
    path.write_text(tomlwrite.dumps({"instances": {"main": PROD, "dev": PROD}, "groups": {"pay": group}}), "utf-8")
    spawned: list[list[str]] = []
    monkeypatch.setattr(service, "spawn_detached", lambda cmd: spawned.append(cmd) or 1)
    state = AdminState(tmp_path, path, "tok", transport=FakeGraylog().transport, allowed_hosts=("testserver",))
    state.server_port = 8000
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_app(state)), base_url="http://testserver",
        headers={"X-Admin-Token": "tok"},
    ) as client:  # fmt: skip

        async def put(**settings):
            return await client.post("/api/remote/settings", json=settings)

        before = (await client.get("/api/remote")).json()
        assert before["enabled"] is False and before["restarts"] is True and before["port"] == 8766
        assert [e["name"] for e in before["environments"]] == ["dev", "main", "pay/staging"]

        on = {"enabled": True, "public_url": PUBLIC}
        assert "at least one environment" in (await put(**on, instances=[])).json()["error"]
        assert "this server's own" in (await put(**on, instances=["dev"], port=8000)).json()["error"]
        assert (
            "https address" in (await put(**{**on, "public_url": "http://x.test"}, instances=["dev"])).json()["error"]
        )
        assert "not defined in this file" in (await put(**on, instances=["nowhere"])).json()["error"]
        assert spawned == [] and "oauth" not in path.read_text("utf-8")

        assert (await put(**on, instances=["dev", "pay/staging"], port=8767)).json() == {
            "ok": True, "restarting": True
        }  # fmt: skip
        shown = (await client.get("/api/remote")).json()
        assert shown["enabled"] and shown["port"] == 8767 and shown["instances"] == ["dev", "pay/staging"]
        assert shown["running"] is False  # nothing listens on 8767 until the server is back
        assert "start" in spawned[0] and spawned[0][-4:] == ["--port", "8000", "--config", str(path)]

        assert (await put(enabled=False, instances=["dev"])).json()["ok"]
        off = (await client.get("/api/remote")).json()
        assert off["enabled"] is False and off["instances"] == ["dev"] and off["public_url"] == PUBLIC
        assert len(spawned) == 2


async def test_a_repository_file_cannot_turn_the_channel_on_from_the_admin_page(tmp_path):
    from graylog_mcp.admin.app import AdminState, build_app
    from graylog_mcp.setup import tomlwrite

    path = tmp_path / ".graylog-mcp.toml"
    path.write_text(tomlwrite.dumps({"instances": {"dev": PROD}}), "utf-8")
    state = AdminState(tmp_path, path, "tok", transport=FakeGraylog().transport, allowed_hosts=("testserver",))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_app(state)), base_url="http://testserver",
        headers={"X-Admin-Token": "tok"},
    ) as client:  # fmt: skip
        refused = await client.post("/api/remote/settings", json={"enabled": True, "public_url": PUBLIC,
                                                                   "instances": ["dev"]})  # fmt: skip
    assert refused.status_code == 400 and "repository's own config file" in refused.json()["error"]
    assert "oauth" not in path.read_text("utf-8")


def test_the_pair_command_prints_a_code_that_works_once(capsys):
    from graylog_mcp.__main__ import main

    assert main(["pair"]) == 0
    line = capsys.readouterr().out.splitlines()[0]
    code = line.split()[2]
    assert OAuthStore().use_code(code) is True and OAuthStore().use_code(code) is False
    assert main(["pair", "--list"]) == 0 and "no client paired" in capsys.readouterr().out
    assert main(["pair", "--revoke", "nobody"]) == 1


def test_a_call_over_the_channel_is_recorded_with_the_paired_client():
    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
    from mcp.server.auth.provider import AccessToken

    from graylog_mcp.recording import RecordContext

    token = AccessToken(token="fake-token", client_id="c1", scopes=[SCOPE], subject="ChatGPT")
    reset = auth_context_var.set(AuthenticatedUser(token))
    try:
        assert RecordContext.from_mcp(None, "describe").client == "client (paired: ChatGPT)"
    finally:
        auth_context_var.reset(reset)
