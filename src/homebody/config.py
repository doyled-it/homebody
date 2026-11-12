"""Read /etc/homebody/local.toml into typed settings.

The local file is the only place identifying data lives (hardware addresses,
display names). Nothing here logs an address, and validation errors name the
person's position in the file, never the value that was wrong.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("/etc/homebody/local.toml")

_MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


class ConfigError(ValueError):
    """The config file is missing or malformed."""


@dataclass(frozen=True)
class Person:
    name: str
    enabled: bool
    wifi_mac: str | None
    bluetooth_mac: str | None = None
    # An opaque slug for the API and the web page; never a name or an address.
    id: str = ""


@dataclass(frozen=True)
class Config:
    people: tuple[Person, ...] = ()
    bluetooth_adapter: str | None = None
    comfort_f: int = 72
    away_f: int = 82
    away_minutes: int = 20

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
    bluetooth = _mac(raw.get("bluetooth_mac"), f"{where} bluetooth_mac")
    if wifi is None and bluetooth is None:
        raise ConfigError(f"{where}: needs a wifi_mac or a bluetooth_mac")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError(f"{where}: enabled must be true or false")
    pid = raw.get("id", f"person-{index + 1}")
    if not isinstance(pid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", pid):
        raise ConfigError(f"{where}: id must be a short lowercase slug")
    return Person(name.strip(), enabled, wifi, bluetooth, pid)


def _int(value: Any, key: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"climate {key}: must be a whole number")
    return value


def parse(text: str) -> Config:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML: {exc}") from None
    people = tuple(_person(p, i) for i, p in enumerate(data.get("person", [])))
    adapter = _mac(data.get("bluetooth", {}).get("adapter"), "bluetooth adapter")
    climate = data.get("climate", {})
    return Config(
        people=people,
        bluetooth_adapter=adapter,
        comfort_f=_int(climate.get("comfort_f"), "comfort_f", 72),
        away_f=_int(climate.get("away_f"), "away_f", 82),
        away_minutes=_int(climate.get("away_minutes"), "away_minutes", 20),
    )


def load(path: Path | str = DEFAULT_CONFIG_PATH) -> Config:
    try:
        text = Path(path).read_text()
    except OSError as exc:
        raise ConfigError(f"cannot read config: {exc.strerror}") from None
    return parse(text)
