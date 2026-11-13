"""The settings API: a tiny local HTTP service inside ``homebody run``.

One endpoint, ``POST /api/homebody/settings``, takes a JSON object with any
subset of ``enabled``, ``comfort_f``, ``away_f`` and ``away_minutes`` and
answers with the full status document. It listens on loopback only; the hub's
nginx proxies ``/api/homebody/`` to it. Requests need the ``X-Snaphouse: 1``
header (a cheap guard against a stray browser form post, not authentication).

The handler is a pure function over (method, path, headers, body) so the rules
are tested without sockets; ``start`` wraps it in a minimal asyncio HTTP server
(stdlib only, one request per connection).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import Any, Protocol

log = logging.getLogger(__name__)

SETTINGS_PATH = "/api/homebody/settings"
GUARD_HEADER = "x-snaphouse"
MAX_BODY = 64 * 1024
MAX_HEADER_BYTES = 16 * 1024
READ_TIMEOUT_SECONDS = 10.0

# key: (minimum, maximum, plain name for messages)
RANGES = {
    "comfort_f": (60, 85, "Comfort temperature"),
    "away_f": (65, 90, "Away temperature"),
    "away_minutes": (5, 120, "Away delay"),
}
KEYS = ("enabled", *RANGES)

Response = tuple[int, dict[str, Any]]


class SettingsTarget(Protocol):
    def settings(self) -> dict[str, Any]: ...

    def apply_settings(self, changes: dict[str, Any]) -> dict[str, Any]: ...


class SettingsError(ValueError):
    """A request body that cannot be applied; the message is shown to people."""


def validate(body: Any, current: Mapping[str, Any]) -> dict[str, Any]:
    """Check a settings body against the current settings; return the changes."""
    if not isinstance(body, dict):
        raise SettingsError("The request body must be a JSON object.")
    unknown = sorted(k for k in body if k not in KEYS)
    if unknown:
        raise SettingsError(f"Unknown setting: {unknown[0]}.")
    changes: dict[str, Any] = {}
    for key, value in body.items():
        if key == "enabled":
            if not isinstance(value, bool):
                raise SettingsError("Automation must be true or false.")
        else:
            low, high, label = RANGES[key]
            if isinstance(value, bool) or not isinstance(value, int):
                raise SettingsError(f"{label} must be a whole number.")
            if not low <= value <= high:
                raise SettingsError(f"{label} must be between {low} and {high}.")
        changes[key] = value
    merged = {**current, **changes}
    if merged["comfort_f"] >= merged["away_f"]:
        raise SettingsError(
            "The comfort temperature must be below the away temperature."
        )
    return changes


def handle(
    target: SettingsTarget,
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
) -> Response:
    """Answer one request. Header names are lower-case."""
    if path != SETTINGS_PATH:
        return 404, {"error": "Not found."}
    if method != "POST":
        return 405, {"error": "Use POST for settings."}
    if headers.get(GUARD_HEADER) != "1":
        return 403, {"error": "Missing the X-Snaphouse header."}
    try:
        parsed = json.loads(body.decode())
    except (UnicodeDecodeError, ValueError):
        return 400, {"error": "The request body is not valid JSON."}
    try:
        changes = validate(parsed, target.settings())
    except SettingsError as error:
        return 400, {"error": str(error)}
    return 200, target.apply_settings(changes)


_REASONS = {
    200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
    405: "Method Not Allowed", 413: "Payload Too Large",
}


def _encode(code: int, document: dict[str, Any]) -> bytes:
    payload = json.dumps(document).encode()
    head = (
        f"HTTP/1.1 {code} {_REASONS.get(code, 'Error')}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n\r\n"
    )
    return head.encode() + payload


async def _read_request(
    reader: asyncio.StreamReader,
) -> tuple[str, str, dict[str, str], int]:
    """Parse the request line and headers; return the declared body length."""
    raw = await reader.readuntil(b"\r\n\r\n")
    if len(raw) > MAX_HEADER_BYTES:
        raise ValueError("headers too large")
    lines = raw.decode("latin-1").split("\r\n")
    parts = lines[0].split()
    if len(parts) != 3 or not parts[2].startswith("HTTP/"):
        raise ValueError("bad request line")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(":")
        if not sep:
            raise ValueError("bad header")
        headers[name.strip().lower()] = value.strip()
    length = headers.get("content-length", "0")
    if not length.isdigit():
        raise ValueError("bad content length")
    return parts[0], parts[1].split("?", 1)[0], headers, int(length)


def _serve_one(target: SettingsTarget):
    async def connection(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            async with asyncio.timeout(READ_TIMEOUT_SECONDS):
                try:
                    method, path, headers, length = await _read_request(reader)
                except (ValueError, asyncio.IncompleteReadError,
                        asyncio.LimitOverrunError):
                    response = _encode(400, {"error": "Malformed request."})
                else:
                    if length > MAX_BODY:
                        response = _encode(413, {"error": "The request is too large."})
                    else:
                        body = await reader.readexactly(length) if length else b""
                        code, document = handle(target, method, path, headers, body)
                        response = _encode(code, document)
            writer.write(response)
            await writer.drain()
        except (TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception:
            log.exception("settings request failed")
        finally:
            writer.close()

    return connection


async def start(target: SettingsTarget, host: str, port: int) -> asyncio.Server:
    """Listen for settings requests; the caller closes the returned server."""
    return await asyncio.start_server(_serve_one(target), host, port)
