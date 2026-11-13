"""The status document (homebody.json) and its atomic write.

The hub's nginx serves the file; the room picker reads it. Every key is always
present and is null when unknown. People listed are the enabled ones only, by
display name (the ids stay internal).
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
) -> dict[str, Any]:
    return {
        "version": VERSION,
        "updated": iso(now),
        "enabled": enabled,
        "occupied": occupied,
        "people": [
            {
                "name": p.name,
                "state": p.state,
                "last_seen": (
                    None
                    if p.seconds_since_seen is None
                    else iso(now - p.seconds_since_seen)
                ),
                "via": p.signal,
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
    }


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
