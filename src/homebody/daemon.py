"""The service loop: presence in, thermostat commands out.

Each tick polls presence (probes are due on their own schedule), feeds the
occupancy to the climate controller, and, when a command is owed, reads the
thermostat and writes the target. Nothing here can take the service down: the
Matter controller or the thermostat being unreachable only means the climate
controller backs off and tries again later, while presence keeps tracking.

State that must survive a restart (what was last commanded, the override flag,
the last transition and change times, runtime enable flags) is saved to
``state.json`` in the state directory with an atomic replace.

The clock is wall time (``time.time``) because some of the saved timestamps are
shown to people; tests inject a fake.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from pathlib import Path
from typing import Any

from homebody import matter, status
from homebody.climate import ClimateController, ClimateState
from homebody.config import Config, Person
from homebody.people import (
    MAX_ADDED,
    PeopleError,
    clean_mac,
    clean_name,
    new_person_id,
    parse_saved,
    to_saved,
)
from homebody.presence import (
    L2pingBluetoothProbe,
    NeighbourWifiProbe,
    PresenceTracker,
)

log = logging.getLogger(__name__)

STATE_FILE = "state.json"
TICK_SECONDS = 15.0
# How often the thermostat is read just to keep the status file fresh.
STATUS_READ_SECONDS = 60.0
SETTING_KEYS = ("enabled", "comfort_f", "away_f", "away_minutes")
# One whole thermostat exchange (connect, read, write) may not hang the loop.
EXCHANGE_TIMEOUT_SECONDS = 30.0

ConnectFactory = Callable[[], AbstractAsyncContextManager[Any]]


class StateStore:
    """The state file: JSON, replaced atomically, tolerant of damage."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self.path = directory / STATE_FILE

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            log.warning("state file unreadable, starting fresh")
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, data: dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._dir, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(data, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


class Daemon:
    def __init__(
        self,
        config: Config,
        *,
        presence: PresenceTracker,
        connect: ConnectFactory,
        clock: Callable[[], float] = time.time,
        store: StateStore | None = None,
        status_writer: status.StatusWriter | None = None,
    ) -> None:
        self._config = config
        self._presence = presence
        self._connect = connect
        self._clock = clock
        self._store = store or StateStore(config.state_dir)
        self._status = status_writer

        saved = self._store.load()
        # Settings changed through the API live here and win over local.toml,
        # which homebody never writes. Only the changed keys are stored.
        self._overrides = _clean_settings(saved.get("settings"))
        current = self.settings()
        self._presence.set_away_window(current["away_minutes"] * 60.0)
        self.climate = ClimateController(
            comfort_f=current["comfort_f"],
            away_f=current["away_f"],
            clock=clock,
            state=ClimateState.from_dict(saved.get("climate")),
        )
        self._added: list[Person] = []
        for person in parse_saved(saved.get("people")):
            if self._presence.person_for_mac(person.wifi_mac or ""):
                continue
            if any(p.id == person.id for p in self._added):
                continue
            self._presence.add_person(person)
            self._added.append(person)
        flags = saved.get("enabled")
        self._enabled: dict[str, bool] = {
            k: v
            for k, v in (flags.items() if isinstance(flags, dict) else [])
            if isinstance(k, str) and isinstance(v, bool)
        }
        for person_id, flag in self._enabled.items():
            self._presence.set_enabled(person_id, flag)

        self._stack: AsyncExitStack | None = None
        self._client: matter.MatterClient | None = None
        self._thermostat: tuple[int, int] | None = None
        self._saved_snapshot: dict[str, Any] | None = saved or None
        self.failure_count = 0
        self._reachable: bool | None = None
        self._reading: matter.ThermostatState | None = None
        self._next_status_read = 0.0

    def settings(self) -> dict[str, Any]:
        """The effective settings: local.toml defaults under the saved overrides."""
        merged: dict[str, Any] = {
            "enabled": True,
            "comfort_f": self._config.comfort_f,
            "away_f": self._config.away_f,
            "away_minutes": self._config.away_minutes,
        }
        merged.update(self._overrides)
        return merged

    def apply_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Apply validated settings, persist them, and return the status document."""
        self._overrides.update({k: v for k, v in changes.items() if k in SETTING_KEYS})
        current = self.settings()
        self._presence.set_away_window(current["away_minutes"] * 60.0)
        self.climate.set_setpoints(current["comfort_f"], current["away_f"])
        try:
            self._save()
        except OSError:
            log.exception("could not save state")
        return self.publish()

    def status_document(self) -> dict[str, Any]:
        now = self._clock()
        settings = self.settings()
        s = self.climate.state
        reading = self._reading
        last_change = None
        if s.last_change is not None:
            c100 = s.last_change_c100
            if c100 is None:
                c100 = s.setpoint_c100
            reason = s.last_reason or ("home" if s.occupied else "away")
            last_change = {
                "at": s.last_change,
                "setpoint_f": (
                    None if c100 is None else matter.centi_celsius_to_fahrenheit(c100)
                ),
                "reason": reason,
            }
        mode = None
        if reading is not None and reading.system_mode is not None:
            mode = reading.system_mode_name
        return status.build_document(
            now=now,
            enabled=settings["enabled"],
            occupied=self._presence.occupied(),
            people=self._presence.statuses(),
            reachable=self._reachable,
            mode=mode,
            setpoint_f=reading.cooling_setpoint_f if reading else None,
            temperature_f=reading.local_temperature_f if reading else None,
            override=s.override,
            last_change=last_change,
            settings={k: settings[k] for k in SETTING_KEYS[1:]},
        )

    def publish(self) -> dict[str, Any]:
        """Build the status document and write it (a failed write only logs)."""
        document = self.status_document()
        if self._status is not None:
            try:
                self._status.write(document)
            except OSError:
                log.exception("could not write the status file")
        return document

    def occupied(self) -> bool | None:
        return self._presence.occupied()

    def set_enabled(self, person_id: str, enabled: bool) -> bool:
        """Turn a person on or off, and keep the choice across restarts."""
        if not self._presence.set_enabled(person_id, enabled):
            return False
        self._enabled[person_id] = enabled
        self._save()
        return True

    def counted_as(self, wifi_mac: str) -> str | None:
        """The name this address is already counted as, if any."""
        person = self._presence.person_for_mac(wifi_mac)
        return person.name if person else None

    def add_person(self, name: object, wifi_mac: str) -> dict[str, Any]:
        """Count a phone as a person, keep them, and return the status document."""
        mac = clean_mac(wifi_mac)
        existing = self.counted_as(mac)
        if existing is not None:
            raise PeopleError(f"This phone is already counted as {existing}.", 409)
        if len(self._added) >= MAX_ADDED:
            raise PeopleError(f"Up to {MAX_ADDED} people can be added here.")
        taken = [p.name for p in self._presence.all_people()]
        person = Person(clean_name(name, taken), True, mac, None, new_person_id())
        self._presence.add_person(person)
        self._added.append(person)
        self._save_quietly()
        return self.publish()

    def remove_person(self, person_id: object) -> dict[str, Any]:
        """Stop counting an added person. Configured people cannot be removed."""
        if not isinstance(person_id, str) or not self._presence.remove_person(
            person_id
        ):
            raise PeopleError("That person cannot be removed here.")
        self._added = [p for p in self._added if p.id != person_id]
        self._enabled.pop(person_id, None)
        self._save_quietly()
        return self.publish()

    def _save_quietly(self) -> None:
        try:
            self._save()
        except OSError:
            log.exception("could not save state")

    async def tick(self) -> None:
        """One pass of the loop. Never raises."""
        try:
            await asyncio.to_thread(self._presence.poll)
            self.climate.observe(self._presence.occupied())
            if self.settings()["enabled"] and self.climate.due():
                await self._drive_thermostat()
            elif self._clock() >= self._next_status_read:
                await self._refresh_thermostat()
        except Exception:
            log.exception("tick failed")
        try:
            self._save()
        except OSError:
            log.exception("could not save state")
        try:
            self.publish()
        except Exception:
            log.exception("could not publish status")

    async def run(self, stop: asyncio.Event | None = None) -> None:
        """Tick until ``stop`` is set, then release the controller."""
        stop = stop or asyncio.Event()
        log.info("homebody running")
        try:
            while not stop.is_set():
                await self.tick()
                try:
                    await asyncio.wait_for(stop.wait(), TICK_SECONDS)
                except TimeoutError:
                    pass
        finally:
            await self._disconnect()

    async def _refresh_thermostat(self) -> None:
        """Read the thermostat for the status file only; never commands anything."""
        self._next_status_read = self._clock() + STATUS_READ_SECONDS
        try:
            async with asyncio.timeout(EXCHANGE_TIMEOUT_SECONDS):
                client = await self._connected()
                node_id, endpoint = await self._find_thermostat(client)
                self._reading = await client.read_thermostat(
                    node_id, endpoint=endpoint
                )
                self._reachable = True
        except Exception as error:
            self._reachable = False
            log.warning("thermostat read failed (%s)", error)
            await self._disconnect()

    async def _drive_thermostat(self) -> None:
        self._next_status_read = self._clock() + STATUS_READ_SECONDS
        try:
            async with asyncio.timeout(EXCHANGE_TIMEOUT_SECONDS):
                await self._exchange()
        except Exception as error:
            self._reachable = False
            self.failure_count += 1
            self.climate.failed()
            log.warning("thermostat exchange failed (%s); will retry", error)
            await self._disconnect()

    async def _exchange(self) -> None:
        client = await self._connected()
        node_id, endpoint = await self._find_thermostat(client)
        state = await client.read_thermostat(node_id, endpoint=endpoint)
        self._reading = state
        self._reachable = True
        was_override = self.climate.state.override
        cmd = self.climate.decide(state)
        if self.climate.state.override and not was_override:
            log.info("thermostat changed by hand; holding until the next transition")
        if cmd is None:
            return
        self.climate.begin_command()
        await client.set_system_mode(node_id, cmd.mode, endpoint=endpoint)
        await client.set_cooling_setpoint_centi_c(
            node_id, cmd.setpoint_c100, endpoint=endpoint
        )
        self.climate.succeeded(cmd)
        log.info("thermostat set: cool at %d (hundredths of a degree C)",
                 cmd.setpoint_c100)

    async def _connected(self) -> matter.MatterClient:
        if self._client is None:
            stack = AsyncExitStack()
            connection = await stack.enter_async_context(self._connect())
            self._stack = stack
            self._client = matter.MatterClient(connection)
        return self._client

    async def _find_thermostat(self, client: matter.MatterClient) -> tuple[int, int]:
        if self._thermostat is None:
            nodes = await client.get_nodes()
            wanted = self._config.matter_node_id
            if wanted:
                nodes = [n for n in nodes if n["node_id"] == wanted]
            found = matter.find_thermostat(nodes)
            if found is None:
                raise matter.MatterError("no thermostat commissioned")
            self._thermostat = found
        return self._thermostat

    async def _disconnect(self) -> None:
        stack, self._stack = self._stack, None
        self._client = None
        self._thermostat = None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:
                log.debug("error closing the controller connection", exc_info=True)

    def _save(self) -> None:
        snapshot = {
            "climate": self.climate.state.to_dict(),
            "enabled": dict(sorted(self._enabled.items())),
            "people": to_saved(self._added),
            "settings": dict(sorted(self._overrides.items())),
        }
        if snapshot != self._saved_snapshot:
            self._store.save(snapshot)
            self._saved_snapshot = snapshot


def _clean_settings(raw: Any) -> dict[str, Any]:
    """Saved settings overrides; anything malformed is dropped."""
    if not isinstance(raw, dict):
        return {}
    clean: dict[str, Any] = {}
    for key in SETTING_KEYS:
        value = raw.get(key)
        if key == "enabled":
            if isinstance(value, bool):
                clean[key] = value
        elif isinstance(value, int) and not isinstance(value, bool):
            clean[key] = value
    return clean


def build(config: Config, *, url: str | None = None) -> Daemon:
    """The real daemon: real probes, a real WebSocket to the controller."""
    presence = PresenceTracker(
        config.people,
        wifi=NeighbourWifiProbe(),
        bluetooth=L2pingBluetoothProbe(config.bluetooth_adapter),
        clock=time.time,
        away_window=config.away_minutes * 60.0,
    )
    target = url or config.matter_url
    return Daemon(
        config,
        presence=presence,
        connect=lambda: matter.WebSocketConnection(target),
        status_writer=status.StatusWriter(config.status_dir),
    )
