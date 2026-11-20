"""The channel for a client on the internet (ChatGPT web): OAuth 2.1 with a pairing code, on a port of its own.

The port serves only the MCP endpoint, the OAuth endpoints and the pairing page; the admin page and /status stay
on the local port, whatever Host header a tunnel sends. Anyone who can reach the tunnel may register a client
(dynamic registration), but only to the redirect addresses the config allows, and a client gets a token only
after someone types a code shown on this machine. Its calls see the environments marked ``remote_access``.
"""

from __future__ import annotations

import html
import json
import logging
import secrets
import time
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from graylog_mcp.config import Config, ConfigError, Focus, OAuthConfig, _applies
from graylog_mcp.oauth_store import OAuthStore, label

log = logging.getLogger(__name__)

SCOPE = "graylog:read"
PAIR_PATH = "/oauth/pair"
PENDING_SECONDS = 600
MAX_PENDING = 20
MAX_TRIES = 5
CODE_SECONDS = 300
MAX_REGISTRATION = 4096  # bytes of one client's metadata: the store is read on every request
# wrong codes, over every request, within PENDING_SECONDS that void the live codes: requests are free to make, so
# the tries of one request alone would not stop guessing
MAX_WRONG = 20


def _same_url(url: str) -> str:
    """'https://chat.example.com' and 'https://chat.example.com/' are one address, however each side wrote it."""
    from pydantic import AnyUrl

    return str(AnyUrl(url))


@dataclass
class _Pending:
    client_id: str
    client_name: str
    params: AuthorizationParams
    created: float
    tries: int = 0


