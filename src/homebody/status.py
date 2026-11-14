"""The status document (homebody.json) and its atomic write.

The hub's nginx serves the file; the room picker reads it. Every key is always
present and is null when unknown. People listed are the enabled ones only, by
display name (the ids stay internal).
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from homebody.garage import GarageModel, GarageSettings
from homebody.presence import PersonStatus

VERSION = 1
FILE_NAME = "homebody.json"


def iso(timestamp: float) -> str:
    """UTC ISO 8601 with a Z, whole seconds."""
    return datetime.fromtimestamp(int(timestamp), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def build_document(
    *,
    now: float,
    enabled: bool,
    occupied: bool | None,
    people: Sequence[PersonStatus],
    reachable: bool | None,
    mode: str | None,
    setpoint_f: float | None,
    temperature_f: float | None,
    override: bool,
    last_change: dict[str, Any] | None,
    settings: dict[str, Any],
    garage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "version": VERSION,
        "updated": iso(now),
        "enabled": enabled,
        "occupied": occupied,
        "people": [
            {
                "id": p.id,
                "name": p.name,
                "state": p.state,
                "last_seen": (
                    None
                    if p.seconds_since_seen is None
                    else iso(now - p.seconds_since_seen)
                ),
                "via": p.signal,
                "removable": p.removable,
            }
            for p in people
        ],
        "thermostat": {
            "reachable": reachable,
            "mode": mode,
            "setpoint_f": _round(setpoint_f),
            "temperature_f": _round(temperature_f),
        },
        "override": override,
        "last_change": (
            None
            if last_change is None
            else {
                "at": iso(last_change["at"]),
                "setpoint_f": _round(last_change["setpoint_f"]),
                "reason": last_change["reason"],
            }
        ),
        "settings": dict(settings),
        "garage": garage,
    }


RECENT_LIMIT = 10


def _past(timestamp: float | None, now: float) -> str | None:
    """A stored time as UTC text, never later than ``now`` (clock steps)."""
    return None if timestamp is None else iso(min(timestamp, now))


def build_garage(
    model: GarageModel,
    settings: GarageSettings,
    recent: Sequence[Mapping[str, Any]],
    labels: Mapping[str, str],
    now: float,
) -> dict[str, Any]:
    """The ``garage`` key of the document.

    ``recent`` holds audit door-change entries, oldest first. ``who`` is a label
    from ``labels`` for a tunnel command's tag, else the bare tag; never an email.
    """
    state = model.state
    known = state != "unknown"
    return {
        "link": {
            "connected": model.connected,
            "last_seen": _past(model.last_seen, now),
        },
        "door": {
            "state": state,
            "since": _past(model.since, now) if known else None,
            "since_approximate": model.since_approximate if known else False,
            "position": model.position_percent if known else None,
            "last_known_state": model.last_known_state,
        },
        "obstruction": model.obstruction,
        "lockout": model.lockout,
        "light": model.light,
        "settings": {
            "left_open_minutes": settings.left_open_minutes,
            "away_open_grace_minutes": settings.away_open_grace_minutes,
            "alerts": settings.alerts,
            "remote": settings.remote,
        },
        "recent": [
            {
                "at": entry.get("at"),
                "event": entry.get("event"),
                "origin": entry.get("origin"),
                "who": _who(entry, labels),
            }
            for entry in list(recent)[-RECENT_LIMIT:]
        ],
    }


def _who(entry: Mapping[str, Any], labels: Mapping[str, str]) -> str | None:
    tag = entry.get("actor")
    if entry.get("origin") != "tunnel" or not isinstance(tag, str) or "@" in tag:
        return None
    return labels.get(tag, tag)


class StatusWriter:
    """Writes homebody.json: temp file in the same directory, fsync, rename."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self.path = directory / FILE_NAME

    def write(self, document: dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._dir, prefix=".status-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(document, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o644)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
