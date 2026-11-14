"""People added at runtime ("count this phone as home").

Added people are kept in ``state.json`` next to the climate state, never in
``local.toml``. They are tracked by Wi-Fi only and shown by name; their hardware
addresses stay in the state file and never reach the status document.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterable, Sequence

from homebody.config import ConfigError, Person
from homebody.config import _mac as _config_mac

MAX_ADDED = 8
MAX_NAME = 30
_ID = re.compile(r"added-[0-9a-f]{8}")


class PeopleError(ValueError):
    """A people request that cannot be done; ``str()`` is shown to people."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def clean_name(raw: object, taken: Iterable[str]) -> str:
    if not isinstance(raw, str):
        raise PeopleError("Enter a name.")
    name = raw.strip()
    if not name:
        raise PeopleError("Enter a name.")
    if len(name) > MAX_NAME:
        raise PeopleError(f"Names can be up to {MAX_NAME} characters.")
    if any(not ch.isprintable() for ch in name):
        raise PeopleError("That name has characters that cannot be used.")
    if name.casefold() in {t.casefold() for t in taken}:
        raise PeopleError("Someone here already has that name.")
    return name


def clean_mac(raw: object) -> str:
    try:
        mac = _config_mac(raw, "address")
    except ConfigError:
        mac = None
    if mac is None:
        raise PeopleError("That does not look like a phone address.")
    return mac


def new_person_id() -> str:
    return "added-" + secrets.token_hex(4)


def parse_saved(raw: object) -> list[Person]:
    """Added people from the state file; bad entries are skipped."""
    if not isinstance(raw, list):
        return []
    people: list[Person] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        pid, name, mac = entry.get("id"), entry.get("name"), entry.get("wifi_mac")
        if not isinstance(pid, str) or not _ID.fullmatch(pid):
            continue
        if not isinstance(name, str) or not name.strip():
            continue
        try:
            wifi = _config_mac(mac, "address")
        except ConfigError:
            continue
        if wifi is None:
            continue
        people.append(Person(name.strip(), True, wifi, None, pid))
    return people


def to_saved(people: Sequence[Person]) -> list[dict[str, str]]:
    return [
        {"id": p.id, "name": p.name, "wifi_mac": p.wifi_mac or ""} for p in people
    ]
