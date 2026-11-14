"""The garage command gate: every door command passes through here.

No HTTP concepts: the API maps ``CommandError.status`` to a response. A command
is a request for an end state, never a toggle, and is never retried. Waits are
asyncio sleeps, so a 30 s close confirmation blocks nothing else.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from homebody.audit import AuditLog
from homebody.garage import GarageModel, GarageSettings
from homebody.garage_alerts import Alert
from homebody.garage_link import GarageLink, LinkError

log = logging.getLogger(__name__)

NONCE_SECONDS = 30
MIN_GAP_SECONDS = 10.0
TUNNEL_PER_HOUR = 6
HOUR = 3600.0
INTENTS_PER_MINUTE = 10
MINUTE = 60.0
LATE_CONFIRM_SECONDS = 60.0
ATTRIBUTION_SECONDS = 120.0
POLL_SECONDS = 0.02

Origin = Literal["home", "tunnel", "opener"]

NO_MOVEMENT = "No movement seen yet. Check the card before trying again."
BAD_NONCE = "That request expired. Start again."


class CommandError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class CommandGate:
    def __init__(
        self,
        *,
        model: GarageModel,
        link: GarageLink,
        audit: AuditLog,
        notify: Callable[[Alert], Awaitable[bool]] | None,
        settings: Callable[[], GarageSettings],
        clock: Callable[[], float],
        close_wait: float = 30.0,
        open_wait: float = 10.0,
        note_wait: float = 5.0,
    ) -> None:
        self._model = model
        self._link = link
        self._audit = audit
        self._notify = notify
        self._settings = settings
        self._clock = clock
        self._close_wait = close_wait
        self._open_wait = open_wait
        self._note_wait = note_wait
        self._nonces: dict[str, tuple[str, float]] = {}
        self._last_accept: float | None = None
        self._tunnel_accepts: list[float] = []
        self._intents: dict[str, list[float]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._late: tuple[str, Origin, str | None, float] | None = None
        self._attrib: tuple[Origin, str | None, float] | None = None

    # intent

    def intent(self, action: str, origin: str, actor: str | None) -> dict[str, Any]:
        if action not in ("open", "close"):
            raise CommandError(400, "Unknown action.")
        now = self._clock()
        self._nonces = {
            n: (a, exp) for n, (a, exp) in self._nonces.items() if exp > now
        }
        caller = actor if origin != "home" and actor else origin
        recent = [t for t in self._intents.get(caller, []) if 0 <= now - t < MINUTE]
        if len(recent) >= INTENTS_PER_MINUTE:
            self._intents[caller] = recent
            raise CommandError(429, "Too many requests. Wait a moment.")
        recent.append(now)
        self._intents[caller] = recent
        nonce = secrets.token_urlsafe(16)
        self._nonces[nonce] = (action, now + NONCE_SECONDS)
        return {
            "nonce": nonce,
            "expires_in": NONCE_SECONDS,
            "confirm_required": origin != "home",
        }

    # command

    def _audit_line(
        self, action: str, origin: Origin, actor: str | None, result: str
    ) -> None:
        self._audit.record(
            action,
            origin=origin,
            actor=actor,
            door=self._model.state,
            result=result,
        )

    def _refuse(
        self, action: str, origin: Origin, actor: str | None, status: int,
        reason: str, message: str,
    ) -> CommandError:
        self._audit_line(action, origin, actor, f"refused: {reason}")
        return CommandError(status, message)

    def _take_nonce(self, action: str, nonce: object) -> bool:
        if not isinstance(nonce, str):
            return False
        held = self._nonces.pop(nonce, None)
        if held is None:
            return False
        wanted, expires = held
        return wanted == action and self._clock() < expires

    async def command(
        self, action: str, nonce: str, origin: str, actor: str | None
    ) -> Literal["sent", "noop"]:
        where: Origin = "home" if origin == "home" else "tunnel"
        if action not in ("open", "close") or not self._take_nonce(action, nonce):
            raise self._refuse(
                action if isinstance(action, str) else "unknown",
                where, actor, 400, "bad nonce", BAD_NONCE,
            )
        tunnel = where == "tunnel"
        if tunnel and not self._settings().remote:
            raise self._refuse(
                action, where, actor, 403, "remote off",
                "Remote control is turned off.",
            )
        if tunnel and self._notify is None:
            raise self._refuse(
                action, where, actor, 409, "no notifications",
                "Remote control needs notifications set up first.",
            )
        model = self._model
        state = model.state
        if state == "unknown":
            if not model.connected:
                raise self._refuse(
                    action, where, actor, 503, "link down",
                    "The garage board is offline.",
                )
            raise self._refuse(
                action, where, actor, 409, "state unknown",
                "The door state is not known yet. Try again in a moment.",
            )
        if model.lockout:
            raise self._refuse(
                action, where, actor, 423, "lockout",
                "Remote lockout is on at the opener. Turn it off at the wall panel.",
            )
        if action == "close" and model.obstruction:
            raise self._refuse(
                action, where, actor, 409, "obstruction",
                "The door sensor reports an obstruction. Clear it first.",
            )
        if (action == "open" and state in ("open", "opening")) or (
            action == "close" and state in ("closed", "closing")
        ):
            self._audit_line(action, where, actor, "noop")
            return "noop"
        if (action == "open" and state == "closing") or (
            action == "close" and state == "opening"
        ):
            raise self._refuse(
                action, where, actor, 409, "moving the other way",
                "The door is moving the other way. Wait for it to stop.",
            )
        now = self._clock()
        if self._last_accept is not None and self._last_accept > now:
            self._last_accept = now
        if self._last_accept is not None and now - self._last_accept < MIN_GAP_SECONDS:
            raise self._refuse(
                action, where, actor, 429, "rate limited",
                "Too many commands. Wait a few seconds.",
            )
        recent = [t for t in self._tunnel_accepts if 0 <= now - t < HOUR]
        if tunnel and len(recent) >= TUNNEL_PER_HOUR:
            self._tunnel_accepts = recent
            raise self._refuse(
                action, where, actor, 429, "rate limited",
                "Too many remote commands this hour. Try again later.",
            )
        self._last_accept = now
        if tunnel:
            recent.append(now)
        self._tunnel_accepts = recent

        note: asyncio.Task[bool] | None = None
        if tunnel:
            note = self._start_note(
                Alert(
                    "remote_note",
                    f"Garage {action} requested from away",
                    f"A remote {action} was accepted. Door was {state}.",
                    "default",
                ),
                action, where, actor,
            )
        if note is not None:
            await asyncio.wait({note}, timeout=self._note_wait)

        wait = self._close_wait if action == "close" else self._open_wait
        if tunnel:
            # the mark lapses on its own if the door never moves for this command
            model.note_remote_command(window=wait + self._note_wait + 5.0)
        try:
            await self._link.send(action)  # type: ignore[arg-type]
        except LinkError:
            model.clear_remote_command()
            await self._followup(
                action, where, actor, "The board refused the command."
            )
            raise self._refuse(
                action, where, actor, 503, "board refused",
                "The garage board did not accept the command.",
            ) from None
        self._audit_line(action, where, actor, "sent")
        self._attrib = (where, actor, self._clock() + ATTRIBUTION_SECONDS)

        if await self._saw_movement(action, wait):
            if action == "close":
                model.note_command("close")
            self._audit_line(action, where, actor, "confirmed")
            return "sent"
        model.clear_remote_command()
        self._audit_line(action, where, actor, "no confirmation")
        self._attrib = None
        self._late = (action, where, actor, self._clock() + LATE_CONFIRM_SECONDS)
        await self._followup(
            action, where, actor, "The board did not report any movement."
        )
        raise CommandError(504, NO_MOVEMENT)

    async def _saw_movement(self, action: str, wait: float) -> bool:
        moving, end = ("opening", "open") if action == "open" else ("closing", "closed")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        while True:
            if self._model.state in (moving, end):
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(POLL_SECONDS)

    # notes

    def _start_note(
        self, alert: Alert, action: str, origin: Origin, actor: str | None
    ) -> asyncio.Task[bool]:
        async def deliver() -> bool:
            try:
                assert self._notify is not None
                ok = bool(await self._notify(alert))
            except Exception as exc:
                log.warning("remote note failed: %s", type(exc).__name__)
                ok = False
            if not ok:
                self._audit_line(action, origin, actor, "note failed")
            return ok

        task = asyncio.ensure_future(deliver())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _followup(
        self, action: str, origin: Origin, actor: str | None, why: str
    ) -> None:
        if origin != "tunnel" or self._notify is None:
            return
        task = self._start_note(
            Alert(
                "remote_note_followup",
                f"Garage {action} request not confirmed",
                f"{why} Check the door.",
                "high",
            ),
            action, origin, actor,
        )
        await asyncio.wait({task}, timeout=self._note_wait)

    # attribution

    def attribute_change(
        self, new_state: str
    ) -> tuple[Literal["home", "tunnel", "opener"], str | None]:
        """Who caused a door change, for its audit line."""
        now = self._clock()
        late = self._late
        if late is not None:
            action, origin, actor, until = late
            moving, end = (
                ("opening", "open") if action == "open" else ("closing", "closed")
            )
            if now > until:
                self._late = None
            elif new_state in (moving, end):
                self._late = None
                self._attrib = (origin, actor, now + ATTRIBUTION_SECONDS)
                self._audit_line(action, origin, actor, "late confirm")
                return origin, actor
        attrib = self._attrib
        if attrib is not None:
            origin, actor, until = attrib
            if now <= until:
                return origin, actor
            self._attrib = None
        return "opener", None
