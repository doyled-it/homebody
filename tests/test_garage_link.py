"""The real ESPHome adapter, run against the plaintext loopback board.

Each test starts a ``LoopbackBoard`` on a local port and drives a real
``EsphomeLink`` (the real ``aioesphomeapi`` client and ``ReconnectLogic``) over
it. The noise handshake is not reimplemented here; a test asserts the key
reaches the client factory instead.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from homebody import garage_service
from homebody.config import GarageConfig
from homebody.garage_link import DEFAULT_ENTITY_NAMES, EsphomeLink, LinkError, LinkState
from loopback_esphome import LoopbackBoard

HOST = "127.0.0.1"
FAKE_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


class Probe:
    """Collects what the link reports, in order."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []
        self.states: list[LinkState] = []

    def on_state(self, state: LinkState) -> None:
        self.states.append(state)
        self.events.append(("state", state))

    def on_connected(self, connected: bool) -> None:
        self.events.append(("connected", connected))

    @property
    def connected_events(self) -> list[bool]:
        return [value for kind, value in self.events if kind == "connected"]

    @property
    def last(self) -> LinkState | None:
        return self.states[-1] if self.states else None


def _own_text(caplog: pytest.LogCaptureFixture) -> str:
    """The log text from homebody's own loggers. The library's lines may carry
    the board address; that is acceptable in the hub's local journal."""
    return "\n".join(
        r.getMessage() for r in caplog.records if r.name.startswith("homebody")
    )


async def until(check: Callable[[], bool], timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.01)


def run_with_board(
    body: Callable[[LoopbackBoard, EsphomeLink, Probe], Awaitable[None]],
    *,
    board: LoopbackBoard | None = None,
    start_link: bool = True,
) -> None:
    async def main() -> None:
        the_board = board or LoopbackBoard()
        await the_board.start()
        link = EsphomeLink(HOST, the_board.port, None, DEFAULT_ENTITY_NAMES)
        probe = Probe()
        task = (
            asyncio.create_task(link.run(probe.on_state, probe.on_connected))
            if start_link
            else None
        )
        try:
            await body(the_board, link, probe)
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await the_board.close()

    asyncio.run(main())


def test_connects_lists_entities_and_reports_initial_state() -> None:
    async def body(board: LoopbackBoard, link: EsphomeLink, probe: Probe) -> None:
        board.binary["Obstruction"] = False
        await until(lambda: probe.last is not None and probe.last.light is not None)
        await until(lambda: probe.last is not None and probe.last.lockout is not None)
        assert probe.connected_events[0] is True
        assert probe.events[0] == ("connected", True)
        assert probe.last == LinkState(
            operation="idle",
            position=0.0,
            has_position=True,
            obstruction=False,
            lockout=False,
            light=False,
        )

    run_with_board(body)


@pytest.mark.parametrize(
    ("shape", "has_position"), [("position", True), ("binary", False)]
)
def test_cover_push_maps_operation_and_position_for_both_shapes(
    shape: str, has_position: bool
) -> None:
    async def body(board: LoopbackBoard, link: EsphomeLink, probe: Probe) -> None:
        await until(lambda: probe.last is not None and probe.last.position == 0.0)
        board.push_cover(0.5 if has_position else 1.0, "opening")
        await until(
            lambda: probe.last is not None and probe.last.operation == "opening"
        )
        assert probe.last is not None
        assert probe.last.has_position is has_position
        assert probe.last.position == (0.5 if has_position else 1.0)
        board.push_cover(1.0, "idle")
        await until(lambda: probe.last is not None and probe.last.operation == "idle")
        assert probe.last.position == 1.0
        board.push_cover(0.0, "closing")
        await until(
            lambda: probe.last is not None and probe.last.operation == "closing"
        )

    run_with_board(body, board=LoopbackBoard(shape=shape))


def test_lock_locked_maps_to_lockout_true() -> None:
    async def body(board: LoopbackBoard, link: EsphomeLink, probe: Probe) -> None:
        await until(lambda: probe.last is not None and probe.last.lockout is False)
        board.push_lock(True)
        await until(lambda: probe.last is not None and probe.last.lockout is True)
        board.push_lock(False)
        await until(lambda: probe.last is not None and probe.last.lockout is False)

    run_with_board(body)


def test_obstruction_push_reaches_link_state() -> None:
    async def body(board: LoopbackBoard, link: EsphomeLink, probe: Probe) -> None:
        await until(lambda: probe.last is not None and probe.last.obstruction is False)
        board.push_binary("Obstruction", True)
        await until(lambda: probe.last is not None and probe.last.obstruction is True)
        # The merged state keeps the door fields from the earlier push.
        assert probe.last is not None
        assert probe.last.position == 0.0

    run_with_board(body)


def test_close_sends_position_0_never_toggle_and_open_sends_position_1() -> None:
    async def body(board: LoopbackBoard, link: EsphomeLink, probe: Probe) -> None:
        await until(lambda: probe.connected_events == [True])
        await link.send("close")
        await until(lambda: len(board.commands) == 1)
        await link.send("open")
        await until(lambda: len(board.commands) == 2)
        close, open_ = board.commands
        assert (close["has_position"], close["position"]) == (True, 0.0)
        assert (open_["has_position"], open_["position"]) == (True, 1.0)
        for command in board.commands:
            assert command["stop"] is False
            assert command["has_legacy_command"] is False

    run_with_board(body)


