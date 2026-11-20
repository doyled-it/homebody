"""Read /etc/homebody/local.toml into typed settings.

The local file is the only place identifying data lives (hardware addresses,
display names). Nothing here logs an address, and validation errors name the
person's position in the file, never the value that was wrong.
"""

from __future__ import annotations

import logging
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from homebody.garage_link import DEFAULT_ENTITY_NAMES
from homebody.matter import DEFAULT_MATTER_URL
from homebody.notify import NtfyConfig

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("/etc/homebody/local.toml")
DEFAULT_STATE_DIR = Path("/var/lib/homebody/state")
DEFAULT_STATUS_DIR = Path("/var/lib/snaphouse/public/homebody")
DEFAULT_API_PORT = 8783

# Garage setting ranges, shared by the config parser, the daemon and the API.
GARAGE_LEFT_OPEN_RANGE = (0, 240)
GARAGE_GRACE_RANGE = (0, 15)
GARAGE_CLOSE_WAIT_MAX = 60
# Headroom over the confirm and note waits for the API's own handler cap.
GARAGE_TIMEOUT_SLACK = 10.0

_MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


class ConfigError(ValueError):
    """The config file is missing or malformed."""


@dataclass(frozen=True)
class Person:
    name: str
    enabled: bool
    wifi_mac: str | None
    # An opaque slug for the API and the web page; never a name or an address.
    id: str = ""


@dataclass(frozen=True)
class GarageConfig:
    host: str
    port: int = 6053
    noise_psk: str | None = None
    allow_plaintext: bool = False
    page_url: str | None = None
    entity_names: Mapping[str, str] = field(
        default_factory=lambda: dict(DEFAULT_ENTITY_NAMES)
    )
    ntfy: NtfyConfig | None = None
    actors: Mapping[str, str] = field(default_factory=dict)
    close_wait: float = 30.0
    open_wait: float = 10.0
    note_wait: float = 5.0
    left_open_minutes: int = 15
    away_open_grace_minutes: int = 5

    @property
    def timeout_seconds(self) -> float:
        """The cap on one garage API request: the longest wait plus headroom."""
        return self.close_wait + self.note_wait + GARAGE_TIMEOUT_SLACK


@dataclass(frozen=True)
class Config:
    people: tuple[Person, ...] = ()
    comfort_f: int = 72
    away_f: int = 82
    away_minutes: int = 20
    matter_url: str = DEFAULT_MATTER_URL
    # 0 means "not set": the daemon finds the first node with a thermostat.
    matter_node_id: int = 0
    state_dir: Path = DEFAULT_STATE_DIR
    # Where homebody.json is written (a directory the service user owns).
    status_dir: Path = DEFAULT_STATUS_DIR
    # The settings API listens on 127.0.0.1 at this port.
    api_port: int = DEFAULT_API_PORT
    # None when [garage] is absent or invalid; garage_error says why (a sentence
    # naming the field, never a value).
    garage: GarageConfig | None = None
    garage_error: str | None = None

    @property
    def enabled_people(self) -> tuple[Person, ...]:
        return tuple(p for p in self.people if p.enabled)


def _mac(value: Any, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _MAC.match(value.strip().lower()):
        raise ConfigError(f"{where}: not a colon-separated hardware address")
    return value.strip().lower()


def _person(raw: dict[str, Any], index: int) -> Person:
    where = f"person #{index + 1}"
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"{where}: a name is required")
    wifi = _mac(raw.get("wifi_mac"), f"{where} wifi_mac")
    if wifi is None:
        hint = (
            " (bluetooth_mac is no longer used for presence)"
            if "bluetooth_mac" in raw
            else ""
        )
        raise ConfigError(f"{where}: needs a wifi_mac{hint}")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError(f"{where}: enabled must be true or false")
    pid = raw.get("id", f"person-{index + 1}")
    if not isinstance(pid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", pid):
        raise ConfigError(f"{where}: id must be a short lowercase slug")
    return Person(name.strip(), enabled, wifi, pid)


def _int(value: Any, key: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"climate {key}: must be a whole number")
    return value


def _text(table: Mapping[str, Any], key: str, where: str, *, required=False):
    value = table.get(key)
    if value is None:
        if required:
            raise ConfigError(f"{where} {key}: is required")
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where} {key}: must be a non-empty string")
    return value.strip()


def _ranged(table: Mapping[str, Any], key: str, default, low, high, *, whole=True):
    value = table.get(key)
    if value is None:
        return default
    bad = isinstance(value, bool) or not isinstance(value, int | float)
    if bad or (whole and not isinstance(value, int)):
        raise ConfigError(f"garage {key}: must be a number")
    if not low <= value <= high:
        raise ConfigError(f"garage {key}: must be between {low} and {high}")
    return value if whole else float(value)


