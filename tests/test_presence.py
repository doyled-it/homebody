"""Tests for presence tracking (src/homebody/presence.py).

Probes and the clock are injected, so no network or real time
is involved. Times are seconds on a fake monotonic clock.
"""

from __future__ import annotations

import subprocess

from homebody import presence
from homebody.config import Person

MIN = 60.0
WINDOW = 20 * MIN


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeWifi:
    def __init__(self) -> None:
        self.visible: set[str] = set()
        self.calls: list[str] = []

    def seen(self, mac: str) -> bool:
        self.calls.append(mac)
        return mac in self.visible


A = Person("A", True, "aa:00:00:00:00:01", "a")
B = Person("B", False, "aa:00:00:00:00:02", "b")
C = Person("C", True, "aa:00:00:00:00:03", "c")


def make(people, clock=None):
    clock = clock or Clock()
    wifi = FakeWifi()
    tracker = presence.PresenceTracker(people, wifi=wifi, clock=clock)
    return tracker, wifi, clock


def step(tracker, clock, seconds=MIN):
    clock.advance(seconds)
    tracker.poll()


def state(tracker, name):
    return next(s for s in tracker.statuses() if s.name == name)


# Wi-Fi


def test_wifi_seen_marks_home_immediately():
    tracker, wifi, _ = make([A])
    wifi.visible.add(A.wifi_mac)
    tracker.poll()
    s = state(tracker, "A")
    assert s.state == presence.HOME
    assert s.signal == "wifi"
    assert tracker.occupied() is True


def test_home_until_exactly_the_away_window_then_away():
    tracker, wifi, clock = make([A])
    wifi.visible.add(A.wifi_mac)
    tracker.poll()
    wifi.visible.clear()
    clock.advance(WINDOW - 1)
    tracker.poll()
    assert state(tracker, "A").state == presence.HOME
    assert tracker.occupied() is True
    clock.advance(1)
    tracker.poll()
    assert state(tracker, "A").state == presence.AWAY
    assert tracker.occupied() is False


def test_wifi_probed_at_most_once_a_minute():
    tracker, wifi, clock = make([A])
    tracker.poll()
    clock.advance(30)
    tracker.poll()
    assert len(wifi.calls) == 1
    clock.advance(30)
    tracker.poll()
    assert len(wifi.calls) == 2


def test_wifi_seen_again_resets_the_window():
    tracker, wifi, clock = make([A])
    wifi.visible.add(A.wifi_mac)
    tracker.poll()
    wifi.visible.clear()
    clock.advance(WINDOW - MIN)
    tracker.poll()
    wifi.visible.add(A.wifi_mac)
    step(tracker, clock)
    wifi.visible.clear()
    clock.advance(WINDOW - 1)
    tracker.poll()
    assert state(tracker, "A").state == presence.HOME


# No Bluetooth fallback (removed, issue #17)


def test_wifi_asleep_ten_to_fifteen_minutes_stays_home():
    tracker, wifi, clock = make([A])
    wifi.visible.add(A.wifi_mac)
    tracker.poll()
    wifi.visible.clear()
    for _ in range(15):
        step(tracker, clock)
        assert tracker.occupied() is True
    wifi.visible.add(A.wifi_mac)
    step(tracker, clock)
    assert state(tracker, "A").signal == "wifi"


def test_silent_wifi_means_away_after_the_window_and_never_runs_l2ping():
    runner = Runner({"ip": (0, ""), "arping": (1, ""), "l2ping": (0, "")})
    clock = Clock()
    tracker = presence.PresenceTracker(
        [A], wifi=presence.NeighbourWifiProbe(runner=runner), clock=clock
    )
    for _ in range(25):
        step(tracker, clock)
    assert state(tracker, "A").state == presence.AWAY
    assert tracker.occupied() is False
    assert all(cmd[0] != "l2ping" for cmd in runner.cmds)


