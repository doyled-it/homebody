"""The link to the garage door opener board: types and the adapter Protocol.

The board is a Konnected blaQ running ESPHome. This module holds only the
shapes the rest of the garage code depends on, so the model, alerts and
command gate can be tested against a fake link. The real adapter is added
alongside later.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

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
