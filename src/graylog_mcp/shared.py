"""One server for every session: ``graylog-mcp serve --shared``.

With stdio, each MCP client session starts its own server process. A shared server is one HTTP process
that every session connects to. Each client says which repository it works in (header ``X-Graylog-MCP-Repo``
or ``?repo=`` on the URL), and the server answers with that repository's configuration: its group, its focus
and its settings, as if it had been started there. Connections, detected versions and stream/field caches are
shared by every repository using the same Graylog instance.

A repository is named in one of two ways:

- its folder (an absolute path): the server reads the folder's ``.graylog-mcp.toml`` and git remote. Only for
  a server on the developer's machine (``http.repo_paths``, by default only on a loopback address); elsewhere
  the folder is not read and only its name is used.
- its identity: a git remote (``github.com/org/payment-api``, ``org/payment-api``) or a bare name. For a
  server on another machine (a VM for the team): the server's config lists the repositories in its groups and
  sets each one's focus and defaults under ``[repos."<repository>"]``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from graylog_mcp.backends import Graylog
from graylog_mcp.config import (
    Config,
    ConfigError,
    InstanceConfig,
    RepoInfo,
    find_project_config,
    is_repo_path,
    load_config,
    repo_from_identity,
)
from graylog_mcp.tools import App

log = logging.getLogger(__name__)

REPO_HEADER = "X-Graylog-MCP-Repo"
RELOAD_SECONDS = 5.0  # how often a repository's config is read again (edits apply without a restart)
MAX_ENTRIES = 256  # repositories kept; the key comes from the client, so the least recently used is dropped
RETIRE_SECONDS = 60.0  # a replaced Graylog client closes after this, so requests still using it can finish

# an absolute folder whatever the server's system: /x from macOS or Linux, C:\ or \\server\share from Windows
_ABSOLUTE = re.compile(r"^(?:/|[A-Za-z]:[\\/]|\\\\)")

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

    def __init__(
        self,
        config_path: str | None = None,
        default: Config | None = None,
        transport: Any = None,
        repo_paths: bool = True,
    ):
        self.config_path = config_path
        self.transport = transport
        self.repo_paths = repo_paths  # read the folders clients name (the server runs on their machine)
        self.graylogs: list[Graylog] = []
        self.entries: dict[Path | str, _Entry] = {}  # insertion order = recency, oldest first
        self._retired: list[Graylog] = []  # replaced clients waiting to close
        self._closing: set[asyncio.Task[None]] = set()
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
                    "(or ?repo= on the URL) with the repository's git remote or folder (${PWD} is empty on "
                    "Windows: write the folder itself), or start the server with --config"
                )
            return self.default
        key, info = self.identify(repo)
        entry = self.entries.pop(key, None)
        now = time.monotonic()
        reloaded = entry is None or now - entry.checked >= RELOAD_SECONDS
        if reloaded:
            entry = self._load(key, info, entry, now)
        assert entry is not None
        self.entries[key] = entry  # re-inserted last: most recently used
        evicted = len(self.entries) > MAX_ENTRIES
        while len(self.entries) > MAX_ENTRIES:
            del self.entries[next(iter(self.entries))]
        if reloaded or evicted:
            self._retire_unused()
        if entry.app is None:
            raise ConfigError(f"repository {key}: {entry.error}")
        return entry.app

    def identify(self, repo: str) -> tuple[Path | str, RepoInfo | None]:
        """The key of a repository named by a client, and its identity when its folder is not read."""
        repo = repo.strip()
        if is_repo_path(repo):
            path = Path(repo).expanduser()
            if not (path.is_absolute() or _ABSOLUTE.match(repo)):
                raise ConfigError(f"{REPO_HEADER}: expected an absolute folder or a git remote, got {repo!r}")
            if self.repo_paths and path.is_dir():
                return path.resolve(), None
            # a folder on the client's machine: only its name says which repository it is
            name = re.split(r"[\\/]", repo.rstrip("\\/"))[-1]
            info = repo_from_identity(name)
        else:
            info = repo_from_identity(repo)
        return info.label, info

    def _load(self, repo: Path | str, info: RepoInfo | None, previous: _Entry | None, now: float) -> _Entry:
        try:
            if isinstance(repo, Path):
                own = find_project_config(repo) is not None or bool(os.environ.get("GRAYLOG_MCP_CONFIG"))
                config = load_config(None if own else self.config_path, repo_dir=repo)
            else:
                config = load_config(self.config_path, repo=info)
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

    def _retire_unused(self) -> None:
        """Stop keeping clients no App uses any more (a changed instance setting makes a new client)."""
        in_use = {id(gl) for app in self.apps() for gl in app.instances.values()}
        unused = [gl for gl in self.graylogs if id(gl) not in in_use]
        if not unused:
            return
        self.graylogs = [gl for gl in self.graylogs if id(gl) in in_use]
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no event loop: nothing can have been using the client
            return
        self._retired.extend(unused)
        for gl in unused:
            task = loop.create_task(self._close_later(gl))
            self._closing.add(task)
            task.add_done_callback(self._closing.discard)

    async def _close_later(self, gl: Graylog) -> None:
        await asyncio.sleep(RETIRE_SECONDS)
        with contextlib.suppress(Exception):
            await gl.aclose()
        self._retired.remove(gl)

    async def close(self) -> None:
        for task in list(self._closing):
            task.cancel()
        for gl in [*self.graylogs, *self._retired]:
            try:
                await gl.aclose()
            except Exception as exc:  # pragma: no cover - closing is best effort
                log.debug("closing %s: %s", gl.cfg.name, exc)
