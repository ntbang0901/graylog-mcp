"""What the remote channel keeps between restarts: paired clients, hashed tokens and pairing codes.

One small JSON file, owner-only, that the server, the admin page and ``graylog-mcp pair`` all read and write;
each change reads it again first, under a lock, so a code added by one of them is not lost to another. Tokens
and codes are kept as SHA-256 hashes: a copy of the file gives no access. A client's registration is kept as
the client sent it, its secret included, which the token endpoint compares; without a token it opens nothing.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import secrets
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L: read off one screen, typed on another
CODE_LENGTH = 8
CODE_MINUTES = 10
MAX_CLIENTS = 50
MAX_CODES = 5

MAX_NAME = 80

_lock = threading.Lock()
log = logging.getLogger(__name__)


def label(name: str) -> str:
    """A client's name as it may be shown or logged: whoever registers chooses it."""
    clean = "".join(ch if ch.isprintable() else " " for ch in name).strip()
    return clean[:MAX_NAME] or "a client"


def default_path() -> Path:
    from graylog_mcp.setup import service

    return service.home_dir() / "oauth.json"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def normalize_code(text: str) -> str:
    return "".join(ch for ch in text.upper() if ch in CODE_ALPHABET)


def show_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


@contextlib.contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Across processes where the platform offers it (fcntl); within this one, always."""
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import fcntl
        except ImportError:  # Windows: one writer at a time within the process only
            yield
            return
        with open(path.with_suffix(".lock"), "a", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


class OAuthStore:
    def __init__(self, path: Path | None = None, clock: Callable[[], float] = time.time):
        self.path = path or default_path()
        self.clock = clock

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        except (ValueError, UnicodeDecodeError):
            data = None
        if not isinstance(data, dict):
            # a damaged file starts again empty, but is kept aside first: the next write would replace it
            aside = self.path.with_name(f"{self.path.name}.bad-{int(self.clock())}")
            os.replace(self.path, aside)
            log.warning(
                "remote channel: %s could not be read; moved to %s, every client must pair again", self.path, aside
            )
            data = {}
        for key in ("clients", "tokens", "codes"):
            if not isinstance(data.get(key), dict):
                data[key] = {}
        now = self.clock()
        data["tokens"] = {k: t for k, t in data["tokens"].items() if t.get("expires", 0) > now}
        data["codes"] = {k: c for k, c in data["codes"].items() if c.get("expires", 0) > now}
        return data

    def _write(self, data: dict[str, Any]) -> None:
        with contextlib.suppress(OSError):  # not supported everywhere (some Windows setups)
            os.chmod(self.path.parent, stat.S_IRWXU)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".oauth-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1)
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    @contextlib.contextmanager
    def _change(self) -> Iterator[dict[str, Any]]:
        with _file_lock(self.path):
            data = self._read()
            yield data
            self._write(data)

    def _view(self) -> dict[str, Any]:
        with _file_lock(self.path):
            return self._read()

    # ---------------------------------------------------------------- pairing codes

    def new_code(self) -> tuple[str, float]:
        """A one-time code for the pairing page, and when it expires. Older unused codes stay valid until then,
        up to the last ``MAX_CODES``."""
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        expires = self.clock() + CODE_MINUTES * 60
        with self._change() as data:
            data["codes"][digest(code)] = {"expires": expires}
            newest = sorted(data["codes"].items(), key=lambda kv: kv[1]["expires"])[-MAX_CODES:]
            data["codes"] = dict(newest)
        return code, expires

    def use_code(self, text: str) -> bool:
        """Whether ``text`` is a live code; a code that matched is gone."""
        code = normalize_code(text)
        if len(code) != CODE_LENGTH:
            return False
        with self._change() as data:
            return data["codes"].pop(digest(code), None) is not None

    def drop_codes(self) -> None:
        with self._change() as data:
            data["codes"] = {}

    # ---------------------------------------------------------------- clients

    def add_client(self, info: dict[str, Any]) -> None:
        with self._change() as data:
            data["clients"][info["client_id"]] = {"info": info, "paired": None, "used": None}
            if len(data["clients"]) > MAX_CLIENTS:
                # a client never paired goes first, then the one used longest ago
                order = sorted(data["clients"], key=lambda c: (data["clients"][c]["paired"] is not None,
                                                                data["clients"][c]["used"] or 0))  # fmt: skip
                for client_id in order[: len(data["clients"]) - MAX_CLIENTS]:
                    self._drop(data, client_id)

    def client(self, client_id: str) -> dict[str, Any] | None:
        entry = self._view()["clients"].get(client_id)
        return entry["info"] if entry else None

    def clients(self) -> list[dict[str, Any]]:
        """The paired clients, for the admin page: no secret, no token."""
        data = self._view()
        out = []
        for client_id, entry in data["clients"].items():
            if entry["paired"] is None:
                continue
            live = [t for t in data["tokens"].values() if t["client_id"] == client_id and t["kind"] == "refresh"]
            out.append(
                {
                    "client_id": client_id,
                    "name": label(entry["info"].get("client_name") or client_id),
                    "paired": entry["paired"],
                    "used": entry["used"],
                    "active": bool(live),
                }
            )
        return sorted(out, key=lambda c: c["paired"], reverse=True)

    def mark_paired(self, client_id: str) -> None:
        with self._change() as data:
            if client_id in data["clients"]:
                data["clients"][client_id]["paired"] = self.clock()

    @staticmethod
    def _drop(data: dict[str, Any], client_id: str) -> None:
        data["clients"].pop(client_id, None)
        data["tokens"] = {k: t for k, t in data["tokens"].items() if t["client_id"] != client_id}

    def remove_client(self, client_id: str) -> bool:
        with self._change() as data:
            known = client_id in data["clients"]
            self._drop(data, client_id)
            return known

    def remove_all(self) -> int:
        with self._change() as data:
            count = sum(1 for c in data["clients"].values() if c["paired"] is not None)
            data["clients"], data["tokens"] = {}, {}
            return count

    # ---------------------------------------------------------------- tokens

    def issue(
        self, client_id: str, scopes: list[str], resource: str | None, access_seconds: int, refresh_seconds: int
    ) -> tuple[str, str]:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = self.clock()
        # the pair shares a grant: revoking either one revokes both
        common = {"client_id": client_id, "scopes": scopes, "resource": resource, "grant": secrets.token_hex(8)}
        with self._change() as data:
            if client_id not in data["clients"]:
                raise KeyError(client_id)
            data["tokens"][digest(access)] = {"kind": "access", "expires": now + access_seconds, **common}
            data["tokens"][digest(refresh)] = {"kind": "refresh", "expires": now + refresh_seconds, **common}
        return access, refresh

    def token(self, value: str, kind: str) -> dict[str, Any] | None:
        """A live token of that kind, marking its client used; None when unknown, expired or revoked."""
        data = self._view()
        found = data["tokens"].get(digest(value))
        if found is None or found["kind"] != kind or found["client_id"] not in data["clients"]:
            return None
        if (data["clients"][found["client_id"]]["used"] or 0) < self.clock() - 60:  # not a write per request
            with self._change() as fresh:
                if found["client_id"] in fresh["clients"]:
                    fresh["clients"][found["client_id"]]["used"] = self.clock()
        return dict(found)

    def revoke(self, value: str) -> None:
        with self._change() as data:
            found = data["tokens"].pop(digest(value), None)
            if found is not None and found.get("grant"):
                data["tokens"] = {k: t for k, t in data["tokens"].items() if t.get("grant") != found["grant"]}

    def client_name(self, client_id: str) -> str:
        info = self.client(client_id) or {}
        return str(info.get("client_name") or client_id)
