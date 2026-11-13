"""Presence: who is home, from the house's own network.

A person is *seen* when one of their devices answers a probe: Wi-Fi through the
kernel neighbour table confirmed by an ARP probe, or Bluetooth through ``l2ping``
as a fallback once Wi-Fi has been quiet for five minutes (phones drop Wi-Fi
while asleep). A person is *home* for 20 minutes after they were last seen and
*away* after that. After a restart nobody has been observed, so presence is
*unknown* (never "away") until someone is seen or one full away window has been
watched. Disabled people are never probed and never count.

The probes and the clock are injected so the logic is tested without a network,
a Bluetooth adapter, or real time. The two probe classes at the bottom are the
real ones; they never raise, a failed probe just means "not seen".
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from homebody.config import Person

log = logging.getLogger(__name__)

HOME = "home"
AWAY = "away"
UNKNOWN = "unknown"

AWAY_WINDOW_SECONDS = 20 * 60.0
WIFI_INTERVAL_SECONDS = 60.0
BLUETOOTH_AFTER_SECONDS = 5 * 60.0
BLUETOOTH_INTERVAL_SECONDS = 60.0

Runner = Callable[[Sequence[str], float], tuple[int, str]]


class WifiProbe(Protocol):
    def seen(self, mac: str) -> bool: ...


class BluetoothProbe(Protocol):
    def ping(self, mac: str) -> bool: ...


@dataclass(frozen=True)
class PersonStatus:
    id: str
    name: str
    state: str
    signal: str | None
    seconds_since_seen: float | None


class _Tracked:
    def __init__(self, person: Person, started: float) -> None:
        self.person = person
        self.last_seen: float | None = None
        self.last_wifi_seen: float | None = None
        self.last_signal: str | None = None
        self.last_bt_attempt: float | None = None
        self.started = started
        self.enabled = person.enabled


class PresenceTracker:
    def __init__(
        self,
        people: Sequence[Person],
        *,
        wifi: WifiProbe,
        bluetooth: BluetoothProbe,
        clock: Callable[[], float],
        away_window: float = AWAY_WINDOW_SECONDS,
    ) -> None:
        self._wifi = wifi
        self._bluetooth = bluetooth
        self._clock = clock
        self._window = away_window
        self._started = clock()
        # Every configured person is tracked; `enabled` starts from the config
        # default and can be flipped at runtime (the web page toggles it).
        self._tracked = [_Tracked(p, self._started) for p in people]
        self._last_wifi_poll: float | None = None

    def poll(self) -> None:
        """Run any probes that are due. Call this as often as you like."""
        now = self._clock()
        wifi_due = (
            self._last_wifi_poll is None
            or now - self._last_wifi_poll >= WIFI_INTERVAL_SECONDS
        )
        if wifi_due:
            self._last_wifi_poll = now
        for t in self._active():
            if wifi_due and t.person.wifi_mac and self._probe_wifi(t.person.wifi_mac):
                t.last_seen = t.last_wifi_seen = now
                t.last_signal = "wifi"
            if self._bluetooth_due(t, now):
                t.last_bt_attempt = now
                if self._probe_bluetooth(t.person.bluetooth_mac):
                    t.last_seen = now
                    t.last_signal = "bluetooth"

    def _active(self) -> list[_Tracked]:
        return [t for t in self._tracked if t.enabled]

    def set_enabled(self, person_id: str, enabled: bool) -> bool:
        """Turn a person on or off at runtime. Returns False for an unknown id.

        Off: never probed again and ignored by occupancy at once. On: observed
        afresh, so they are unknown (not away) until seen or a full window passes.
        """
        for t in self._tracked:
            if t.person.id == person_id:
                if enabled and not t.enabled:
                    t.started = self._clock()
                    t.last_seen = t.last_wifi_seen = None
                    t.last_signal = t.last_bt_attempt = None
                t.enabled = enabled
                return True
        return False

    def set_away_window(self, seconds: float) -> None:
        """Change how long someone stays home after the last sighting."""
        self._window = seconds

    def enabled_flags(self) -> dict[str, bool]:
        return {t.person.id: t.enabled for t in self._tracked}

    def _bluetooth_due(self, t: _Tracked, now: float) -> bool:
        if not t.person.bluetooth_mac:
            return False
        quiet_since = t.last_wifi_seen if t.last_wifi_seen is not None else t.started
        if now - quiet_since < BLUETOOTH_AFTER_SECONDS:
            return False
        return (
            t.last_bt_attempt is None
            or now - t.last_bt_attempt >= BLUETOOTH_INTERVAL_SECONDS
        )

    def _probe_wifi(self, mac: str) -> bool:
        try:
            return bool(self._wifi.seen(mac))
        except Exception:
            log.exception("wifi probe failed")
            return False

    def _probe_bluetooth(self, mac: str) -> bool:
        try:
            return bool(self._bluetooth.ping(mac))
        except Exception:
            log.exception("bluetooth probe failed")
            return False

    def _state(self, t: _Tracked, now: float) -> str:
        if t.last_seen is not None and now - t.last_seen < self._window:
            return HOME
        # Not seen within the window. Away only once we have watched a full
        # window (since boot, or since the last sighting); otherwise unknown.
        watched_since = max(t.last_seen, t.started) if t.last_seen else t.started
        return AWAY if now - watched_since >= self._window else UNKNOWN

    def statuses(self) -> list[PersonStatus]:
        now = self._clock()
        return [
            PersonStatus(
                id=t.person.id,
                name=t.person.name,
                state=self._state(t, now),
                signal=t.last_signal if t.last_seen is not None else None,
                seconds_since_seen=(
                    now - t.last_seen if t.last_seen is not None else None
                ),
            )
            for t in self._active()
        ]

    def occupied(self) -> bool | None:
        """True if anyone enabled is home, False if all are away, else None."""
        states = [s.state for s in self.statuses()]
        if HOME in states:
            return True
        if states and all(s == AWAY for s in states):
            return False
        return None


def run_command(cmd: Sequence[str], timeout: float) -> tuple[int, str]:
    done = subprocess.run(
        list(cmd), capture_output=True, text=True, timeout=timeout, check=False
    )
    return done.returncode, done.stdout


class NeighbourWifiProbe:
    """Wi-Fi presence from the kernel neighbour table plus an ARP probe.

    A REACHABLE entry for the address is proof the kernel just heard from the
    device. Otherwise the last known IP (from the table, remembered across
    entries vanishing) is ARP-pinged with ``arping``; a reply is proof. Both
    ``arping`` and ``l2ping`` need CAP_NET_RAW (see AGENTS.md, "Presence").
    """

    _BAD = {"FAILED", "INCOMPLETE"}

    def __init__(self, runner: Runner = run_command) -> None:
        self._runner = runner
        self._last_ip: dict[str, str] = {}

    def seen(self, mac: str) -> bool:
        mac = mac.lower()
        try:
            rc, out = self._runner(["ip", "-4", "neigh", "show"], 5)
            if rc == 0:
                for line in out.splitlines():
                    parts = line.split()
                    if "lladdr" not in parts:
                        continue
                    if parts[parts.index("lladdr") + 1].lower() != mac:
                        continue
                    state = parts[-1]
                    if state in self._BAD:
                        continue
                    self._last_ip[mac] = parts[0]
                    if state == "REACHABLE":
                        return True
            ip = self._last_ip.get(mac)
            if ip is None:
                return False
            rc, _ = self._runner(["arping", "-c", "1", "-w", "2", ip], 5)
            return rc == 0
        except (OSError, subprocess.SubprocessError):
            log.warning("wifi probe could not run")
            return False


class L2pingBluetoothProbe:
    """Bluetooth presence: one ``l2ping`` of a paired device."""

    def __init__(self, adapter: str | None, runner: Runner = run_command) -> None:
        self._adapter = adapter
        self._runner = runner

    def ping(self, mac: str) -> bool:
        cmd = ["l2ping"]
        if self._adapter:
            cmd += ["-i", self._adapter]
        cmd += ["-c", "1", "-t", "3", mac]
        try:
            rc, _ = self._runner(cmd, 8)
        except (OSError, subprocess.SubprocessError):
            log.warning("bluetooth probe could not run")
            return False
        return rc == 0
