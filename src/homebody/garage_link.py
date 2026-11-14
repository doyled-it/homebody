"""The link to the garage door opener board: types and the adapter Protocol.

The board is a Konnected blaQ running ESPHome. This module holds only the
shapes the rest of the garage code depends on, so the model, alerts and
command gate can be tested against a fake link. ``EsphomeLink`` is the real
adapter, built on ``aioesphomeapi``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import aioesphomeapi
import aioesphomeapi.core

log = logging.getLogger(__name__)

# ESPHome entity names on the stock blaQ firmware, keyed by role. These can be
# overridden (partially) from local.toml.
DEFAULT_ENTITY_NAMES: dict[str, str] = {
    "cover": "Garage Door",
    "obstruction": "Obstruction",
    "lockout": "Lock",
    "light": "Garage Light",
    "motion": "Motion",
}


@dataclass(frozen=True)
class LinkState:
    """The merged state of the board's entities.

    ``position`` runs from 0.0 (closed) to 1.0 (open). ``has_position`` is the
    cover's ``supports_position``; without it position is only 0.0 or 1.0.
    ``lockout`` true means the opener's remote lockout is on. ``None`` means
    not yet known since the last connect.
    """

    operation: Literal["idle", "opening", "closing"] | None = None
    position: float | None = None
    has_position: bool = False
    obstruction: bool | None = None
    lockout: bool | None = None
    light: bool | None = None


class LinkError(Exception):
    """A command could not be sent: not connected, or no answer in time."""


class GarageLink(Protocol):
    async def run(
        self,
        on_state: Callable[[LinkState], None],
        on_connected: Callable[[bool], None],
    ) -> None:
        """Connect and keep reconnecting until cancelled.

        Call ``on_connected(True)`` after the entities are listed and
        subscribed, and ``on_connected(False)`` on every drop. After a drop the
        merged state is reset, so the next push never carries stale fields.
        Call ``on_state`` with the full merged state after each push. Never
        raise except ``CancelledError``.
        """
        ...

    async def send(self, action: Literal["open", "close"]) -> None:
        """Ask the door for an end state.

        Raises ``LinkError`` when not connected or after a 5 s timeout.
        """
        ...


# The command is written to the socket at once; this bounds how long it may
# take to be handed over before the caller is told it failed.
SEND_TIMEOUT = 5.0

_OPERATIONS: dict[Any, Literal["idle", "opening", "closing"]] = {
    aioesphomeapi.CoverOperation.IDLE: "idle",
    aioesphomeapi.CoverOperation.IS_OPENING: "opening",
    aioesphomeapi.CoverOperation.IS_CLOSING: "closing",
}


def _reason(error: BaseException) -> str:
    """A short reason for the log. Never the error text, which can carry the
    board's address."""
    if isinstance(error, aioesphomeapi.InvalidAuthAPIError):
        return "authentication failed"
    if isinstance(error, aioesphomeapi.HandshakeAPIError):
        return "encryption handshake failed"
    if isinstance(error, aioesphomeapi.ResolveAPIError):
        return "address could not be resolved"
    if isinstance(error, (TimeoutError, aioesphomeapi.core.TimeoutAPIError)):
        return "timed out"
    if isinstance(error, aioesphomeapi.APIConnectionError):
        cause = error.__cause__ or error.__context__
        if isinstance(cause, ConnectionRefusedError):
            return "connection refused"
        return "connection failed"
    return "unexpected error"


