"""The settings API: a tiny local HTTP service inside ``homebody run``.

``POST /api/homebody/settings`` takes a JSON object with any
subset of ``enabled``, ``comfort_f``, ``away_f`` and ``away_minutes`` and
answers with the full status document. It listens on loopback only; the hub's
nginx proxies ``/api/homebody/`` to it. Requests need the ``X-Snaphouse: 1``
header (a cheap guard against a stray browser form post, not authentication).

Three more endpoints let a phone on the home Wi-Fi count itself as home:
``GET people/this-device``, ``POST people/add`` and ``POST people/remove``. The
device is found from the ``X-Real-IP`` header nginx sets, looked up in the
kernel neighbour table. A request with ``CF-Connecting-IP`` came through the
tunnel from away, so it cannot add (removing is allowed).

The handler is a pure function over (method, path, headers, body) so the rules
are tested without sockets; ``start`` wraps it in a minimal asyncio HTTP server
(stdlib only, one request per connection).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from typing import Any, Literal, Protocol

from homebody import presence
from homebody.config import GARAGE_GRACE_RANGE, GARAGE_LEFT_OPEN_RANGE
from homebody.garage_commands import CommandError, CommandGate
from homebody.people import PeopleError

log = logging.getLogger(__name__)

SETTINGS_PATH = "/api/homebody/settings"
PEOPLE_DEVICE = "/api/homebody/people/this-device"
PEOPLE_ADD = "/api/homebody/people/add"
PEOPLE_REMOVE = "/api/homebody/people/remove"
PEOPLE_PATHS = {
    PEOPLE_DEVICE: "GET",
    PEOPLE_ADD: "POST",
    PEOPLE_REMOVE: "POST",
}
GARAGE_INTENT = "/api/homebody/garage/intent"
GARAGE_COMMAND = "/api/homebody/garage/command"
GARAGE_PATHS = (GARAGE_INTENT, GARAGE_COMMAND)
GUARD_HEADER = "x-snaphouse"
ORIGIN_HEADER = "x-snaphouse-origin"
ACCESS_EMAIL_HEADER = "cf-access-authenticated-user-email"
MAX_ACTION = 16
MAX_NONCE = 128
MAX_BODY = 64 * 1024
MAX_HEADER_BYTES = 16 * 1024
READ_TIMEOUT_SECONDS = 10.0
# A garage request waits on the door, so it runs under its own cap. The cap is
# the target's ``timeout_seconds`` (the config waits plus headroom); this is
# what a target without one gets.
GARAGE_TIMEOUT_SECONDS = 45.0

# key: (minimum, maximum, plain name for messages)
RANGES = {
    "comfort_f": (60, 85, "Comfort temperature"),
    "away_f": (65, 90, "Away temperature"),
    "away_minutes": (5, 120, "Away delay"),
}
GARAGE_RANGES = {
    "garage_left_open_minutes": (*GARAGE_LEFT_OPEN_RANGE, "Left-open alert delay"),
    "garage_away_open_grace_minutes": (*GARAGE_GRACE_RANGE, "Arrival grace"),
}
GARAGE_FLAGS = {
    "garage_alerts": "Garage alerts",
    "garage_remote": "Remote garage control",
}
GARAGE_KEYS = (*GARAGE_RANGES, *GARAGE_FLAGS)
KEYS = ("enabled", *RANGES, *GARAGE_KEYS)
GARAGE_OFF = "The garage is not set up."

Response = tuple[int, dict[str, Any]]


class SettingsTarget(Protocol):
    def settings(self) -> dict[str, Any]: ...

    def apply_settings(self, changes: dict[str, Any]) -> dict[str, Any]: ...

    def counted_as(self, wifi_mac: str) -> str | None: ...

    def add_person(self, name: object, wifi_mac: str) -> dict[str, Any]: ...

    def remove_person(self, person_id: object) -> dict[str, Any]: ...

    def publish(self) -> dict[str, Any]: ...


class GarageTarget(Protocol):
    gate: CommandGate
    # The cap on one garage request: close wait plus note wait plus headroom.
    timeout_seconds: float

    def snapshot(self) -> dict | None: ...

    def actor_tag(self, email: str) -> str: ...


class SettingsError(ValueError):
    """A request body that cannot be applied; the message is shown to people."""


def validate(
    body: Any, current: Mapping[str, Any], garage: bool = False
) -> dict[str, Any]:
    """Check a settings body against the current settings; return the changes."""
    if not isinstance(body, dict):
        raise SettingsError("The request body must be a JSON object.")
    unknown = sorted(k for k in body if k not in KEYS)
    if unknown:
        raise SettingsError(f"Unknown setting: {unknown[0]}.")
    if not garage and any(k in GARAGE_KEYS for k in body):
        raise SettingsError(GARAGE_OFF)
    changes: dict[str, Any] = {}
    for key, value in body.items():
        if key == "enabled":
            if not isinstance(value, bool):
                raise SettingsError("Automation must be true or false.")
        elif key in GARAGE_FLAGS:
            if not isinstance(value, bool):
                raise SettingsError(f"{GARAGE_FLAGS[key]} must be true or false.")
        else:
            low, high, label = {**RANGES, **GARAGE_RANGES}[key]
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


Lookup = Callable[[str], "str | None"]

_REMOTE_ERROR = "Count a phone from the home Wi-Fi, not from away."
_UNKNOWN_ERROR = "The hub could not tell which device this is."


def resolve_device(
    headers: Mapping[str, str], lookup: Lookup
) -> tuple[str | None, str | None]:
    """Find the caller's hardware address: ``(mac, reason)``.

    ``reason`` is ``"remote"`` for a request through the tunnel (checked first)
    and ``"unknown"`` when the address cannot be found.
    """
    if "cf-connecting-ip" in headers:
        return None, "remote"
    ip = headers.get("x-real-ip")
    mac = lookup(ip) if ip else None
    if mac is None:
        return None, "unknown"
    return mac, None


def _json_object(body: bytes, allowed: tuple[str, ...]) -> dict[str, Any] | Response:
    """Parse a body that must be an object with only ``allowed`` keys."""
    try:
        parsed = json.loads(body.decode())
    except (UnicodeDecodeError, ValueError):
        return 400, {"error": "The request body is not valid JSON."}
    if not isinstance(parsed, dict):
        return 400, {"error": "The request body must be a JSON object."}
    unknown = sorted(k for k in parsed if k not in allowed)
    if unknown:
        return 400, {"error": f"Unknown field: {unknown[0]}."}
    return parsed


def _handle_people(
    target: SettingsTarget,
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
    lookup: Lookup,
) -> Response:
    if method != PEOPLE_PATHS[path]:
        return 405, {"error": f"Use {PEOPLE_PATHS[path]} here."}
    if path == PEOPLE_DEVICE:
        mac, reason = resolve_device(headers, lookup)
        counted = target.counted_as(mac) if mac else None
        return 200, {
            "can_add": mac is not None and counted is None,
            "counted_as": counted,
            "reason": reason,
        }
    if headers.get(GUARD_HEADER) != "1":
        return 403, {"error": "Missing the X-Snaphouse header."}
    if path == PEOPLE_ADD:
        mac, reason = resolve_device(headers, lookup)
        if mac is None:
            message = _REMOTE_ERROR if reason == "remote" else _UNKNOWN_ERROR
            return 403, {"error": message, "reason": reason}
    parsed = _json_object(body, ("name",) if path == PEOPLE_ADD else ("id",))
    if isinstance(parsed, tuple):
        return parsed
    try:
        if path == PEOPLE_ADD:
            document = target.add_person(parsed.get("name"), mac)
        else:
            document = target.remove_person(parsed.get("id"))
    except PeopleError as error:
        return error.status, {"error": str(error)}
    return 200, document


def origin_of(headers: Mapping[str, str]) -> Literal["home", "tunnel"]:
    """``home`` only when the hub's nginx set ``X-Snaphouse-Origin`` to exactly
    ``home``. Missing or anything else is the tunnel (the safe side)."""
    return "home" if headers.get(ORIGIN_HEADER) == "home" else "tunnel"


def handle(
    target: SettingsTarget,
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
    lookup: Lookup = presence.neighbour_mac,
    garage: GarageTarget | None = None,
) -> Response:
    """Answer one request. Header names are lower-case."""
    if path in PEOPLE_PATHS:
        return _handle_people(target, method, path, headers, body, lookup)
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
        changes = validate(parsed, target.settings(), garage is not None)
    except SettingsError as error:
        return 400, {"error": str(error)}
    if changes.get("garage_remote") is True and origin_of(headers) != "home":
        return 403, {"error": "Turn remote control on from the home Wi-Fi."}
    return 200, target.apply_settings(changes)


def _bounded_text(value: Any, limit: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit


async def handle_garage(
    target: GarageTarget | None,
    daemon: SettingsTarget,
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
) -> Response:
    """The two garage endpoints. Errors carry ``CommandError.status``."""
    if target is None or path not in GARAGE_PATHS:
        return 404, {"error": "Not found."}
    if method != "POST":
        return 405, {"error": "Use POST here."}
    if headers.get(GUARD_HEADER) != "1":
        return 403, {"error": "Missing the X-Snaphouse header."}
    origin = origin_of(headers)
    actor = None
    if origin == "tunnel":
        email = headers.get(ACCESS_EMAIL_HEADER, "")
        if not email.strip():
            return 403, {"error": "Sign in again to control the garage."}
        actor = target.actor_tag(email.strip())
    intent = path == GARAGE_INTENT
    parsed = _json_object(body, ("action",) if intent else ("action", "nonce"))
    if isinstance(parsed, tuple):
        return parsed
    action = parsed.get("action")
    if not _bounded_text(action, MAX_ACTION) or action not in ("open", "close"):
        return 400, {"error": "The action must be open or close."}
    try:
        if intent:
            return 200, target.gate.intent(action, origin, actor)
        nonce = parsed.get("nonce")
        if not _bounded_text(nonce, MAX_NONCE):
            return 400, {"error": "That request expired. Start again."}
        await target.gate.command(action, nonce, origin, actor)
    except CommandError as error:
        return error.status, {"error": error.message}
    return 200, daemon.publish()


_REASONS = {
    200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
    405: "Method Not Allowed", 409: "Conflict", 413: "Payload Too Large",
    423: "Locked", 429: "Too Many Requests", 503: "Service Unavailable",
    504: "Gateway Timeout",
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


def _serve_one(target: SettingsTarget, lookup: Lookup, garage: GarageTarget | None):
    async def connection(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            # The read timeout covers only reading the request.
            async with asyncio.timeout(READ_TIMEOUT_SECONDS):
                try:
                    method, path, headers, length = await _read_request(reader)
                except (ValueError, asyncio.IncompleteReadError,
                        asyncio.LimitOverrunError):
                    response = _encode(400, {"error": "Malformed request."})
                    request = None
                else:
                    if length > MAX_BODY:
                        response = _encode(413, {"error": "The request is too large."})
                        request = None
                    else:
                        body = await reader.readexactly(length) if length else b""
                        request = (method, path, headers, body)
            if request is not None:
                method, path, headers, body = request
                if path in GARAGE_PATHS:
                    code, document = await _garage_request(
                        target, garage, method, path, headers, body
                    )
                else:
                    code, document = handle(
                        target, method, path, headers, body, lookup, garage
                    )
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


async def _garage_request(
    target: SettingsTarget,
    garage: GarageTarget | None,
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
) -> Response:
    cap = garage.timeout_seconds if garage is not None else GARAGE_TIMEOUT_SECONDS
    try:
        async with asyncio.timeout(cap):
            return await handle_garage(garage, target, method, path, headers, body)
    except TimeoutError:
        return 504, {"error": "The garage did not answer in time. Check the card."}


async def start(
    target: SettingsTarget,
    host: str,
    port: int,
    lookup: Lookup = presence.neighbour_mac,
    garage: GarageTarget | None = None,
) -> asyncio.Server:
    """Listen for requests; the caller closes the returned server."""
    return await asyncio.start_server(_serve_one(target, lookup, garage), host, port)
