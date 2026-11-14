"""Tests for the status document and its atomic write (src/homebody/status.py)."""

from __future__ import annotations

import json
import re
import stat

from homebody import status
from homebody.presence import PersonStatus

KEYS = {
    "version", "updated", "enabled", "occupied", "people", "thermostat",
    "override", "last_change", "settings", "garage",
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


# The garage key

from pathlib import Path  # noqa: E402

from garage_fakes import FakeGarageLink  # noqa: E402
from homebody.garage import GarageModel, GarageSettings  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "garage_status_sample.json"
T_SINCE = 1_794_678_003.0  # 2026-11-14T17:40:03Z
T_SEEN = T_SINCE + 22 * 60 + 8  # 2026-11-14T18:02:11Z


class Clk:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def garage_model(shape="position", *, connected=True, push=True):
    clock = Clk(T_SINCE - 600)
    model = GarageModel(clock)
    link = FakeGarageLink(shape)
    link.bind(model.state_received, model.link_changed)
    if connected:
        link.connect()
        if push:
            link.push(operation="idle", position=1.0)  # open, then it closes
            clock.now = T_SINCE
            link.push(operation="idle", position=0.0, obstruction=False,
                      lockout=False, light=False)
    clock.now = T_SEEN
    return model, link, clock


def test_document_has_garage_null_by_default():
    document = doc()
    assert "garage" in document and document["garage"] is None
    assert document["version"] == 1
    assert doc(garage={"x": 1})["garage"] == {"x": 1}


def test_build_garage_matches_the_sample_fixture():
    model, link, clock = garage_model()
    link.push(obstruction=False)  # a later push moves last_seen
    recent = [{"at": "2026-11-14T17:40:03Z", "event": "closed", "origin": "home",
               "actor": None, "result": "ok"}]
    built = status.build_garage(model, GarageSettings(15, 5, True, True), recent, {},
                                clock())
    assert built == json.loads(FIXTURE.read_text())["garage"]


def test_sample_fixture_is_the_documented_shape():
    sample = json.loads(FIXTURE.read_text())["garage"]
    assert set(sample) == {"link", "door", "obstruction", "lockout", "light",
                           "settings", "recent"}
    assert set(sample["door"]) == {"state", "since", "since_approximate", "position",
                                   "last_known_state"}
    assert set(sample["recent"][0]) == {"at", "event", "origin", "who"}


def test_state_unknown_when_link_down_never_closed():
    model, link, clock = garage_model()
    link.drop()
    built = status.build_garage(model, GarageSettings(), [], {}, clock())
    assert built["link"]["connected"] is False
    assert built["link"]["last_seen"] == status.iso(T_SINCE)
    assert built["door"]["state"] == "unknown"
    assert built["door"]["last_known_state"] == "closed"
    assert built["door"]["position"] is None
    assert built["door"]["since"] is None
    assert (built["obstruction"], built["lockout"], built["light"]) == (None,) * 3


def test_state_unknown_before_the_first_door_push_after_connect():
    model, _, clock = garage_model(push=False)
    built = status.build_garage(model, GarageSettings(), [], {}, clock())
    assert built["link"]["connected"] is True
    assert built["door"]["state"] == "unknown"
    assert built["link"]["last_seen"] is None


def test_position_null_for_binary_cover():
    model, _, clock = garage_model("binary")
    built = status.build_garage(model, GarageSettings(), [], {}, clock())
    assert built["door"]["state"] == "closed"
    assert built["door"]["position"] is None


def test_position_is_a_percentage_for_a_position_cover():
    model, link, clock = garage_model()
    link.push(operation="idle", position=0.4)
    built = status.build_garage(model, GarageSettings(), [], {}, clock())
    assert built["door"]["position"] == 40


def test_who_uses_label_else_tag_never_email():
    model, _, clock = garage_model()
    recent = [
        {"at": "2026-11-14T17:00:00Z", "event": "opened", "origin": "tunnel",
         "actor": "ab12", "result": "ok"},
        {"at": "2026-11-14T17:01:00Z", "event": "closed", "origin": "tunnel",
         "actor": "cd34", "result": "ok"},
        {"at": "2026-11-14T17:02:00Z", "event": "opened", "origin": "home",
         "actor": "ab12", "result": "ok"},
        {"at": "2026-11-14T17:03:00Z", "event": "closed", "origin": "opener",
         "actor": None, "result": "ok"},
        {"at": "2026-11-14T17:04:00Z", "event": "opened", "origin": "tunnel",
         "actor": "person@example.invalid", "result": "ok"},
    ]
    built = status.build_garage(model, GarageSettings(), recent,
                                {"ab12": "Person A"}, clock())
    assert [r["who"] for r in built["recent"]] == [
        "Person A", "cd34", None, None, None]
    assert "@" not in json.dumps(built)
    assert [r["origin"] for r in built["recent"]][3] == "opener"


def test_recent_keeps_the_last_ten_newest_last():
    model, _, clock = garage_model()
    recent = [{"at": f"2026-11-14T17:{i:02d}:00Z", "event": "opened",
               "origin": "home", "actor": None, "result": "ok"} for i in range(14)]
    built = status.build_garage(model, GarageSettings(), recent, {}, clock())
    assert len(built["recent"]) == 10
    assert built["recent"][-1]["at"] == "2026-11-14T17:13:00Z"
