"""Tests for the status document and its atomic write (src/homebody/status.py)."""

from __future__ import annotations

import json
import re
import stat

from homebody import status
from homebody.presence import PersonStatus

KEYS = {
    "version", "updated", "enabled", "occupied", "people", "thermostat",
    "override", "last_change", "settings",
}
SETTINGS = {"comfort_f": 72, "away_f": 82, "away_minutes": 20}


def doc(**over):
    args = dict(
        now=1_800_000_000.0,
        enabled=True,
        occupied=None,
        people=[],
        reachable=None,
        mode=None,
        setpoint_f=None,
        temperature_f=None,
        override=False,
        last_change=None,
        settings=SETTINGS,
    )
    args.update(over)
    return status.build_document(**args)


def test_everything_unknown_still_has_every_key():
    d = doc()
    assert set(d) == KEYS
    assert d["version"] == 1
    assert d["occupied"] is None
    assert d["people"] == []
    assert d["thermostat"] == {
        "reachable": None, "mode": None, "setpoint_f": None, "temperature_f": None,
    }
    assert d["last_change"] is None
    assert d["settings"] == SETTINGS


def test_timestamps_are_utc_whole_seconds_with_z():
    assert doc(now=1_800_000_000.9)["updated"] == "2027-01-15T08:00:00Z"
    assert status.iso(1_800_000_000.9) == "2027-01-15T08:00:00Z"


def test_people_carry_state_last_seen_and_signal():
    now = 1_800_000_000.0
    d = doc(
        now=now,
        occupied=True,
        people=[
            PersonStatus("a", "Person A", "home", "wifi", 8.0),
            PersonStatus("b", "Person B", "unknown", None, None),
        ],
    )
    assert d["people"] == [
        {"id": "a", "name": "Person A", "state": "home",
         "last_seen": "2027-01-15T07:59:52Z", "via": "wifi", "removable": False},
        {"id": "b", "name": "Person B", "state": "unknown", "last_seen": None,
         "via": None, "removable": False},
    ]


def test_status_document_has_no_addresses():
    d = doc(people=[PersonStatus("added-0000000a", "Sam", "home", "wifi", 8.0, True)])
    assert d["people"][0]["removable"] is True
    assert not re.search(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}", json.dumps(d))


def test_thermostat_and_last_change_shape():
    d = doc(
        reachable=True, mode="cool", setpoint_f=72.04, temperature_f=74.26,
        override=True,
        last_change={"at": 1_799_999_000.0, "setpoint_f": 72.0, "reason": "home"},
    )
    assert d["thermostat"] == {
        "reachable": True, "mode": "cool", "setpoint_f": 72.0, "temperature_f": 74.3,
    }
    assert d["override"] is True
    assert d["last_change"] == {
        "at": "2027-01-15T07:43:20Z", "setpoint_f": 72.0, "reason": "home",
    }


def test_write_is_atomic_mode_0644_and_leaves_no_temp(tmp_path):
    writer = status.StatusWriter(tmp_path / "pub")
    writer.write(doc())
    target = tmp_path / "pub" / "homebody.json"
    assert json.loads(target.read_text())["version"] == 1
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert [p.name for p in (tmp_path / "pub").iterdir()] == ["homebody.json"]


def test_a_failed_write_keeps_the_old_file_whole(tmp_path, monkeypatch):
    writer = status.StatusWriter(tmp_path)
    writer.write(doc(enabled=True))
    seen: list[str] = []
    real = status.os.replace

    def boom(src, dst):
        seen.append(src)
        raise OSError("disk")

    monkeypatch.setattr(status.os, "replace", boom)
    try:
        writer.write(doc(enabled=False))
    except OSError:
        pass
    monkeypatch.setattr(status.os, "replace", real)
    # The temp file lived in the same directory as the target, and is cleaned up.
    assert seen and seen[0].startswith(str(tmp_path))
    assert json.loads((tmp_path / "homebody.json").read_text())["enabled"] is True
    assert [p.name for p in tmp_path.iterdir()] == ["homebody.json"]
