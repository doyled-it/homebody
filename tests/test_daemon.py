"""Tests for the service loop (src/homebody/daemon.py).

The loop runs against the in-memory FakeController, fake probes, and a fake
clock; a test calls ``tick()`` and advances the clock by hand, so nothing sleeps
and nothing touches the network. Times are seconds on the fake clock.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fakes import FakeConnectionContext, FakeController
from homebody import matter
from homebody.config import Config, Person
from homebody.daemon import STATE_FILE, Daemon
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


def test_review_focus_3_manual_change_is_held_until_the_next_transition(tmp_path):
    rig = Rig(tmp_path)
    rig.wifi.home = {A.wifi_mac}
    rig.run_for(5 * MIN)
    # A person turns the Nest to 75 F (2389) by hand.
    rig.controller.nodes[1]["attributes"][SETPOINT_PATH] = 2389
    writes_before = len(rig.controller.writes())
    rig.wifi.home = set()
    rig.run_for(WINDOW + 5 * MIN)  # everyone leaves: a transition, but held
    assert rig.setpoint() == 2389
    assert len(rig.controller.writes()) == writes_before
    assert rig.saved()["climate"]["override"] is True
    # Someone comes home: the next transition, automation resumes.
    rig.wifi.home = {A.wifi_mac}
    rig.run_for(3 * MIN)
    assert rig.setpoint() == COMFORT
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
    rig.wifi.home = set()
    rig.run_for(WINDOW + 5 * MIN)
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