class EsphomeLink:
    """The ``GarageLink`` for a Konnected blaQ board over the ESPHome native API.

    ``aioesphomeapi``'s ``ReconnectLogic`` owns connecting, backoff and the
    keepalive. On each connect the entities are listed and matched by name
    (case-insensitive) to the cover, obstruction, lockout, light and motion
    roles, then states are subscribed. Pushes are merged into one ``LinkState``;
    a drop resets it.
    """

    def __init__(
        self,
        host: str,
        port: int,
        noise_psk: str | None,
        entity_names: Mapping[str, str],
        *,
        client_factory: Callable[..., Any] = aioesphomeapi.APIClient,
    ) -> None:
        names = {**DEFAULT_ENTITY_NAMES, **entity_names}
        self._names = {role: name.casefold() for role, name in names.items()}
        self._make_client = lambda: client_factory(
            host, port, None, client_info="homebody", noise_psk=noise_psk
        )
        # Built in ``run``: the client needs a running event loop.
        self._client: Any = None
        self._on_state: Callable[[LinkState], None] = lambda _s: None
        self._on_connected: Callable[[bool], None] = lambda _c: None
        self._connected = False
        self._cover_key: int | None = None
        self._roles: dict[int, str] = {}
        self._state = LinkState()
        self._last_problem: str | None = None
        self._missing_logged = False

    async def run(
        self,
        on_state: Callable[[LinkState], None],
        on_connected: Callable[[bool], None],
    ) -> None:
        self._on_state = on_state
        self._on_connected = on_connected
        # Keep the library's connection errors visible in the local journal
        # (install-day bring-up) but drop its chatty INFO and DEBUG lines. Our
        # own lines carry a reason only.
        logging.getLogger("aioesphomeapi").setLevel(logging.WARNING)
        logic: Any = None
        try:
            self._client = self._make_client()
            logic = aioesphomeapi.ReconnectLogic(
                client=self._client,
                on_connect=self._handle_connect,
                on_disconnect=self._handle_disconnect,
                on_connect_error=self._handle_connect_error,
            )
            await logic.start()
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - run never raises
            log.warning("garage link stopped: %s", _reason(error))
        finally:
            await self._shutdown(logic)

    async def _shutdown(self, logic: Any) -> None:
        try:
            if logic is not None:
                await logic.stop()
                await self._client.disconnect()
        except Exception as error:  # noqa: BLE001
            log.debug("garage link shutdown: %s", type(error).__name__)
        self._lost()

    async def send(self, action: Literal["open", "close"]) -> None:
        key = self._cover_key
        if not self._connected or key is None or self._client is None:
            raise LinkError("not connected")
        position = 1.0 if action == "open" else 0.0
        try:
            # cover_command only writes to the transport, so this bound is the
            # contract more than a guard; the state push confirms the move.
            async with asyncio.timeout(SEND_TIMEOUT):
                self._client.cover_command(key=key, position=position)
        except (aioesphomeapi.APIConnectionError, TimeoutError) as error:
            raise LinkError(_reason(error)) from None

    # ReconnectLogic callbacks

    async def _handle_connect(self) -> None:
        try:
            entities, _services = await self._client.list_entities_services()
            self._match(entities)
            if self._cover_key is None:
                if not self._missing_logged:
                    self._missing_logged = True
                    log.warning(
                        "garage board has no cover entity with the configured name"
                    )
                await self._client.disconnect()
                return
            self._state = LinkState(has_position=self._has_position)
            self._client.subscribe_states(self._push)
        except Exception as error:  # noqa: BLE001
            self._problem(_reason(error))
            await self._client.disconnect()
            return
        self._missing_logged = False
        if self._last_problem is not None:
            log.info("garage board connected")
            self._last_problem = None
        self._connected = True
        self._on_connected(True)

    async def _handle_disconnect(self, expected: bool) -> None:
        self._lost()

    async def _handle_connect_error(self, error: Exception) -> None:
        self._problem(_reason(error))

    # internals

    def _problem(self, reason: str) -> None:
        if reason != self._last_problem:
            self._last_problem = reason
            log.warning("garage board link failed: %s", reason)

    def _lost(self) -> None:
        """The session ended: forget the keys and the merged state."""
        was = self._connected
        self._connected = False
        self._cover_key = None
        self._roles = {}
        self._state = LinkState()
        if was:
            try:
                self._on_connected(False)
            except Exception as error:  # noqa: BLE001
                log.warning("garage link callback failed: %s", type(error).__name__)

    def _match(self, entities: list[Any]) -> None:
        self._cover_key = None
        self._has_position = False
        self._roles = {}
        for entity in entities:
            name = entity.name.casefold()
            if isinstance(entity, aioesphomeapi.CoverInfo):
                if name == self._names["cover"] and self._cover_key is None:
                    self._cover_key = entity.key
                    self._has_position = entity.supports_position
                    self._roles[entity.key] = "cover"
            elif isinstance(entity, aioesphomeapi.BinarySensorInfo):
                if name == self._names["obstruction"]:
                    self._roles[entity.key] = "obstruction"
                elif name == self._names["motion"]:
                    self._roles[entity.key] = "motion"
            elif isinstance(entity, aioesphomeapi.LockInfo):
                if name == self._names["lockout"]:
                    self._roles[entity.key] = "lockout"
            elif isinstance(entity, aioesphomeapi.LightInfo):
                if name == self._names["light"]:
                    self._roles[entity.key] = "light"

    def _push(self, state: Any) -> None:
        role = self._roles.get(state.key)
        merged = self._state
        if role == "cover" and isinstance(state, aioesphomeapi.CoverState):
            merged = dataclasses.replace(
                merged,
                operation=_OPERATIONS.get(state.current_operation, "idle"),
                position=float(state.position),
            )
        elif role == "obstruction" and isinstance(
            state, aioesphomeapi.BinarySensorState
        ):
            merged = dataclasses.replace(
                merged, obstruction=None if state.missing_state else state.state
            )
        elif role == "lockout" and isinstance(state, aioesphomeapi.LockEntityState):
            merged = dataclasses.replace(
                merged, lockout=state.state == aioesphomeapi.LockState.LOCKED
            )
        elif role == "light" and isinstance(state, aioesphomeapi.LightState):
            merged = dataclasses.replace(merged, light=bool(state.state))
        else:
            return  # motion, or an entity we do not use
        self._state = merged
        try:
            self._on_state(merged)
        except Exception as error:  # noqa: BLE001 - never break the client's reader
            log.warning("garage link callback failed: %s", type(error).__name__)