class PairingProvider:
    """The SDK's authorization server provider, with a pairing code between authorize and the code it grants."""

    def __init__(self, oauth: OAuthConfig, store: OAuthStore, resource: str, clock: Any = time.time):
        self.oauth, self.store, self.resource, self.clock = oauth, store, resource, clock
        self.pending: dict[str, _Pending] = {}
        self.codes: dict[str, AuthorizationCode] = {}
        self.wrong: list[float] = []
        self.allowed = {_same_url(u) for u in oauth.allowed_redirects}

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        info = self.store.client(client_id)
        return OAuthClientInformationFull.model_validate(info) if info else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        refused = [str(u) for u in client_info.redirect_uris or [] if _same_url(str(u)) not in self.allowed]
        if refused or not client_info.redirect_uris:
            log.warning("remote channel: refused a client registering %d other redirect(s)", len(refused))
            raise RegistrationError(
                "invalid_redirect_uri", "this server only redirects to: " + ", ".join(self.oauth.allowed_redirects)
            )
        info = client_info.model_dump(mode="json", exclude_none=True)
        if len(json.dumps(info)) > MAX_REGISTRATION:
            log.warning("remote channel: refused a client registration over %d bytes", MAX_REGISTRATION)
            raise RegistrationError("invalid_client_metadata", f"client metadata over {MAX_REGISTRATION} bytes")
        self.store.add_client(info)
        log.info("remote channel: client registered: %s", label(client_info.client_name or ""))

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        now = self.clock()
        self.pending = {k: p for k, p in self.pending.items() if p.created > now - PENDING_SECONDS}
        while len(self.pending) >= MAX_PENDING:
            self.pending.pop(min(self.pending, key=lambda k: self.pending[k].created))
        request = secrets.token_urlsafe(16)
        self.pending[request] = _Pending(client.client_id or "", label(client.client_name or ""), params, now)
        return f"{self.oauth.public_url}{PAIR_PATH}?request={request}"

    def waiting(self, request: str) -> _Pending | None:
        found = self.pending.get(request)
        if found is None or found.created <= self.clock() - PENDING_SECONDS:
            return None
        return found

    def pair(self, request: str, code: str) -> tuple[str, str | None]:
        """("ok", where to send the browser), ("wrong", None) with tries left, or ("gone", None)."""
        found = self.waiting(request)
        if found is None:
            return "gone", None
        if not self.store.use_code(code):
            found.tries += 1
            now = self.clock()
            self.wrong = [t for t in self.wrong if t > now - PENDING_SECONDS] + [now]
            log.warning(
                "remote channel: wrong pairing code for %s (%d of %d)", found.client_name, found.tries, MAX_TRIES
            )
            if len(self.wrong) >= MAX_WRONG:
                self.store.drop_codes()
                self.wrong = []
                log.warning(
                    "remote channel: %d wrong pairing codes: every live code is void, make a new one", MAX_WRONG
                )
            if found.tries >= MAX_TRIES:
                self.pending.pop(request, None)
                return "gone", None
            return "wrong", None
        self.pending.pop(request, None)
        params = found.params
        value = secrets.token_urlsafe(32)
        self.codes[value] = AuthorizationCode(
            code=value,
            scopes=params.scopes or [SCOPE],
            expires_at=self.clock() + CODE_SECONDS,
            client_id=found.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        self.store.mark_paired(found.client_id)
        log.info("remote channel: paired %s", found.client_name)
        return "ok", construct_redirect_uri(str(params.redirect_uri), code=value, state=params.state)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        found = self.codes.get(authorization_code)
        if found is None or found.client_id != client.client_id or found.expires_at < self.clock():
            return None
        return found

    def _tokens(self, client_id: str, scopes: list[str]) -> OAuthToken:
        """Every token is for this server's MCP endpoint, the one resource it serves, whatever a client asked."""
        access_seconds = self.oauth.access_minutes * 60
        refresh_seconds = self.oauth.refresh_days * 86400
        access, refresh = self.store.issue(client_id, scopes, self.resource, access_seconds, refresh_seconds)
        return OAuthToken(access_token=access, expires_in=access_seconds, scope=" ".join(scopes), refresh_token=refresh)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if self.codes.pop(authorization_code.code, None) is None:
            raise TokenError("invalid_grant", "authorization code already used")
        return self._tokens(client.client_id or "", authorization_code.scopes)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        found = self.store.token(refresh_token, "refresh")
        if found is None or found["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token, client_id=found["client_id"], scopes=found["scopes"], expires_at=int(found["expires"])
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        self.store.revoke(refresh_token.token)  # a refresh token works once: a stolen copy dies at the next use
        log.info("remote channel: tokens refreshed for %s", label(client.client_name or ""))
        return self._tokens(client.client_id or "", scopes or refresh_token.scopes)

    async def load_access_token(self, token: str) -> AccessToken | None:
        found = self.store.token(token, "access")
        if found is None:
            return None
        return AccessToken(
            token=token,
            client_id=found["client_id"],
            scopes=found["scopes"],
            expires_at=int(found["expires"]),
            resource=found.get("resource"),
            subject=label(self.store.client_name(found["client_id"])),
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.store.revoke(token.token)  # with the other token of its pair: a revoked refresh kills its access too

    async def exchange_identity_assertion(self, client: OAuthClientInformationFull, params: Any) -> OAuthToken:
        raise TokenError("unsupported_grant_type", "only authorization_code and refresh_token")


def remote_config(config: Config) -> Config:
    """``config`` with only the environments marked ``remote_access``: the others are not named, not even in
    an error, to a client on the internet."""
    keep = {name: inst for name, inst in config.instances.items() if inst.remote_access}
    if not keep:
        raise ConfigError(
            "http.oauth: no environment has remote_access = true; set it on each one a remote client may read"
        )
    groups = {g: info for g, info in config.groups.items() if any(i.group == g for i in keep.values())}

    def kept(names: tuple[str, ...]) -> tuple[str, ...] | None:
        """``names`` (instances, groups, environments) left with those of a kept instance; None when it named
        some and none is kept: then the item it limits goes too, rather than apply everywhere."""
        left = tuple(n for n in names if any(_applies((n,), inst) for inst in keep.values()))
        return None if names and not left else left

    def trimmed(items: dict[str, Any]) -> dict[str, Any]:
        return {k: replace(v, instances=left) for k, v in items.items() if (left := kept(v.instances)) is not None}

    presets = {
        name: preset
        for name, preset in config.presets.items()
        if not isinstance(preset.args.get("instance"), str) or kept((preset.args["instance"],))
    }
    terms = {k: t if t.preset in presets or t.preset is None else replace(t, preset=None)
             for k, t in trimmed(config.glossary.terms).items()}  # fmt: skip
    return replace(
        config,
        instances=keep,
        presets=presets,
        glossary=replace(config.glossary, terms=terms, codes=trimmed(config.glossary.codes)),
        scan=replace(config.scan, rules=trimmed(config.scan.rules)),
        default_instance=config.default_instance if config.default_instance in keep else next(iter(keep)),
        groups=groups,
        default_group=config.default_group if config.default_group in groups else None,
        out_of_scope={},
        scope=None,
        scope_reason=None,
        current_repo=None,
        repo_group=None,
        focus=Focus(),
        ui_links=False,  # links to this machine's admin page mean nothing to a remote client
    )


def auth_settings(oauth: OAuthConfig, path: str) -> AuthSettings:
    return AuthSettings(
        issuer_url=oauth.public_url,  # type: ignore[arg-type]
        resource_server_url=oauth.public_url + path,  # type: ignore[arg-type]
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
        ),
        revocation_options=RevocationOptions(enabled=True),
        required_scopes=[SCOPE],
        validate_token_resource=True,
    )


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Pair with graylog-mcp</title>
<style>body{{font:16px system-ui,sans-serif;max-width:28rem;margin:3rem auto;padding:0 1rem;color:#1d2433}}
input{{font:1.4rem ui-monospace,monospace;letter-spacing:.15em;width:100%;padding:.5rem;box-sizing:border-box}}
button{{font:inherit;margin-top:1rem;padding:.5rem 1.2rem}}.note{{color:#5b6475;font-size:.9rem}}
.bad{{color:#b42318}}</style></head><body><h1>Pair with graylog-mcp</h1>{body}</body></html>"""


def _page(body: str, status: int, redirect_origin: str = "") -> Any:
    from starlette.responses import HTMLResponse

    form_action = f"'self' {redirect_origin}".strip()
    return HTMLResponse(
        _PAGE.format(body=body),
        status_code=status,
        headers={
            # the browser follows the redirect a successful post answers with: form-action must allow it
            "Content-Security-Policy": f"default-src 'none'; style-src 'unsafe-inline'; form-action {form_action}; "
            "frame-ancestors 'none'; base-uri 'none'",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
        },
    )


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def pair_page(provider: PairingProvider) -> Any:
    from starlette.requests import Request
    from starlette.responses import RedirectResponse

    gone = (
        "<p class='bad'>This request has expired or was cancelled after too many wrong codes.</p>"
        "<p>Start again from the app that sent you here.</p>"
    )

    def form(request: str, found: Any, message: str = "") -> str:
        who = html.escape(found.client_name)
        return (
            f"<p><b>{who}</b> asks to read logs through graylog-mcp on this computer.</p>{message}"
            f"<form method='post'><input type='hidden' name='request' value='{html.escape(request)}'>"
            "<label for='code'>Pairing code</label>"
            "<input id='code' name='code' autocomplete='off' autofocus required placeholder='XXXX-XXXX'>"
            "<button type='submit'>Allow</button></form>"
            "<p class='note'>Get the code on the computer running graylog-mcp: admin page, Connect, "
            "<i>ChatGPT web</i>, <i>New pairing code</i>; or <code>graylog-mcp pair</code>. Did not start this "
            "yourself? Close this page.</p>"
        )

    async def handle(request: Request) -> Any:
        if request.method == "GET":
            key = request.query_params.get("request", "")
            found = provider.waiting(key)
            if found is None:
                return _page(gone, 410)
            return _page(form(key, found), 200, _origin(str(found.params.redirect_uri)))
        data = await request.form()
        key, code = str(data.get("request", "")), str(data.get("code", ""))
        found = provider.waiting(key)
        status, location = provider.pair(key, code)
        if status == "ok" and location:
            return RedirectResponse(location, status_code=302, headers={"Cache-Control": "no-store"})
        if status == "gone" or found is None:
            return _page(gone, 410)
        left = MAX_TRIES - found.tries
        message = f"<p class='bad'>That code is not right or has expired. {left} tries left.</p>"
        return _page(form(key, found, message), 403, _origin(str(found.params.redirect_uri)))

    return handle


def build_remote_app(config: Config, transport: Any = None, store: OAuthStore | None = None) -> Any:
    """The ASGI app of the remote channel, over its own App holding only the ``remote_access`` environments."""
    from mcp.server.transport_security import TransportSecuritySettings

    from graylog_mcp.server import build_server
    from graylog_mcp.tools import App

    oauth = config.http.oauth
    if not oauth.enabled:
        raise ConfigError("http.oauth: not enabled")
    provider = PairingProvider(oauth, store or OAuthStore(), oauth.public_url + config.http.path)
    server = build_server(
        App.create(remote_config(config), transport), auth=(provider, auth_settings(oauth, config.http.path))
    )
    server.custom_route(PAIR_PATH, methods=["GET", "POST"])(pair_page(provider))
    # every request needs a token: a browser on this machine tricked into calling the port has none to send
    security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return server.streamable_http_app(
        streamable_http_path=config.http.path, host=oauth.host, transport_security=security
    )