def test_presence_module_has_no_bluetooth_probe():
    assert not hasattr(presence, "L2pingBluetoothProbe")
    assert not hasattr(presence, "BluetoothProbe")


# Disabled people (Review Focus 4)


def test_disabled_person_is_never_probed():
    tracker, wifi, clock = make([B])
    wifi.visible.add(B.wifi_mac)
    for _ in range(30):
        step(tracker, clock)
    assert wifi.calls == []


def test_disabled_person_never_affects_occupancy():
    tracker, wifi, clock = make([A, B])
    wifi.visible.add(B.wifi_mac)
    tracker.poll()
    assert tracker.occupied() is None
    for _ in range(21):
        step(tracker, clock)
    assert tracker.occupied() is False
    assert [s.name for s in tracker.statuses()] == ["A"]


def test_only_disabled_people_means_unknown_forever():
    tracker, _, clock = make([B])
    for _ in range(30):
        step(tracker, clock)
    assert tracker.occupied() is None


# Runtime enabled flag


def test_person_disabled_at_runtime_stops_being_probed_and_counted():
    tracker, wifi, clock = make([A, C])
    wifi.visible.update({A.wifi_mac, C.wifi_mac})
    tracker.poll()
    assert tracker.occupied() is True
    assert tracker.set_enabled("a", False) is True
    wifi.calls.clear()
    wifi.visible.discard(C.wifi_mac)
    for _ in range(30):
        step(tracker, clock)
    assert A.wifi_mac not in wifi.calls
    assert [s.name for s in tracker.statuses()] == ["C"]
    assert tracker.enabled_flags() == {"a": False, "c": True}
    assert tracker.occupied() is False  # A's old sighting does not count


def test_disabling_the_only_home_person_takes_effect_at_once():
    tracker, wifi, _ = make([A])
    wifi.visible.add(A.wifi_mac)
    tracker.poll()
    tracker.set_enabled("a", False)
    assert tracker.occupied() is None


def test_person_enabled_at_runtime_starts_unknown_then_is_probed():
    tracker, wifi, clock = make([B])
    for _ in range(30):
        step(tracker, clock)
    assert wifi.calls == []
    assert tracker.set_enabled("b", True) is True
    assert tracker.occupied() is None  # observed afresh, not "away"
    wifi.visible.add(B.wifi_mac)
    step(tracker, clock)
    assert tracker.occupied() is True


def test_enabling_again_forgets_the_old_sighting():
    tracker, wifi, clock = make([A])
    wifi.visible.add(A.wifi_mac)
    tracker.poll()
    tracker.set_enabled("a", False)
    clock.advance(WINDOW + MIN)
    wifi.visible.clear()
    tracker.set_enabled("a", True)
    assert state(tracker, "A").state == presence.UNKNOWN


def test_set_enabled_unknown_id_is_false():
    tracker, *_ = make([A])
    assert tracker.set_enabled("nobody", True) is False


# Boot (Review Focus 5)


def test_starts_unknown_and_not_away_before_a_full_window():
    tracker, _, clock = make([A])
    tracker.poll()
    assert tracker.occupied() is None
    assert state(tracker, "A").state == presence.UNKNOWN
    clock.advance(WINDOW - 1)
    tracker.poll()
    assert tracker.occupied() is None


def test_never_seen_becomes_away_after_one_full_window_of_observation():
    tracker, _, clock = make([A])
    tracker.poll()
    clock.advance(WINDOW)
    tracker.poll()
    assert state(tracker, "A").state == presence.AWAY
    assert tracker.occupied() is False


def test_seen_after_boot_marks_home_on_the_next_probe():
    tracker, wifi, clock = make([A])
    tracker.poll()
    wifi.visible.add(A.wifi_mac)
    step(tracker, clock)
    assert tracker.occupied() is True


