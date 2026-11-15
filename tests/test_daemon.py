"""Tests for the service loop (src/homebody/daemon.py).

The loop runs against the in-memory FakeController, fake probes, and a fake
clock; a test calls ``tick()`` and advances the clock by hand, so nothing sleeps
and nothing touches the network. Times are seconds on the fake clock.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from fakes import FakeConnectionContext, FakeController
from homebody import matter
from homebody.config import Config, Person
from homebody.daemon import STATE_FILE, Daemon
from homebody.people import PeopleError
from homebody.presence import PresenceTracker

MIN = 60.0
WINDOW = 20 * MIN
COOL = int(matter.SystemMode.COOL)
COMFORT = 2222
AWAY = 2778
_CLUSTER = matter.THERMOSTAT_CLUSTER_ID
SETPOINT_PATH = f"1/{_CLUSTER}/{matter.ATTR_OCCUPIED_COOLING_SETPOINT}"
MODE_PATH = f"1/{_CLUSTER}/{matter.ATTR_SYSTEM_MODE}"

A = Person("Person A", True, "aa:00:00:00:00:01", None, "person-a")
B = Person("Person B", False, "aa:00:00:00:00:02", None, "person-b")


class Clock:
    def __init__(self, now: float = 100_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Wifi:
    def __init__(self) -> None:
        self.home: set[str] = set()
        self.probed: list[str] = []

    def seen(self, mac: str) -> bool:
        self.probed.append(mac)
        return mac in self.home


class NoBluetooth:
    def ping(self, mac: str) -> bool:
        return False


class FlakyController(FakeController):
    """A controller that can be switched off, like a dead service or radio."""

    def __init__(self) -> None:
        super().__init__()
        self.down = False

    async def command(self, command, args=None):
        if self.down:
            raise matter.MatterConnectionError("controller unreachable")
        return await super().command(command, args)

    def writes(self) -> list[dict]:
        return [a for c, a in self.calls if c == "write_attribute"]


class Rig:
    def __init__(self, tmp_path: Path, people=(A, B), **config_kwargs) -> None:
        self.clock = Clock()
        self.wifi = Wifi()
        self.controller = FlakyController()
        self.controller.add_thermostat(cooling_c100=AWAY)
        self.tmp_path = tmp_path
        self.people = people
        self.config = Config(people=tuple(people), state_dir=tmp_path, **config_kwargs)
        self.daemon = self.new_daemon()

    def new_daemon(self) -> Daemon:
        presence = PresenceTracker(
            self.config.people,
            wifi=self.wifi,
            bluetooth=NoBluetooth(),
            clock=self.clock,
            away_window=self.config.away_minutes * MIN,
        )
        return Daemon(
            self.config,
            presence=presence,
            connect=lambda: FakeConnectionContext(self.controller),
            clock=self.clock,
        )

    def tick(self, advance: float = 15.0) -> None:
        self.clock.advance(advance)
        asyncio.run(self.daemon.tick())

    def run_for(self, seconds: float, step: float = 15.0) -> None:
        end = self.clock.now + seconds
        while self.clock.now < end:
            self.tick(step)

    def setpoint(self) -> int:
        return self.controller.nodes[1]["attributes"][SETPOINT_PATH]

    def saved(self) -> dict:
        return json.loads((self.tmp_path / STATE_FILE).read_text())


def test_review_focus_5_boot_is_unknown_and_changes_nothing(tmp_path):
    rig = Rig(tmp_path)
    rig.run_for(10 * MIN)  # nobody seen yet, less than a full window
    assert rig.controller.writes() == []
    assert rig.setpoint() == AWAY


def test_review_focus_5_a_full_unseen_window_then_goes_away(tmp_path):
    rig = Rig(tmp_path)
    rig.controller.nodes[1]["attributes"][SETPOINT_PATH] = COMFORT
    rig.run_for(WINDOW - 2 * MIN)
    assert rig.controller.writes() == []  # still unknown, not "away"
    rig.run_for(4 * MIN)
    assert rig.setpoint() == AWAY  # the first real state is "away"
    state = rig.saved()["climate"]
    assert state["occupied"] is False


def test_first_sighting_after_boot_sets_comfort(tmp_path):
    rig = Rig(tmp_path)
    rig.wifi.home = {A.wifi_mac}
    rig.run_for(2 * MIN)
    assert rig.setpoint() == COMFORT
    assert rig.controller.nodes[1]["attributes"][MODE_PATH] == COOL


def test_review_focus_4_disabled_person_is_never_probed_or_counted(tmp_path):
    rig = Rig(tmp_path)
    rig.wifi.home = {B.wifi_mac}  # only the disabled person is "home"
    rig.run_for(WINDOW + 2 * MIN)
    assert B.wifi_mac not in rig.wifi.probed
    assert rig.saved()["climate"]["occupied"] is False


def test_one_command_per_transition_and_none_while_steady(tmp_path):
    rig = Rig(tmp_path)
    rig.wifi.home = {A.wifi_mac}
    rig.run_for(30 * MIN)
    # One mode write and one setpoint write for the single transition.
    assert len(rig.controller.writes()) == 2


def test_away_after_everyone_leaves(tmp_path):
    rig = Rig(tmp_path)
    rig.wifi.home = {A.wifi_mac}
    rig.run_for(5 * MIN)
    assert rig.setpoint() == COMFORT
    rig.wifi.home = set()
    rig.run_for(WINDOW + 3 * MIN)
    assert rig.setpoint() == AWAY


def test_review_focus_3_a_transition_takes_over_a_manual_change(tmp_path):
    rig = Rig(tmp_path)
    rig.wifi.home = {A.wifi_mac}
    rig.run_for(5 * MIN)
    # A person turns the Nest to 75 F (2389) by hand.
    rig.controller.nodes[1]["attributes"][SETPOINT_PATH] = 2389
    rig.wifi.home = set()
    rig.run_for(WINDOW + 5 * MIN)  # everyone leaves: the transition takes over
    assert rig.setpoint() == AWAY
    assert rig.saved()["climate"]["override"] is False


def test_review_focus_3_manual_change_holds_a_settings_change_until_the_next_transition(
    tmp_path,
):
    rig = Rig(tmp_path)
    rig.wifi.home = {A.wifi_mac}
    rig.run_for(5 * MIN)
    rig.controller.nodes[1]["attributes"][SETPOINT_PATH] = 2389
    writes_before = len(rig.controller.writes())
    rig.daemon.apply_settings({"comfort_f": 73})  # owes a command
    rig.run_for(5 * MIN)
    assert rig.setpoint() == 2389
    assert len(rig.controller.writes()) == writes_before
    assert rig.saved()["climate"]["override"] is True
    # Everyone leaves: the next transition, automation resumes.
    rig.wifi.home = set()
    rig.run_for(WINDOW + 5 * MIN)
    assert rig.setpoint() == AWAY
    assert rig.saved()["climate"]["override"] is False


def test_review_focus_2_outage_keeps_presence_never_crashes_and_latest_wins(tmp_path):
    rig = Rig(tmp_path)
    rig.wifi.home = {A.wifi_mac}
    rig.run_for(5 * MIN)
    assert rig.setpoint() == COMFORT
    writes_before = len(rig.controller.writes())

    rig.controller.down = True
    rig.wifi.home = set()
    rig.run_for(WINDOW + 5 * MIN)  # away transition happens during the outage
    rig.wifi.home = {A.wifi_mac}
    rig.run_for(2 * MIN)  # home again, still down
    rig.wifi.home = set()
    rig.run_for(WINDOW + 5 * MIN)  # and away again
    assert len(rig.controller.writes()) == writes_before  # nothing got through
    # Presence kept tracking the whole time.
    assert rig.daemon.occupied() is False

    rig.controller.down = False
    rig.run_for(20 * MIN)  # backoff elapses, the daemon reconnects
    assert rig.setpoint() == AWAY
    # Only the latest state was sent: one mode write and one setpoint write.
    assert len(rig.controller.writes()) == writes_before + 2


def test_unreachable_controller_is_retried_with_growing_backoff(tmp_path):
    rig = Rig(tmp_path)
    rig.wifi.home = {A.wifi_mac}
    rig.controller.down = True
    rig.run_for(30 * MIN, step=5.0)
    # 360 ticks of 5 s, but backoff (1, 2, 4, 8, 15 min...) allows only a few.
    assert 2 <= rig.daemon.failure_count <= 6


def test_commands_are_paced_to_one_a_minute_even_across_a_restart(tmp_path):
    rig = Rig(tmp_path)
    start = rig.clock.now
    # The previous process sent a command 10 seconds ago, then was restarted.
    (tmp_path / STATE_FILE).write_text(
        json.dumps(
            {
                "climate": {
                    "occupied": False,
                    "mode": COOL,
                    "setpoint_c100": AWAY,
                    "last_change": start - 10,
                }
            }
        )
    )
    rig.daemon = rig.new_daemon()
    rig.wifi.home = {A.wifi_mac}  # a transition to occupied is owed at once
    write_times: list[float] = []
    real = rig.controller.command

    async def spy(command, args=None):
        if command == "write_attribute":
            write_times.append(rig.clock.now)
        return await real(command, args)

    rig.controller.command = spy
    rig.run_for(2 * MIN, step=5.0)
    assert write_times, "the transition was never acted on"
    assert min(write_times) - (start - 10) >= MIN
    assert rig.setpoint() == COMFORT


def test_state_survives_a_restart(tmp_path):
    rig = Rig(tmp_path)
    rig.wifi.home = {A.wifi_mac}
    rig.run_for(5 * MIN)
    assert rig.setpoint() == COMFORT
    writes = len(rig.controller.writes())

    rig.daemon = rig.new_daemon()  # a fresh process reads the state file
    assert rig.daemon.climate.state.occupied is True
    assert rig.daemon.climate.state.setpoint_c100 == COMFORT
    rig.run_for(10 * MIN)
    assert len(rig.controller.writes()) == writes  # no repeat command


def test_override_survives_a_restart(tmp_path):
    rig = Rig(tmp_path)
    rig.wifi.home = {A.wifi_mac}
    rig.run_for(5 * MIN)
    rig.controller.nodes[1]["attributes"][SETPOINT_PATH] = 2389
    rig.daemon.apply_settings({"comfort_f": 73})
    rig.run_for(5 * MIN)
    assert rig.saved()["climate"]["override"] is True

    rig.daemon = rig.new_daemon()
    assert rig.daemon.climate.state.override is True


def test_a_corrupt_state_file_starts_fresh(tmp_path):
    (tmp_path / STATE_FILE).write_text("{not json")
    rig = Rig(tmp_path)
    assert rig.daemon.climate.state.occupied is None


def test_state_file_is_written_atomically_and_leaves_no_temp_files(tmp_path):
    rig = Rig(tmp_path)
    rig.wifi.home = {A.wifi_mac}
    rig.run_for(2 * MIN)
    assert sorted(p.name for p in tmp_path.iterdir()) == [STATE_FILE]


def test_runtime_enable_flag_is_persisted_and_applied_on_restart(tmp_path):
    rig = Rig(tmp_path)
    rig.wifi.home = {B.wifi_mac}
    assert rig.daemon.set_enabled("person-b", True) is True
    assert rig.daemon.set_enabled("nobody", True) is False
    assert rig.saved()["enabled"] == {"person-b": True}

    rig.daemon = rig.new_daemon()
    rig.run_for(2 * MIN)
    assert B.wifi_mac in rig.wifi.probed
    assert rig.setpoint() == COMFORT


def test_no_thermostat_commissioned_is_a_retry_not_a_crash(tmp_path):
    rig = Rig(tmp_path)
    rig.controller.nodes.clear()
    rig.wifi.home = {A.wifi_mac}
    rig.run_for(5 * MIN)
    rig.controller.add_thermostat(cooling_c100=AWAY)
    rig.run_for(20 * MIN)
    assert rig.setpoint() == COMFORT


# Added people (the "count this phone as home" feature).
SAM_MAC = "02:00:00:00:00:0a"


def test_add_person_counts_and_saves(tmp_path):
    rig = Rig(tmp_path)
    doc = rig.daemon.add_person(" Sam ", SAM_MAC.upper())
    added = [p for p in doc["people"] if p["removable"]]
    assert [p["name"] for p in added] == ["Sam"]
    assert rig.daemon.counted_as(SAM_MAC) == "Sam"
    assert rig.saved()["people"] == [
        {"id": added[0]["id"], "name": "Sam", "wifi_mac": SAM_MAC}
    ]


def test_add_person_twice_is_409(tmp_path):
    rig = Rig(tmp_path)
    rig.daemon.add_person("Sam", SAM_MAC)
    with pytest.raises(PeopleError) as e:
        rig.daemon.add_person("Other", SAM_MAC)
    assert e.value.status == 409
    assert str(e.value) == "This phone is already counted as Sam."


def test_ninth_added_person_is_400(tmp_path):
    rig = Rig(tmp_path)
    for i in range(8):
        rig.daemon.add_person(f"Guest {i}", f"02:00:00:00:01:{i:02x}")
    with pytest.raises(PeopleError) as e:
        rig.daemon.add_person("Guest 9", "02:00:00:00:01:ff")
    assert e.value.status == 400
    assert str(e.value) == "Up to 8 people can be added here."


def test_remove_configured_person_is_400(tmp_path):
    rig = Rig(tmp_path)
    with pytest.raises(PeopleError) as e:
        rig.daemon.remove_person("person-a")
    assert e.value.status == 400
    assert str(e.value) == "That person cannot be removed here."
    with pytest.raises(PeopleError):
        rig.daemon.remove_person(5)


def test_added_people_survive_restart(tmp_path):
    rig = Rig(tmp_path)
    rig.daemon.add_person("Sam", SAM_MAC)
    doc = rig.daemon.add_person("Jo", "02:00:00:00:00:0b")
    sam = next(p for p in doc["people"] if p["name"] == "Sam")
    rig.daemon.remove_person(sam["id"])
    rig.daemon = rig.new_daemon()
    people = rig.daemon.status_document()["people"]
    names = [p["name"] for p in people if p["removable"]]
    assert names == ["Jo"]


def test_saved_people_duplicating_a_configured_phone_or_an_id_are_not_tracked_twice(
    tmp_path,
):
    rig = Rig(tmp_path)
    state = {
        "people": [
            {"id": "added-00000001", "name": "Dup", "wifi_mac": A.wifi_mac},
            {"id": "added-00000002", "name": "Sam", "wifi_mac": SAM_MAC},
            {"id": "added-00000002", "name": "Jo", "wifi_mac": "02:00:00:00:00:0b"},
        ]
    }
    (tmp_path / STATE_FILE).write_text(json.dumps(state))
    rig.daemon = rig.new_daemon()
    people = rig.daemon.status_document()["people"]
    added = [p for p in people if p["removable"]]
    assert [p["name"] for p in added] == ["Sam"]
    assert [p["id"] for p in people] == ["person-a", "added-00000002"]
    assert rig.daemon.counted_as(A.wifi_mac) == "Person A"


def test_add_person_survives_a_failed_state_save(tmp_path, monkeypatch):
    rig = Rig(tmp_path)

    def broken(data):
        raise OSError("disk full")

    monkeypatch.setattr(rig.daemon._store, "save", broken)
    doc = rig.daemon.add_person("Sam", SAM_MAC)
    assert [p["name"] for p in doc["people"] if p["removable"]] == ["Sam"]
    assert rig.daemon.counted_as(SAM_MAC) == "Sam"


def test_add_person_with_an_invalid_address_is_400(tmp_path):
    rig = Rig(tmp_path)
    with pytest.raises(PeopleError) as e:
        rig.daemon.add_person("Sam", "not-a-mac")
    assert e.value.status == 400
    assert rig.daemon.counted_as("not-a-mac") is None


# The garage settings and the garage key of the document


def test_garage_settings_persist_and_do_not_appear_in_climate_settings(tmp_path):
    rig = Rig(tmp_path)
    document = rig.daemon.apply_settings(
        {"garage_left_open_minutes": 30, "garage_remote": True, "comfort_f": 70}
    )
    assert document["settings"] == {
        "comfort_f": 70, "away_f": 82, "away_minutes": 20}
    assert rig.saved()["settings"] == {
        "comfort_f": 70, "garage_left_open_minutes": 30, "garage_remote": True}
    fresh = rig.new_daemon()
    g = fresh.garage_settings()
    assert (g.left_open_minutes, g.away_open_grace_minutes) == (30, 5)
    assert (g.alerts, g.remote) == (True, True)
    assert "garage_remote" in fresh.settings()


def test_garage_settings_default_from_the_garage_config(tmp_path):
    from homebody.config import GarageConfig

    garage = GarageConfig(host="h", left_open_minutes=22, away_open_grace_minutes=1)
    rig = Rig(tmp_path, garage=garage)
    g = rig.daemon.garage_settings()
    assert (g.left_open_minutes, g.away_open_grace_minutes) == (22, 1)
    assert (g.alerts, g.remote) == (True, False)


def test_damaged_garage_settings_in_state_are_dropped(tmp_path):
    (tmp_path / STATE_FILE).write_text(json.dumps({"settings": {
        "garage_left_open_minutes": 9999,
        "garage_away_open_grace_minutes": "5",
        "garage_alerts": 1,
        "garage_remote": "yes",
        "comfort_f": 70,
    }}))
    rig = Rig(tmp_path)
    g = rig.daemon.garage_settings()
    assert (g.left_open_minutes, g.away_open_grace_minutes) == (15, 5)
    assert (g.alerts, g.remote) == (True, False)
    assert rig.daemon.settings()["comfort_f"] == 70


def test_failing_garage_snapshot_publishes_null_and_climate_fields(tmp_path):
    rig = Rig(tmp_path)
    rig.tick()

    def boom():
        raise RuntimeError("link exploded")

    rig.daemon.attach_garage(boom)
    document = rig.daemon.publish()
    assert document["garage"] is None
    assert document["settings"]["comfort_f"] == 72
    assert document["people"]


def test_attached_garage_source_fills_the_document(tmp_path):
    rig = Rig(tmp_path)
    assert rig.daemon.status_document()["garage"] is None
    rig.daemon.attach_garage(lambda: {"link": {"connected": True}})
    assert rig.daemon.status_document()["garage"] == {"link": {"connected": True}}
    rig.daemon.attach_garage(lambda: None)
    assert rig.daemon.status_document()["garage"] is None


def test_request_publish_wakes_run_and_publishes_without_a_tick(tmp_path, monkeypatch):
    from homebody import daemon as daemon_module

    monkeypatch.setattr(daemon_module, "TICK_SECONDS", 30.0)
    rig = Rig(tmp_path)
    published: list[int] = []
    real_publish = rig.daemon.publish

    def spy():
        published.append(1)
        return real_publish()

    rig.daemon.publish = spy  # type: ignore[method-assign]

    async def until(check):
        for _ in range(400):
            if check():
                return
            await asyncio.sleep(0.005)
        raise AssertionError("not reached")

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(rig.daemon.run(stop))
        await until(lambda: len(published) >= 1)  # the first tick published
        probes = len(rig.wifi.probed)
        before = len(published)
        rig.daemon.request_publish()
        await until(lambda: len(published) > before)
        assert len(rig.wifi.probed) == probes  # no second tick ran
        stop.set()
        await asyncio.wait_for(task, 3)

    asyncio.run(scenario())
