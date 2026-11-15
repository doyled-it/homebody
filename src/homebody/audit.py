"""The garage audit log: JSON lines, rotated, never raising.

Each line holds a UTC time, an event, the origin, a short actor tag, the door
state before, and the result. The tag is four hex characters of an HMAC of the
caller's identity under a random key kept beside the log, so the log never
holds an email or an address. Failures to write are logged without content.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from homebody.status import iso

log = logging.getLogger(__name__)

LOG_NAME = "garage-audit.log"
KEY_NAME = "garage-audit.key"

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_IPV6 = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:])")


def _scrub(text: str) -> str:
    """Drop anything shaped like an email or an address from free text."""
    text = _EMAIL.sub("[removed]", text)
    text = _IPV4.sub("[removed]", text)
    return _IPV6.sub("[removed]", text)


class AuditLog:
    def __init__(
        self,
        directory: Path,
        clock: Callable[[], float],
        *,
        max_bytes: int = 262144,
        keep: int = 2,
    ) -> None:
        self._dir = directory
        self._clock = clock
        self._max = max_bytes
        self._keep = keep
        self._key: bytes | None = None
        self.path = directory / LOG_NAME

    def _load_key(self) -> bytes:
        if self._key is not None:
            return self._key
        path = self._dir / KEY_NAME
        try:
            key = bytes.fromhex(path.read_text().strip())
            if len(key) >= 16:
                self._key = key
                return key
        except (OSError, ValueError):
            pass
        key = secrets.token_bytes(32)
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            tmp = self._dir / (KEY_NAME + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(key.hex())
            os.replace(tmp, path)
        except OSError:
            log.warning("audit key could not be saved; tags last until restart")
        self._key = key
        return key

    def tag(self, identity: str) -> str:
        digest = hmac.new(self._load_key(), identity.encode("utf-8"), sha256)
        return digest.hexdigest()[:4]

    def _rotate(self) -> None:
        for n in range(self._keep, 0, -1):
            src = self.path if n == 1 else self._old(n - 1)
            if not src.exists():
                continue
            if n == self._keep:
                self._old(n).unlink(missing_ok=True)
            os.replace(src, self._old(n))

    def _old(self, n: int) -> Path:
        return self._dir / f"{LOG_NAME}.{n}"

    def record(
        self,
        event: str,
        *,
        origin: Literal["home", "tunnel", "opener"],
        actor: str | None,
        door: str,
        result: str,
        kind: Literal["door", "command"] = "command",
    ) -> None:
        """Append one line. ``kind`` is ``door`` for a door change, else ``command``."""
        try:
            line = (
                json.dumps(
                    {
                        "at": iso(self._clock()),
                        "kind": kind,
                        "event": _scrub(event),
                        "origin": origin,
                        "actor": None if actor is None else _scrub(actor),
                        "door": _scrub(door),
                        "result": _scrub(result),
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            self._dir.mkdir(parents=True, exist_ok=True)
            try:
                size = self.path.stat().st_size
            except FileNotFoundError:
                size = 0
            if size and size + len(line.encode("utf-8")) > self._max:
                self._rotate()
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line)
        except Exception as exc:
            log.warning("audit write failed: %s", type(exc).__name__)

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        """The last ``limit`` door-change entries, oldest first."""
        out: list[dict[str, Any]] = []
        paths = [self._old(n) for n in range(self._keep, 0, -1)] + [self.path]
        for path in paths:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for raw in text.splitlines():
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or entry.get("kind") != "door":
                    continue
                out.append(
                    {
                        "at": entry.get("at"),
                        "event": entry.get("event"),
                        "origin": entry.get("origin"),
                        "actor": entry.get("actor"),
                        "result": entry.get("result"),
                    }
                )
        return out[-limit:] if limit > 0 else []
