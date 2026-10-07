"""One server for every session: ``graylog-mcp serve --shared``.

With stdio, each MCP client session starts its own server process. A shared server is one HTTP process
that every session connects to. Each client says which repository it works in (header ``X-Graylog-MCP-Repo``
or ``?repo=`` on the URL), and the server answers with that repository's configuration: its
``.graylog-mcp.toml``, its group and its focus, as if it had been started there. Connections, detected
versions and stream/field caches are shared by every repository using the same Graylog instance.
"""

from __future__ import annotations

import logging
import os
import time
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from graylog_mcp.backends import Graylog
from graylog_mcp.config import Config, ConfigError, InstanceConfig, find_project_config, load_config
from graylog_mcp.tools import App

log = logging.getLogger(__name__)

REPO_HEADER = "X-Graylog-MCP-Repo"
RELOAD_SECONDS = 5.0  # how often a repository's config is read again (edits apply without a restart)

current_repo: ContextVar[str | None] = ContextVar("graylog_mcp_repo", default=None)


class RepoContext:
    """ASGI middleware: the repository of the request, from the header or the ``repo`` query parameter."""

    def __init__(self, app: Any):
        self.app = app
        self.header = REPO_HEADER.lower().encode()

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        value = dict(scope.get("headers") or []).get(self.header, b"").decode("utf-8", "replace").strip()
        if not value:
            query = parse_qs(scope.get("query_string", b"").decode("utf-8", "replace"))
            value = (query.get("repo") or [""])[0].strip()
        token = current_repo.set(value or None)
        try:
            await self.app(scope, receive, send)
        finally:
            current_repo.reset(token)


@dataclass
class _Entry:
    app: App | None
    error: str | None
    checked: float


class AppPool:
    """The App of each repository, loaded on first use and reloaded when its configuration changes.

    A repository uses its own ``.graylog-mcp.toml``; one without it uses ``config_path`` (the server's
    ``--config``, e.g. the company file listing every group and its repositories), else the user config.
    The group and focus always follow the repository. ``default`` serves requests naming no repository.
    """

    def __init__(self, config_path: str | None = None, default: Config | None = None, transport: Any = None):
        self.config_path = config_path
        self.transport = transport
        self.graylogs: list[Graylog] = []
        self.entries: dict[Path | None, _Entry] = {}
        self.default = App.create(default, graylog=self._graylog) if default is not None else None

    def _graylog(self, cfg: InstanceConfig) -> Graylog:
        """One client per distinct instance settings, shared by every repository using them."""
        for gl in self.graylogs:
            if gl.cfg == cfg:
                return gl
        gl = Graylog(cfg, self.transport)
        self.graylogs.append(gl)
        return gl

    def current(self) -> App:
        return self.get(current_repo.get())

    def get(self, repo: str | None) -> App:
        if not repo:
            if self.default is None:
                raise ConfigError(
                    f"no repository given and no default configuration: send the {REPO_HEADER} header "
                    "(or ?repo=<folder> on the URL) with the repository folder, or start the server with --config"
                )
            return self.default
        path = Path(repo).expanduser()
        if not path.is_absolute():
            raise ConfigError(f"{REPO_HEADER}: expected an absolute folder, got {repo!r}")
        key = path.resolve()
        entry = self.entries.get(key)
        now = time.monotonic()
        if entry is None or now - entry.checked >= RELOAD_SECONDS:
            entry = self._load(key, entry, now)
            self.entries[key] = entry
        if entry.app is None:
            raise ConfigError(f"repository {key}: {entry.error}")
        return entry.app

    def _load(self, repo: Path, previous: _Entry | None, now: float) -> _Entry:
        own = find_project_config(repo) is not None or bool(os.environ.get("GRAYLOG_MCP_CONFIG"))
        try:
            config = load_config(None if own else self.config_path, repo_dir=repo)
        except ConfigError as exc:
            if previous is None or previous.error != str(exc):
                log.warning("repository %s: %s", repo, exc)
            return _Entry(None, str(exc), now)
        if previous is not None and previous.app is not None and previous.app.config == config:
            return _Entry(previous.app, None, now)
        if previous is None or previous.app is None:
            log.info("repository %s: config %s, default instance %s", repo, config.source, config.default_instance)
        else:
            log.info("repository %s: configuration changed, reloaded", repo)
        return _Entry(App.create(config, graylog=self._graylog), None, now)

    def apps(self) -> list[App]:
        found = [e.app for e in self.entries.values() if e.app is not None]
        return [self.default, *found] if self.default is not None else found

    async def close(self) -> None:
        for gl in self.graylogs:
            try:
                await gl.aclose()
            except Exception as exc:  # pragma: no cover - closing is best effort
                log.debug("closing %s: %s", gl.cfg.name, exc)