def test_server_drop_calls_on_connected_false_then_reconnects() -> None:
    async def body(board: LoopbackBoard, link: EsphomeLink, probe: Probe) -> None:
        await until(lambda: probe.connected_events == [True])
        await until(lambda: probe.last is not None and probe.last.position == 0.0)
        # While the link is down the board forgets the door, so a stale merged
        # state would show through after the reconnect.
        board.cover = None
        board.binary["Obstruction"] = True
        await board.drop()
        await until(lambda: probe.connected_events[:2] == [True, False])
        await until(lambda: probe.connected_events == [True, False, True])
        await until(lambda: probe.last is not None and probe.last.obstruction is True)
        assert probe.last is not None
        assert probe.last.position is None
        assert probe.last.operation is None

    run_with_board(body)


def test_send_when_disconnected_raises_link_error() -> None:
    async def body(board: LoopbackBoard, link: EsphomeLink, probe: Probe) -> None:
        with pytest.raises(LinkError):
            await link.send("close")
        assert board.commands == []

    run_with_board(body, start_link=False)

    async def after_drop(board: LoopbackBoard, link: EsphomeLink, probe: Probe) -> None:
        await until(lambda: probe.connected_events == [True])
        await board.close()  # drops the client and stops listening
        await until(lambda: probe.connected_events[:2] == [True, False])
        with pytest.raises(LinkError):
            await link.send("open")

    run_with_board(after_drop)


def test_missing_cover_entity_stays_disconnected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def body(board: LoopbackBoard, link: EsphomeLink, probe: Probe) -> None:
        await asyncio.sleep(0.5)
        assert probe.connected_events == []
        assert probe.states == []
        with pytest.raises(LinkError):
            await link.send("close")

    with caplog.at_level(logging.INFO):
        run_with_board(body, board=LoopbackBoard(has_cover=False))
    cover_lines = [r for r in caplog.records if "cover" in r.getMessage()]
    assert len(cover_lines) == 1


def test_noise_psk_is_passed_to_the_client_factory() -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def factory(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        return object()

    async def start(psk: str | None) -> None:
        link = EsphomeLink(
            HOST, 6053, psk, DEFAULT_ENTITY_NAMES, client_factory=factory
        )
        task = asyncio.create_task(link.run(lambda _s: None, lambda _c: None))
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(start(FAKE_KEY))
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[:2] == (HOST, 6053)
    assert kwargs["noise_psk"] == FAKE_KEY


def test_auth_failure_logs_a_reason_and_never_the_key_or_host(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def body(board: LoopbackBoard, link: EsphomeLink, probe: Probe) -> None:
        await until(
            lambda: any(
                "authentication failed" in r.getMessage() for r in caplog.records
            )
        )

    with caplog.at_level(logging.DEBUG):
        run_with_board(body, board=LoopbackBoard(auth_fails=True))
    assert _own_text(caplog) != ""
    assert HOST not in _own_text(caplog)
    assert "localhost" not in _own_text(caplog)


def test_encrypted_client_against_a_plaintext_board_logs_a_reason_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def main() -> None:
        board = LoopbackBoard()
        await board.start()
        link = EsphomeLink(HOST, board.port, FAKE_KEY, DEFAULT_ENTITY_NAMES)
        probe = Probe()
        task = asyncio.create_task(link.run(probe.on_state, probe.on_connected))
        try:
            await until(
                lambda: any(
                    "connection failed" in r.getMessage() for r in caplog.records
                )
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await board.close()

    with caplog.at_level(logging.DEBUG):
        asyncio.run(main())
    assert FAKE_KEY not in _own_text(caplog)
    assert HOST not in _own_text(caplog)


def test_reasons_never_carry_the_error_text() -> None:
    from aioesphomeapi import InvalidEncryptionKeyAPIError, ResolveAPIError

    from homebody.garage_link import _reason

    secret = "the-board-address:6053"
    assert (
        _reason(InvalidEncryptionKeyAPIError(secret)) == "encryption handshake failed"
    )
    assert _reason(ResolveAPIError(secret)) == "address could not be resolved"
    assert _reason(RuntimeError(secret)) == "unexpected error"


def test_connection_refused_logs_a_reason(caplog: pytest.LogCaptureFixture) -> None:
    async def main() -> None:
        board = LoopbackBoard()
        await board.start()
        port = board.port
        await board.close()
        link = EsphomeLink(HOST, port, None, DEFAULT_ENTITY_NAMES)
        probe = Probe()
        task = asyncio.create_task(link.run(probe.on_state, probe.on_connected))
        try:
            await until(
                lambda: any(
                    "connection refused" in r.getMessage() for r in caplog.records
                )
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    with caplog.at_level(logging.DEBUG):
        asyncio.run(main())
    assert HOST not in _own_text(caplog)


def test_default_service_factory_builds_the_esphome_adapter() -> None:
    config = dataclasses.replace(GarageConfig(host=HOST), noise_psk=FAKE_KEY)
    link = garage_service._default_link(config)
    assert isinstance(link, EsphomeLink)


def test_library_warnings_still_reach_the_log_but_debug_does_not(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def body(board: LoopbackBoard, link: EsphomeLink, probe: Probe) -> None:
        await until(lambda: probe.connected_events == [True])
        library = logging.getLogger("aioesphomeapi.reconnect_logic")
        library.warning("library-warning-marker")
        library.debug("library-debug-marker")

    with caplog.at_level(logging.DEBUG):
        run_with_board(body)
    assert "library-warning-marker" in caplog.text
    assert "library-debug-marker" not in caplog.text
