"""The garage door model: pure state, open episodes, and its own state file.

Pure apart from ``GarageStore``: an injected clock, no asyncio, no link calls.
The model is fed ``LinkState`` pushes and link up/down events and answers what
the door is, since when, and whether an open episode is running. It never
carries a door state across a link drop: while the link is down, or until the
first door push after a connect, ``state`` reads ``unknown``.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from homebody.garage_link import LinkState

log = logging.getLogger(__name__)

GARAGE_FILE = "garage.json"

DoorState = Literal["closed", "open", "opening", "closing", "stopped", "unknown"]

_REAL_STATES = ("closed", "open", "opening", "closing", "stopped")


@dataclass(frozen=True)
class GarageSettings:
    left_open_minutes: int = 15
    away_open_grace_minutes: int = 5
    alerts: bool = True
    remote: bool = False


@dataclass
class Episode:
    """One run of the door being away from closed.

    ``fired`` holds the alert kinds already sent for this episode.
    ``remote_started`` is set when a remote command opened it.
    ``close_commanded`` is set when an accepted command asked it to close.
    """

    started: float
    fired: set[str] = field(default_factory=set)
    remote_started: bool = False
    since_approximate: bool = False
    close_commanded: bool = False


def derive_state(link: LinkState) -> DoorState | None:
    """The door state a push carries, or None when it carries no door data."""
    if link.operation in ("opening", "closing"):
        return link.operation
    if link.position is None:
        return None
    if link.position <= 0.0:
        return "closed"
    if link.position >= 1.0:
        return "open"
    return "stopped" if link.has_position else "open"


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


class GarageModel:
    def __init__(
        self, clock: Callable[[], float], saved: dict[str, Any] | None = None
    ) -> None:
        self._clock = clock
        self._connected = False
        self._awaiting_door = True
        self._connect_time: float | None = None
        self._state: DoorState = "unknown"
        self._since: float | None = None
        self._since_approximate = False
        self._last_seen: float | None = None
        self._last_known: DoorState | None = None
        self._episode: Episode | None = None
        self._pending_remote_until: float | None = None
        self._remote_marked: Episode | None = None
        self._obstruction: bool | None = None
        self._lockout: bool | None = None
        self._light: bool | None = None
        self._position: float | None = None
        self.changes: list[tuple[float, DoorState]] = []
        self._restore(saved if isinstance(saved, dict) else {})

    # persistence

    def _restore(self, saved: dict[str, Any]) -> None:
        known = saved.get("last_known_state")
        if known in _REAL_STATES:
            self._last_known = known
        since = _number(saved.get("since"))
        if since is not None and self._last_known is not None:
            self._since = since
            self._since_approximate = saved.get("since_approximate") is True
        last_seen = _number(saved.get("last_seen"))
        if last_seen is not None:
            self._last_seen = last_seen
        raw = saved.get("episode")
        if isinstance(raw, dict):
            started = _number(raw.get("started"))
            if started is not None:
                fired_raw = raw.get("fired")
                fired = (
                    {k for k in fired_raw if isinstance(k, str)}
                    if isinstance(fired_raw, list)
                    else set()
                )
                self._episode = Episode(
                    started=started,
                    fired=fired,
                    remote_started=raw.get("remote_started") is True,
                    since_approximate=raw.get("since_approximate") is True,
                    close_commanded=raw.get("close_commanded") is True,
                )

    def to_saved(self) -> dict[str, Any]:
        episode = None
        if self._episode is not None:
            episode = {
                "started": self._episode.started,
                "fired": sorted(self._episode.fired),
                "remote_started": self._episode.remote_started,
                "since_approximate": self._episode.since_approximate,
                "close_commanded": self._episode.close_commanded,
            }
        return {
            "last_known_state": self._last_known,
            "since": self._since,
            "since_approximate": self._since_approximate,
            "last_seen": self._last_seen,
            "episode": episode,
        }

    # properties

    @property
    def state(self) -> DoorState:
        if not self._connected or self._awaiting_door:
            return "unknown"
        return self._state

    @property
    def since(self) -> float | None:
        return self._since

    @property
    def since_approximate(self) -> bool:
        return self._since_approximate

    @property
    def last_seen(self) -> float | None:
        return self._last_seen

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def episode(self) -> Episode | None:
        return self._episode

    @property
    def obstruction(self) -> bool | None:
        return self._obstruction

    @property
    def lockout(self) -> bool | None:
        return self._lockout

    @property
    def light(self) -> bool | None:
        return self._light

    @property
    def position_percent(self) -> int | None:
        if self._position is None:
            return None
        return round(self._position * 100)

    @property
    def last_known_state(self) -> DoorState | None:
        return self._last_known

    # events

    def _now(self) -> float:
        """The clock, with stored times pulled back if the clock stepped back."""
        now = self._clock()
        if self._since is not None and self._since > now:
            self._since = now
        if self._last_seen is not None and self._last_seen > now:
            self._last_seen = now
        if self._connect_time is not None and self._connect_time > now:
            self._connect_time = now
        if self._episode is not None and self._episode.started > now:
            self._episode.started = now
        return now

    def link_changed(self, connected: bool) -> None:
        now = self._now()
        if connected:
            if self._connected:
                return
            self._connected = True
            self._awaiting_door = True
            self._connect_time = now
            return
        self._connected = False
        self._awaiting_door = True
        self._obstruction = None
        self._lockout = None
        self._light = None
        self._position = None

    def state_received(self, link: LinkState) -> None:
        if not self._connected:
            self.link_changed(True)
        now = self._now()
        self._last_seen = now
        if link.obstruction is not None:
            self._obstruction = link.obstruction
        if link.lockout is not None:
            self._lockout = link.lockout
        if link.light is not None:
            self._light = link.light
        derived = derive_state(link)
        if derived is None:
            return
        self._position = link.position
        first = self._awaiting_door
        self._awaiting_door = False
        previous = self._last_known
        self._state = derived
        self._last_known = derived
        if first:
            when = self._connect_time if self._connect_time is not None else now
            if derived == previous and self._since is not None:
                pass
            else:
                self._since = when
                self._since_approximate = True
            self._episode_on_connect(derived, when)
        else:
            if derived != previous:
                self._since = now
                self._since_approximate = False
            self._episode_on_change(previous, derived, now)
        if derived != previous:
            self.changes.append((now, derived))

    def _new_episode(self, started: float, approximate: bool) -> None:
        self._episode = Episode(
            started=started,
            remote_started=self._pending_remote_live(),
            since_approximate=approximate,
        )
        if self._episode.remote_started:
            self._remote_marked = self._episode
        self._pending_remote_until = None

    def _episode_on_connect(self, derived: DoorState, when: float) -> None:
        if derived == "closed":
            self._episode = None
        elif self._episode is None:
            self._new_episode(when, True)

    def _episode_on_change(
        self, previous: DoorState | None, derived: DoorState, now: float
    ) -> None:
        if derived == "closed":
            self._episode = None
        elif self._episode is None:
            self._new_episode(now, False)

    # commands

    def _pending_remote_live(self) -> bool:
        until = self._pending_remote_until
        return until is not None and self._clock() <= until

    def note_remote_command(self, window: float = 60.0) -> None:
        """Mark the current episode, or the next one begun within ``window`` seconds.

        An unclaimed mark expires, so a command that never moved the door cannot
        mark a later episode that someone else started.
        """
        if self._episode is not None:
            if not self._episode.remote_started:
                self._remote_marked = self._episode
            self._episode.remote_started = True
        else:
            self._pending_remote_until = self._now() + window

    def clear_remote_command(self) -> None:
        """Undo ``note_remote_command`` after a failed or unconfirmed command."""
        self._pending_remote_until = None
        if self._remote_marked is not None and self._remote_marked is self._episode:
            self._remote_marked.remote_started = False
        self._remote_marked = None

    def note_command(self, action: str) -> None:
        """Record that an accepted command asked for ``action``."""
        if action == "close" and self._episode is not None:
            self._episode.close_commanded = True

    def pop_changes(self) -> list[tuple[float, DoorState]]:
        out = self.changes
        self.changes = []
        return out


class GarageStore:
    """``garage.json``: replaced atomically, tolerant of damage.

    Deliberately separate from ``daemon.StateStore`` so a garage write failure
    can never touch climate state.
    """

    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self.path = directory / GARAGE_FILE

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            log.warning("garage file unreadable, starting fresh")
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, data: dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._dir, prefix=".garage-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(data, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