def _table(raw: Any, name: str) -> Mapping[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{name}: must be a table")
    return raw


def _garage(raw: Any) -> GarageConfig:
    table = _table(raw, "garage")
    host = _text(table, "host", "garage", required=True)
    psk = _text(table, "noise_psk", "garage")
    plaintext = table.get("allow_plaintext", False)
    if not isinstance(plaintext, bool):
        raise ConfigError("garage allow_plaintext: must be true or false")
    if psk is None and not plaintext:
        raise ConfigError(
            "garage noise_psk: an encryption key is required "
            "(or set allow_plaintext = true)"
        )
    page_url = _text(table, "page_url", "garage")
    names = dict(DEFAULT_ENTITY_NAMES)
    given = _table(table.get("entity_names"), "garage entity_names")
    for role, name in given.items():
        if role not in DEFAULT_ENTITY_NAMES or not isinstance(name, str) or not name:
            raise ConfigError("garage entity_names: unknown role or bad name")
        names[role] = name
    ntfy = None
    if table.get("ntfy") is not None:
        n = _table(table.get("ntfy"), "garage ntfy")
        ntfy = NtfyConfig(
            url=_text(n, "url", "garage ntfy", required=True),
            topic=_text(n, "topic", "garage ntfy", required=True),
            token=_text(n, "token", "garage ntfy"),
            click_url=None if page_url is None else page_url + "#garage",
        )
    actors = _table(table.get("actors"), "garage actors")
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in actors.items()):
        raise ConfigError("garage actors: labels must be strings")
    if psk is None:
        log.warning("garage: the unencrypted board link is allowed by config")
    return GarageConfig(
        host=host,
        port=_ranged(table, "port", 6053, 1, 65535),
        noise_psk=psk,
        allow_plaintext=plaintext,
        page_url=page_url,
        entity_names=names,
        ntfy=ntfy,
        actors=dict(actors),
        close_wait=_ranged(
            table, "close_wait_seconds", 30.0, 1, GARAGE_CLOSE_WAIT_MAX, whole=False
        ),
        open_wait=_ranged(table, "open_wait_seconds", 10.0, 1, 30, whole=False),
        note_wait=_ranged(table, "note_wait_seconds", 5.0, 0, 15, whole=False),
        left_open_minutes=_ranged(
            table, "left_open_minutes", 15, *GARAGE_LEFT_OPEN_RANGE
        ),
        away_open_grace_minutes=_ranged(
            table, "away_open_grace_minutes", 5, *GARAGE_GRACE_RANGE
        ),
    )


def parse(text: str) -> Config:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML: {exc}") from None
    people = tuple(_person(p, i) for i, p in enumerate(data.get("person", [])))
    # Bluetooth presence was removed (issue #17). Old configs still load: the
    # keys are ignored, with one warning per key (never the value).
    if any("bluetooth_mac" in p for p in data.get("person", [])):
        log.warning("person bluetooth_mac is no longer used and is ignored")
    if "adapter" in data.get("bluetooth", {}):
        log.warning("[bluetooth] adapter is no longer used and is ignored")
    climate = data.get("climate", {})
    matter = data.get("matter", {})
    url = matter.get("url", DEFAULT_MATTER_URL)
    if not isinstance(url, str) or not url.startswith(("ws://", "wss://")):
        raise ConfigError("matter url: must be a ws:// or wss:// address")
    state_dir = data.get("daemon", {}).get("state_dir", str(DEFAULT_STATE_DIR))
    if not isinstance(state_dir, str) or not state_dir:
        raise ConfigError("daemon state_dir: must be a path")
    status_dir = data.get("status", {}).get("dir", str(DEFAULT_STATUS_DIR))
    if not isinstance(status_dir, str) or not status_dir:
        raise ConfigError("status dir: must be a path")
    api_port = _int(data.get("api", {}).get("port"), "api port", DEFAULT_API_PORT)
    if not 1 <= api_port <= 65535:
        raise ConfigError("api port: must be between 1 and 65535")
    garage, garage_error = None, None
    if "garage" in data:
        try:
            garage = _garage(data["garage"])
        except ConfigError as exc:
            garage_error = f"{exc}."
            log.warning("garage disabled: %s", garage_error)
    return Config(
        garage=garage,
        garage_error=garage_error,
        people=people,
        comfort_f=_int(climate.get("comfort_f"), "comfort_f", 72),
        away_f=_int(climate.get("away_f"), "away_f", 82),
        away_minutes=_int(climate.get("away_minutes"), "away_minutes", 20),
        matter_url=url,
        matter_node_id=_int(matter.get("node_id"), "matter node_id", 0),
        state_dir=Path(state_dir),
        status_dir=Path(status_dir),
        api_port=api_port,
    )


def load(path: Path | str = DEFAULT_CONFIG_PATH) -> Config:
    try:
        text = Path(path).read_text()
    except OSError as exc:
        raise ConfigError(f"cannot read config: {exc.strerror}") from None
    return parse(text)
