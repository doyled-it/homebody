"""Tests for presence tracking (src/homebody/presence.py).

Probes and the clock are injected, so no network, Bluetooth adapter, or real time
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


class FakeBluetooth:
    def __init__(self) -> None:
        self.visible: set[str] = set()
        self.calls: list[str] = []

    def ping(self, mac: str) -> bool:
        self.calls.append(mac)
        return mac in self.visible


A = Person("A", True, "aa:00:00:00:00:01", "bb:00:00:00:00:01")
B = Person("B", False, "aa:00:00:00:00:02", "bb:00:00:00:00:02")
C = Person("C", True, "aa:00:00:00:00:03", None)


def make(people, clock=None):
    clock = clock or Clock()
    wifi, bt = FakeWifi(), FakeBluetooth()
    tracker = presence.PresenceTracker(people, wifi=wifi, bluetooth=bt, clock=clock)
    return tracker, wifi, bt, clock


def step(tracker, clock, seconds=MIN):
    clock.advance(seconds)
    tracker.poll()


def state(tracker, name):
    return next(s for s in tracker.statuses() if s.name == name)


# Wi-Fi


def test_wifi_seen_marks_home_immediately():
    tracker, wifi, _, _ = make([A])
    wifi.visible.add(A.wifi_mac)
    tracker.poll()
    s = state(tracker, "A")
    assert s.state == presence.HOME
    assert s.signal == "wifi"
    assert tracker.occupied() is True


def test_home_until_exactly_the_away_window_then_away():
    tracker, wifi, _, clock = make([A])
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
    tracker, wifi, _, clock = make([A])
    tracker.poll()
    clock.advance(30)
    tracker.poll()
    assert len(wifi.calls) == 1
    clock.advance(30)
    tracker.poll()
    assert len(wifi.calls) == 2


def test_wifi_seen_again_resets_the_window():
    tracker, wifi, _, clock = make([A])
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


# Bluetooth fallback


def test_bluetooth_not_tried_before_five_minutes_without_wifi():
    tracker, wifi, bt, clock = make([A])
    wifi.visible.add(A.wifi_mac)
    tracker.poll()
    wifi.visible.clear()
    for _ in range(4):
        step(tracker, clock)
    assert bt.calls == []  # 4 minutes since last Wi-Fi
    step(tracker, clock)  # 5 minutes
    assert bt.calls == [A.bluetooth_mac]


def test_bluetooth_at_most_once_a_minute():
    tracker, wifi, bt, clock = make([A])
    wifi.visible.add(A.wifi_mac)
    tracker.poll()
    wifi.visible.clear()
    clock.advance(5 * MIN)
    tracker.poll()
    clock.advance(20)
    tracker.poll()
    clock.advance(20)
    tracker.poll()
    assert len(bt.calls) == 1
    clock.advance(20)
    tracker.poll()
    assert len(bt.calls) == 2


def test_bluetooth_seen_keeps_a_sleeping_phone_home():
    """Review Focus 1: Wi-Fi asleep for 15 to 25 minutes, the house stays occupied."""
    tracker, wifi, bt, clock = make([A])
    wifi.visible.add(A.wifi_mac)
    tracker.poll()
    wifi.visible.clear()
    bt.visible.add(A.bluetooth_mac)
    for _ in range(30):
        step(tracker, clock)
        assert tracker.occupied() is True
    s = state(tracker, "A")
    assert s.state == presence.HOME
    assert s.signal == "bluetooth"


def test_wifi_asleep_ten_to_fifteen_minutes_stays_home_even_without_bluetooth():
    tracker, wifi, _, clock = make([A])
    wifi.visible.add(A.wifi_mac)
    tracker.poll()
    wifi.visible.clear()
    for _ in range(15):
        step(tracker, clock)
        assert tracker.occupied() is True
    wifi.visible.add(A.wifi_mac)
    step(tracker, clock)
    assert state(tracker, "A").signal == "wifi"


def test_bluetooth_ping_failure_does_not_extend_the_window():
    tracker, wifi, bt, clock = make([A])
    wifi.visible.add(A.wifi_mac)
    tracker.poll()
    wifi.visible.clear()
    clock.advance(WINDOW)
    tracker.poll()
    assert state(tracker, "A").state == presence.AWAY
    assert bt.calls  # it tried, and failed


def test_no_bluetooth_probe_without_a_bluetooth_address():
    tracker, _, bt, clock = make([C])
    for _ in range(10):
        step(tracker, clock)
    assert bt.calls == []


def test_bluetooth_fallback_counts_from_boot_when_wifi_never_seen():
    tracker, _, bt, clock = make([A])
    tracker.poll()
    for _ in range(4):
        step(tracker, clock)
    assert bt.calls == []
    step(tracker, clock)
    assert len(bt.calls) == 1


# Disabled people (Review Focus 4)


def test_disabled_person_is_never_probed():
    tracker, wifi, bt, clock = make([B])
    wifi.visible.add(B.wifi_mac)
    bt.visible.add(B.bluetooth_mac)
    for _ in range(30):
        step(tracker, clock)
    assert wifi.calls == []
    assert bt.calls == []


def test_disabled_person_never_affects_occupancy():
    tracker, wifi, _, clock = make([A, B])
    wifi.visible.add(B.wifi_mac)
    tracker.poll()
    assert tracker.occupied() is None
    for _ in range(21):
        step(tracker, clock)
    assert tracker.occupied() is False
    assert [s.name for s in tracker.statuses()] == ["A"]


def test_only_disabled_people_means_unknown_forever():
    tracker, _, _, clock = make([B])
    for _ in range(30):
        step(tracker, clock)
    assert tracker.occupied() is None


# Boot (Review Focus 5)


def test_starts_unknown_and_not_away_before_a_full_window():
    tracker, _, _, clock = make([A])
    tracker.poll()
    assert tracker.occupied() is None
    assert state(tracker, "A").state == presence.UNKNOWN
    clock.advance(WINDOW - 1)
    tracker.poll()
    assert tracker.occupied() is None


def test_never_seen_becomes_away_after_one_full_window_of_observation():
    tracker, _, _, clock = make([A])
    tracker.poll()
    clock.advance(WINDOW)
    tracker.poll()
    assert state(tracker, "A").state == presence.AWAY
    assert tracker.occupied() is False


def test_seen_after_boot_marks_home_on_the_next_probe():
    tracker, wifi, _, clock = make([A])
    tracker.poll()
    wifi.visible.add(A.wifi_mac)
    step(tracker, clock)
    assert tracker.occupied() is True


def test_one_person_home_one_unknown_is_occupied():
    tracker, wifi, _, _ = make([A, C])
    wifi.visible.add(A.wifi_mac)
    tracker.poll()
    assert tracker.occupied() is True


def test_one_away_one_home_is_occupied():
    tracker, wifi, _, clock = make([A, C])
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
    tracker = presence.PresenceTracker([A], wifi=Boom(), bluetooth=Boom(), clock=clock)
    tracker.poll()
    clock.advance(6 * MIN)
    tracker.poll()
    assert tracker.occupied() is None


def test_status_reports_last_seen_age():
    tracker, wifi, _, clock = make([A])
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


def test_l2ping_uses_the_adapter_and_one_packet():
    runner = Runner({"l2ping": (0, "")})
    probe = presence.L2pingBluetoothProbe(adapter="00:00:00:00:00:01", runner=runner)
    assert probe.ping("bb:00:00:00:00:01") is True
    cmd = runner.cmds[0]
    assert cmd[0] == "l2ping"
    assert cmd[cmd.index("-i") + 1] == "00:00:00:00:00:01"
    assert cmd[cmd.index("-c") + 1] == "1"
    assert cmd[-1] == "bb:00:00:00:00:01"


def test_l2ping_failure_and_missing_tool_are_not_seen():
    runner = Runner({"l2ping": (1, "")})
    probe = presence.L2pingBluetoothProbe(adapter=None, runner=runner)
    assert probe.ping("bb:00:00:00:00:01") is False
    assert "-i" not in runner.cmds[0]
    missing = Runner({"l2ping": FileNotFoundError()})
    gone = presence.L2pingBluetoothProbe(None, runner=missing)
    assert gone.ping("bb:00:00:00:00:01") is False


def test_subprocess_runner_runs_a_real_command():
    rc, out = presence.run_command(["echo", "hi"], timeout=5)
    assert (rc, out.strip()) == (0, "hi")