def test_one_person_home_one_unknown_is_occupied():
    tracker, wifi, _ = make([A, C])
    wifi.visible.add(A.wifi_mac)
    tracker.poll()
    assert tracker.occupied() is True


def test_one_away_one_home_is_occupied():
    tracker, wifi, clock = make([A, C])
    tracker.poll()
    clock.advance(WINDOW - MIN)
    wifi.visible.add(C.wifi_mac)
    tracker.poll()
    wifi.visible.clear()
    clock.advance(WINDOW - 1)
    tracker.poll()
    # A was never seen and has been observed a full window; C was seen 19:59 ago.
    assert state(tracker, "A").state == presence.AWAY
    assert state(tracker, "C").state == presence.HOME
    assert tracker.occupied() is True


def test_probe_exceptions_are_contained():
    class Boom:
        def seen(self, mac):
            raise RuntimeError("probe broke")

        def ping(self, mac):
            raise RuntimeError("probe broke")

    clock = Clock()
    tracker = presence.PresenceTracker([A], wifi=Boom(), clock=clock)
    tracker.poll()
    clock.advance(6 * MIN)
    tracker.poll()
    assert tracker.occupied() is None


def test_status_reports_last_seen_age():
    tracker, wifi, clock = make([A])
    wifi.visible.add(A.wifi_mac)
    tracker.poll()
    wifi.visible.clear()
    clock.advance(90)
    tracker.poll()
    assert state(tracker, "A").seconds_since_seen == 90


# Real probes with an injected command runner


class Runner:
    def __init__(self, results):
        self.results = results
        self.cmds: list[list[str]] = []

    def __call__(self, cmd, timeout):
        self.cmds.append(cmd)
        result = self.results.get(cmd[0])
        if isinstance(result, Exception):
            raise result
        return result


NEIGH_REACHABLE = "192.0.2.10 dev eth0 lladdr aa:00:00:00:00:01 REACHABLE\n"
NEIGH_STALE = "192.0.2.10 dev eth0 lladdr aa:00:00:00:00:01 STALE\n"
NEIGH_FAILED = "192.0.2.10 dev eth0  FAILED\n"
NEIGH_OTHER = "192.0.2.11 dev eth0 lladdr aa:00:00:00:00:99 REACHABLE\n"


def test_neighbour_reachable_is_seen_without_arping():
    runner = Runner({"ip": (0, NEIGH_REACHABLE)})
    probe = presence.NeighbourWifiProbe(runner=runner)
    assert probe.seen("aa:00:00:00:00:01") is True
    assert [c[0] for c in runner.cmds] == ["ip"]


def test_neighbour_stale_is_confirmed_by_arping_the_known_ip():
    runner = Runner({"ip": (0, NEIGH_STALE), "arping": (0, "reply")})
    probe = presence.NeighbourWifiProbe(runner=runner)
    assert probe.seen("AA:00:00:00:00:01") is True
    arp = next(c for c in runner.cmds if c[0] == "arping")
    assert "192.0.2.10" in arp


def test_neighbour_stale_and_no_arp_reply_is_not_seen():
    runner = Runner({"ip": (0, NEIGH_STALE), "arping": (1, "")})
    assert presence.NeighbourWifiProbe(runner=runner).seen("aa:00:00:00:00:01") is False


def test_unknown_mac_is_not_seen_and_not_arped():
    runner = Runner({"ip": (0, NEIGH_OTHER)})
    assert presence.NeighbourWifiProbe(runner=runner).seen("aa:00:00:00:00:01") is False
    assert [c[0] for c in runner.cmds] == ["ip"]


def test_failed_entry_is_not_seen():
    runner = Runner({"ip": (0, NEIGH_FAILED)})
    assert presence.NeighbourWifiProbe(runner=runner).seen("aa:00:00:00:00:01") is False


