"""Climate decisions: what the thermostat should be doing, and when to say so.

Pure logic with an injected clock; it never touches the network. The daemon
feeds it the occupancy from presence and thermostat snapshots from Matter, and
asks what (if anything) to send.

Rules (from the design):

- Occupied (anyone home) means Cool at the comfort setpoint. Unoccupied
  (everyone away for the window) means Cool at the away setpoint.
- Presence "unknown" (just after a boot) never triggers anything. Only the first
  real occupied or unoccupied state does.
- A command is owed once per occupied/unoccupied transition. Transitions that
  pile up while the thermostat is unreachable collapse: the target is always
  computed from the latest occupancy, so only the latest state is ever sent.
- Before commanding, the thermostat's mode and setpoint are compared with what
  homebody last set. A difference means a person changed it by hand: record an
  override and send nothing until the next transition, which takes over again.
- Commands are paced to one a minute; failures retry with a growing backoff.

Setpoint comparisons allow a quarter of a degree Celsius. The Nest keeps
half-degree steps, so a 72 F write (22.22 C) can read back as 22.0 C; that is
rounding, not a person. One Nest step (50) is always larger than the tolerance.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from typing import Any

from homebody.matter import (
    SystemMode,
    ThermostatState,
    fahrenheit_to_centi_celsius,
)

MIN_COMMAND_INTERVAL = 60.0
BACKOFF_INITIAL = 60.0
BACKOFF_CAP = 15 * 60.0
SETPOINT_TOLERANCE_C100 = 25


@dataclass(frozen=True)
class Command:
    mode: int
    setpoint_c100: int


@dataclass
class ClimateState:
    """The minimal state that survives a restart."""

    # The occupancy of the last transition seen (None until the first one).
    occupied: bool | None = None
    # What homebody last set (None until it has set anything).
    mode: int | None = None
    setpoint_c100: int | None = None
    # A person changed the thermostat by hand; hold until the next transition.
    override: bool = False
    # A transition is owed a command.
    pending: bool = False
    # The transition followed an override: take over without the hand check.
    takeover: bool = False
    # A command was started and has not finished, so the thermostat may be
    # half-written and must not be read as a hand change.
    attempting: bool = False
    last_transition: float | None = None
    last_change: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Any) -> ClimateState:
        """Rebuild from saved data; anything malformed falls back to defaults."""
        if not isinstance(raw, dict):
            return cls()
        kinds: dict[str, tuple[type, ...]] = {
            "occupied": (bool,),
            "mode": (int,),
            "setpoint_c100": (int,),
            "override": (bool,),
            "pending": (bool,),
            "takeover": (bool,),
            "attempting": (bool,),
            "last_transition": (int, float),
            "last_change": (int, float),
        }
        clean: dict[str, Any] = {}
        for f in fields(cls):
            value = raw.get(f.name)
            allowed = kinds[f.name]
            if isinstance(value, bool) and bool not in allowed:
                continue
            if isinstance(value, allowed):
                clean[f.name] = value
        return cls(**clean)


class ClimateController:
    def __init__(
        self,
        *,
        comfort_f: float,
        away_f: float,
        clock: Callable[[], float],
        state: ClimateState | None = None,
    ) -> None:
        self._comfort = fahrenheit_to_centi_celsius(comfort_f)
        self._away = fahrenheit_to_centi_celsius(away_f)
        self._clock = clock
        self.state = state or ClimateState()
        self._last_attempt: float | None = None
        self._retry_at = 0.0
        self._failures = 0

    def target(self) -> Command | None:
        """What the thermostat should be set to now, or None while unknown."""
        occupied = self.state.occupied
        if occupied is None:
            return None
        return Command(
            int(SystemMode.COOL), self._comfort if occupied else self._away
        )

    def observe(self, occupied: bool | None) -> None:
        """Feed the latest occupancy. A change of state is a transition."""
        if occupied is None or occupied == self.state.occupied:
            return
        s = self.state
        s.takeover = s.override
        s.override = False
        s.attempting = False
        s.occupied = occupied
        s.pending = True
        s.last_transition = self._clock()

    def due(self) -> bool:
        """True when a command is owed and pacing and backoff allow trying."""
        s = self.state
        if not s.pending or s.override or s.occupied is None:
            return False
        now = self._clock()
        if now < self._retry_at:
            return False
        recent = [t for t in (self._last_attempt, s.last_change) if t is not None]
        return not recent or now - max(recent) >= MIN_COMMAND_INTERVAL

    def decide(self, thermostat: ThermostatState) -> Command | None:
        """Given a fresh thermostat read, the command to send, or None.

        None also covers the two ways a transition ends without a write: a
        hand change (override recorded) and a thermostat already at the target.
        """
        s = self.state
        cmd = self.target()
        if not s.pending or s.override or cmd is None:
            return None
        if not s.takeover and not s.attempting and self._hand_changed(thermostat):
            s.override = True
            s.pending = False
            return None
        if self._matches(thermostat, cmd):
            self._record(cmd)
            self._settle()
            return None
        return cmd

    def begin_command(self) -> None:
        self.state.attempting = True
        self._last_attempt = self._clock()

    def succeeded(self, cmd: Command) -> None:
        self._record(cmd)
        self.state.last_change = self._clock()
        self._settle()

    def failed(self) -> None:
        """A read or write failed: back off before the next try."""
        delay = min(BACKOFF_INITIAL * 2**self._failures, BACKOFF_CAP)
        self._failures += 1
        self._retry_at = self._clock() + delay

    def _settle(self) -> None:
        s = self.state
        s.pending = False
        s.takeover = False
        s.attempting = False
        self._failures = 0
        self._retry_at = 0.0

    def _record(self, cmd: Command) -> None:
        self.state.mode = cmd.mode
        self.state.setpoint_c100 = cmd.setpoint_c100

    def _hand_changed(self, t: ThermostatState) -> bool:
        s = self.state
        if s.mode is None or s.setpoint_c100 is None:
            return False  # nothing commanded yet, so nothing to compare with
        return not self._matches(t, Command(s.mode, s.setpoint_c100))

    @staticmethod
    def _matches(t: ThermostatState, cmd: Command) -> bool:
        if t.system_mode != cmd.mode or t.occupied_cooling_setpoint_c100 is None:
            return False
        gap = abs(t.occupied_cooling_setpoint_c100 - cmd.setpoint_c100)
        return gap <= SETPOINT_TOLERANCE_C100
