"""The supervised garage service.

One asyncio task owns the board link, the door model, the alert rules, the
command gate and ``garage.json``. It never runs inside ``Daemon.tick`` and
calls into the daemon only through three hooks it is handed: ``occupied``,
``settings`` and ``request_publish``. The supervisor (``run``) catches every
exception, logs the exception type only, and restarts the loop with backoff, so
a broken garage cannot take climate down with it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from homebody import status
from homebody.audit import AuditLog
from homebody.config import Config, GarageConfig
from homebody.garage import GarageModel, GarageSettings, GarageStore
from homebody.garage_alerts import Alert, AlertRules
from homebody.garage_commands import CommandGate
from homebody.garage_link import GarageLink
from homebody.notify import Notifier

if TYPE_CHECKING:
    from homebody.daemon import Daemon

log = logging.getLogger(__name__)

# Supervisor backoff: 5 s, doubling to 5 min.
BACKOFF_START = 5.0
BACKOFF_MAX = 300.0
# A loop that stayed up this long before failing starts the backoff over.
HEALTHY_SECONDS = 300.0
# The loop wakes on a link push or this timer, whichever comes first.
TICK_SECONDS = 1.0
# After a failed delivery (the notifier has already retried), wait this long
# before offering the alert again, and give up after this many failed rounds.
RETRY_SECONDS = 30.0
MAX_DELIVERY_ROUNDS = 3
# garage.json: how often a change that is only a newer ``last_seen`` is saved,
# and the wait after a failed write.
SAVE_QUIET_SECONDS = 60.0
SAVE_RETRY_SECONDS = 30.0

# Door states that are recorded in the audit log. The moving states are fed to
# the command gate (so its attribution is consumed) but not recorded, because
# they are followed by the end state.
_RECORDED = {"open": "opened", "closed": "closed", "stopped": "stopped"}

Pause = Callable[[asyncio.Event, float], Awaitable[None]]


async def _pause(stop: asyncio.Event, seconds: float) -> None:
    """Sleep for the backoff, ending early when ``stop`` is set."""
    try:
        await asyncio.wait_for(stop.wait(), seconds)
    except TimeoutError:
        pass


class GarageService:
    def __init__(
        self,
        config: GarageConfig,
        *,
        link: GarageLink,
        notifier: Notifier | None,
        audit: AuditLog,
        store: GarageStore,
        occupied: Callable[[], bool | None],
        settings: Callable[[], GarageSettings],
        request_publish: Callable[[], None],
        clock: Callable[[], float],
        tick_seconds: float = TICK_SECONDS,
        pause: Pause | None = None,
    ) -> None:
        self._config = config
        self._link = link
        self._notifier = notifier
        self._audit = audit
        self._store = store
        self._occupied = occupied
        self._settings = settings
        self._request_publish = request_publish
        self._clock = clock
        self._tick = tick_seconds
        self._pause = pause or _pause
        self.timeout_seconds = config.timeout_seconds

        self._model = GarageModel(clock, store.load())
        self._rules = AlertRules(self._model, clock)
        self.gate = CommandGate(
            model=self._model,
            link=link,
            audit=audit,
            notify=notifier.send if notifier is not None else None,
            settings=settings,
            clock=clock,
            close_wait=config.close_wait,
            open_wait=config.open_wait,
            note_wait=config.note_wait,
        )
        self._wake = asyncio.Event()
        self._sending: dict[str, asyncio.Task[None]] = {}
        self._rounds: dict[str, int] = {}
        self._retry_at: dict[str, float] = {}
        self._door_before: str = self._model.last_known_state or "unknown"
        self._published: tuple[str, bool] | None = None
        self._saved: dict[str, Any] | None = None
        self._saved_at: float | None = None
        self._save_failed_at: float | None = None

    # the surface the API and the daemon use

    def snapshot(self) -> dict[str, Any] | None:
        return status.build_garage(
            self._model,
            self._settings(),
            self._audit.recent(),
            self._config.actors,
            self._clock(),
        )

    def actor_tag(self, email: str) -> str:
        return self._audit.tag(email)

    # supervisor

    async def run(self, stop: asyncio.Event) -> None:
        """Run the loop until ``stop``; restart it on any failure. Never raises
        (apart from cancellation, which is how a caller ends it early)."""
        delay = BACKOFF_START
        watcher = asyncio.create_task(self._watch(stop))
        try:
            while not stop.is_set():
                began = time.monotonic()
                try:
                    await self._loop(stop)
                except Exception as error:  # noqa: BLE001 - the supervisor's job
                    log.warning("garage loop failed: %s", type(error).__name__)
                if stop.is_set():
                    break
                if time.monotonic() - began >= HEALTHY_SECONDS:
                    delay = BACKOFF_START
                try:
                    await self._pause(stop, delay)
                except Exception as error:  # noqa: BLE001
                    log.warning("garage backoff failed: %s", type(error).__name__)
                delay = min(delay * 2, BACKOFF_MAX)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    async def _watch(self, stop: asyncio.Event) -> None:
        await stop.wait()
        self._wake.set()

    # the loop

    async def _loop(self, stop: asyncio.Event) -> None:
        link_task = asyncio.create_task(
            self._link.run(self._on_state, self._on_connected)
        )
        link_task.add_done_callback(lambda _t: self._wake.set())
        try:
            while not stop.is_set():
                if link_task.done():
                    link_task.result()
                    raise RuntimeError("the link stopped")
                self._wake.clear()
                self._pass()
                try:
                    await asyncio.wait_for(self._wake.wait(), self._tick)
                except TimeoutError:
                    pass
        finally:
            link_task.cancel()
            await asyncio.gather(link_task, return_exceptions=True)
            for task in list(self._sending.values()):
                task.cancel()
            await asyncio.gather(*self._sending.values(), return_exceptions=True)
            self._sending.clear()
            self._save(force=True)

    def _on_state(self, state: Any) -> None:
        self._model.state_received(state)
        self._wake.set()

    def _on_connected(self, connected: bool) -> None:
        self._model.link_changed(connected)
        self._wake.set()

    def _pass(self) -> None:
        now = self._clock()
        changed = False
        for _at, state in self._model.pop_changes():
            changed = True
            origin, actor = self.gate.attribute_change(state)
            event = _RECORDED.get(state)
            if event is not None:
                self._audit.record(
                    event,
                    origin=origin,
                    actor=actor,
                    door=self._door_before,
                    result="seen",
                    kind="door",
                )
            self._door_before = state
        key = (self._model.state, self._model.connected)
        if changed or key != self._published:
            self._published = key
            self._publish()
        self._alerts(now)
        self._save(force=changed)

    def _publish(self) -> None:
        try:
            self._request_publish()
        except Exception as error:  # noqa: BLE001
            log.warning("garage publish request failed: %s", type(error).__name__)

    # alerts

    def _occupancy(self) -> bool | None:
        try:
            return self._occupied()
        except Exception as error:  # noqa: BLE001
            log.warning("garage occupancy read failed: %s", type(error).__name__)
            return None

    def _alerts(self, now: float) -> None:
        notifier = self._notifier
        offered = self._rules.evaluate(
            self._occupancy(), self._settings(), notifier is not None
        )
        if notifier is None:
            return
        for alert in offered:
            kind = alert.kind
            if kind in self._sending:
                continue
            if now < self._retry_at.get(kind, 0.0):
                continue
            self._sending[kind] = asyncio.create_task(self._deliver(alert, notifier))

    async def _deliver(self, alert: Alert, notifier: Notifier) -> None:
        """Send one alert off the loop's path, then record how it went."""
        kind = alert.kind
        try:
            try:
                ok = bool(await notifier.send(alert))
            except Exception as error:  # noqa: BLE001
                log.warning("garage alert send failed: %s", type(error).__name__)
                ok = False
            if ok:
                self._rounds.pop(kind, None)
                self._retry_at.pop(kind, None)
                self._rules.mark_sent(alert)
                return
            rounds = self._rounds.get(kind, 0) + 1
            if rounds >= MAX_DELIVERY_ROUNDS:
                self._rounds.pop(kind, None)
                self._retry_at.pop(kind, None)
                self._rules.mark_dropped(alert)
            else:
                self._rounds[kind] = rounds
                self._retry_at[kind] = self._clock() + RETRY_SECONDS
        finally:
            self._sending.pop(kind, None)
            self._wake.set()

    # the state file

    def _save(self, *, force: bool = False) -> None:
        """Write ``garage.json`` when it changed; a failure logs and is ignored."""
        now = self._clock()
        data = self._model.to_saved()
        before = self._saved
        if before is not None and data == before:
            return
        quiet = (
            before is not None
            and {k: v for k, v in data.items() if k != "last_seen"}
            == {k: v for k, v in before.items() if k != "last_seen"}
        )
        if quiet and self._saved_at is not None and 0 <= now - self._saved_at < (
            SAVE_QUIET_SECONDS
        ):
            return
        failed = self._save_failed_at
        if not force and failed is not None and 0 <= now - failed < SAVE_RETRY_SECONDS:
            return
        try:
            self._store.save(data)
        except Exception as error:  # noqa: BLE001
            self._save_failed_at = now
            log.warning("garage state could not be saved: %s", type(error).__name__)
            return
        self._save_failed_at = None
        self._saved = data
        self._saved_at = now


def build(
    config: Config,
    daemon: Daemon,
    *,
    link_factory: Callable[[GarageConfig], GarageLink] | None = None,
) -> GarageService | None:
    """The real service, or None when the module is off or cannot start."""
    garage = config.garage
    if garage is None:
        if config.garage_error:
            log.warning("garage is off: %s", config.garage_error)
        return None
    try:
        factory = link_factory or _default_link
        link = factory(garage)
    except Exception as error:  # noqa: BLE001 - a module that cannot start stays off
        log.warning("garage link could not be built: %s", type(error).__name__)
        return None
    notifier = Notifier(garage.ntfy) if garage.ntfy is not None else None
    directory = config.state_dir
    return GarageService(
        garage,
        link=link,
        notifier=notifier,
        audit=AuditLog(directory, time.time),
        store=GarageStore(directory),
        occupied=daemon.occupied,
        settings=daemon.garage_settings,
        request_publish=daemon.request_publish,
        clock=time.time,
    )


def _default_link(garage: GarageConfig) -> GarageLink:
    """The ESPHome adapter, imported only here so tests never load it."""
    from homebody.garage_link import EsphomeLink

    return EsphomeLink(
        garage.host, garage.port, garage.noise_psk, garage.entity_names
    )