def test_remembers_last_known_ip_when_the_entry_vanishes():
    runner = Runner({"ip": (0, NEIGH_STALE), "arping": (0, "")})
    probe = presence.NeighbourWifiProbe(runner=runner)
    assert probe.seen("aa:00:00:00:00:01") is True
    runner.results["ip"] = (0, "")
    runner.cmds.clear()
    assert probe.seen("aa:00:00:00:00:01") is True
    assert any("192.0.2.10" in c for c in runner.cmds)


def test_wifi_probe_survives_missing_tools_and_timeouts():
    for err in (FileNotFoundError(), subprocess.TimeoutExpired("ip", 1), OSError()):
        probe = presence.NeighbourWifiProbe(runner=Runner({"ip": err}))
        assert probe.seen("aa:00:00:00:00:01") is False


def test_subprocess_runner_runs_a_real_command():
    rc, out = presence.run_command(["echo", "hi"], timeout=5)
    assert (rc, out.strip()) == (0, "hi")


def test_added_person_is_tracked_and_removable():
    clock, wifi = Clock(), FakeWifi()
    tracker = presence.PresenceTracker(
        [Person("Alex", True, "02:00:00:00:00:01", "person-1")],
        wifi=wifi, clock=clock,
    )
    tracker.add_person(Person("Sam", True, "02:00:00:00:00:0a", "added-0000000a"))
    by_id = {s.id: s for s in tracker.statuses()}
    assert by_id["added-0000000a"].removable is True
    assert by_id["added-0000000a"].state == presence.UNKNOWN
    assert by_id["person-1"].removable is False
    wifi.visible.add("02:00:00:00:00:0a")
    tracker.poll()
    states = {s.id: s.state for s in tracker.statuses()}
    assert states["added-0000000a"] == presence.HOME
    assert tracker.remove_person("added-0000000a") is True
    assert [s.id for s in tracker.statuses()] == ["person-1"]


def test_configured_person_cannot_be_removed():
    tracker = presence.PresenceTracker(
        [Person("Alex", True, "02:00:00:00:00:01", "person-1")],
        wifi=FakeWifi(), clock=Clock(),
    )
    assert tracker.remove_person("person-1") is False
    assert tracker.remove_person("nobody") is False
    assert len(tracker.statuses()) == 1


def test_counted_as_finds_config_people_ignoring_case():
    alex = Person("Alex", False, "02:00:00:00:00:0a", "person-1")
    tracker = presence.PresenceTracker(
        [alex], wifi=FakeWifi(), clock=Clock(),
    )
    assert tracker.person_for_mac("02:00:00:00:00:0A") == alex
    assert tracker.person_for_mac("02:00:00:00:00:0b") is None


def test_neighbour_mac_reads_lladdr():
    runner = Runner(
        {"ip": (0, "192.0.2.5 dev eth0 lladdr 02:00:00:00:00:0A STALE\n")}
    )
    assert presence.neighbour_mac("192.0.2.5", runner) == "02:00:00:00:00:0a"
    assert runner.cmds == [["ip", "-4", "neigh", "show", "192.0.2.5"]]


def test_neighbour_mac_rejects_failed_and_non_ipv4():
    failed = Runner({"ip": (0, "192.0.2.5 dev eth0  FAILED\n")})
    assert presence.neighbour_mac("192.0.2.5", failed) is None
    line = "192.0.2.5 dev eth0 lladdr 02:00:00:00:00:0a INCOMPLETE\n"
    incomplete = Runner({"ip": (0, line)})
    assert presence.neighbour_mac("192.0.2.5", incomplete) is None
    untouched = Runner({})
    for bad in ("fe80::1", "not-an-ip", "", "192.0.2.5; ls"):
        assert presence.neighbour_mac(bad, untouched) is None
    assert untouched.cmds == []


def test_neighbour_mac_is_none_on_errors():
    assert presence.neighbour_mac("192.0.2.5", Runner({"ip": OSError("x")})) is None
    assert presence.neighbour_mac("192.0.2.5", Runner({"ip": (1, "")})) is None
