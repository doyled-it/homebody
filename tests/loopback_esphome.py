"""A plaintext ESPHome native-API server standing in for the garage board.

It speaks just enough of the protocol for ``aioesphomeapi``: hello, optional
authentication, device info, entity listing (from ``fixtures/blaq_entities.json``),
state subscription, the cover command, and ping. Frames are the plaintext
shape: a zero byte, a varuint length, a varuint message type, then the payload.
The noise handshake is not reimplemented; the encrypted path is covered by the
library and the install-day check.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from aioesphomeapi import api_pb2 as pb
from aioesphomeapi.core import MESSAGE_TYPE_TO_PROTO

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "blaq_entities.json"
TYPE_OF = {proto: number for number, proto in MESSAGE_TYPE_TO_PROTO.items()}

_OPERATION = {
    "idle": pb.COVER_OPERATION_IDLE,
    "opening": pb.COVER_OPERATION_IS_OPENING,
    "closing": pb.COVER_OPERATION_IS_CLOSING,
}


def _varuint(value: int) -> bytes:
    out = bytearray()
    while True:
        low = value & 0x7F
        value >>= 7
        if value:
            out.append(low | 0x80)
        else:
            out.append(low)
            return bytes(out)


def frame(message: Any) -> bytes:
    payload = message.SerializeToString()
    return b"\x00" + _varuint(len(payload)) + _varuint(TYPE_OF[type(message)]) + payload


async def _read_varuint(reader: asyncio.StreamReader) -> int:
    value = 0
    shift = 0
    while True:
        byte = (await reader.readexactly(1))[0]
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value
        shift += 7


class LoopbackBoard:
    """One board. ``shape`` is ``position`` or ``binary`` (a cover that cannot
    report a position, only fully open or fully closed)."""

    def __init__(
        self,
        *,
        shape: str = "position",
        has_cover: bool = True,
        auth_fails: bool = False,
    ) -> None:
        self.shape = shape
        self.has_cover = has_cover
        self.auth_fails = auth_fails
        self.commands: list[dict[str, Any]] = []
        self.port = 0
        # What the board reports on subscribe; tests change these between connects.
        self.cover: tuple[float, str] | None = (0.0, "idle")
        self.binary: dict[str, bool] = {"Obstruction": False}
        self.lock_locked: bool | None = False
        self.light_on: bool | None = False
        self._server: asyncio.Server | None = None
        self._writers: list[asyncio.StreamWriter] = []
        self._subscribed: list[asyncio.StreamWriter] = []
        self._entities = json.loads(FIXTURE.read_text(encoding="utf-8"))["entities"]
        self._keys = {e["name"]: i + 1 for i, e in enumerate(self._entities)}

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        await self.drop()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def drop(self) -> None:
        """Cut every client connection; the server keeps listening."""
        for writer in list(self._writers):
            writer.close()
        self._writers.clear()
        self._subscribed.clear()
        await asyncio.sleep(0)

    # pushes

    def _send_subscribed(self, message: Any) -> None:
        for writer in list(self._subscribed):
            writer.write(frame(message))

    def push_cover(self, position: float, operation: str = "idle") -> None:
        self.cover = (position, operation)
        self._send_subscribed(self._cover_state())

    def push_binary(self, name: str, value: bool) -> None:
        self.binary[name] = value
        self._send_subscribed(self._binary_state(name))

    def push_lock(self, locked: bool) -> None:
        self.lock_locked = locked
        self._send_subscribed(self._lock_state())

    # messages

    def _cover_state(self) -> pb.CoverStateResponse:
        assert self.cover is not None
        position, operation = self.cover
        return pb.CoverStateResponse(
            key=self._keys["Garage Door"],
            position=position,
            current_operation=_OPERATION[operation],
        )

    def _binary_state(self, name: str) -> pb.BinarySensorStateResponse:
        return pb.BinarySensorStateResponse(
            key=self._keys[name], state=self.binary[name]
        )

    def _lock_state(self) -> pb.LockStateResponse:
        locked = bool(self.lock_locked)
        return pb.LockStateResponse(
            key=self._keys["Lock"],
            state=pb.LOCK_STATE_LOCKED if locked else pb.LOCK_STATE_UNLOCKED,
        )

    def _entity_messages(self) -> list[Any]:
        out: list[Any] = []
        for entity in self._entities:
            kind, name = entity["type"], entity["name"]
            key = self._keys[name]
            object_id = name.lower().replace(" ", "_")
            if kind == "cover":
                if self.has_cover:
                    out.append(
                        pb.ListEntitiesCoverResponse(
                            object_id=object_id,
                            key=key,
                            name=name,
                            supports_position=self.shape == "position",
                        )
                    )
            elif kind == "light":
                out.append(
                    pb.ListEntitiesLightResponse(
                        object_id=object_id, key=key, name=name
                    )
                )
            elif kind == "lock":
                out.append(
                    pb.ListEntitiesLockResponse(object_id=object_id, key=key, name=name)
                )
            elif kind == "binary_sensor":
                out.append(
                    pb.ListEntitiesBinarySensorResponse(
                        object_id=object_id,
                        key=key,
                        name=name,
                        device_class=entity.get("device_class", ""),
                    )
                )
            elif kind == "sensor":
                out.append(
                    pb.ListEntitiesSensorResponse(
                        object_id=object_id, key=key, name=name
                    )
                )
        return out

    def _state_messages(self) -> list[Any]:
        out: list[Any] = []
        if self.has_cover and self.cover is not None:
            out.append(self._cover_state())
        for name in self.binary:
            out.append(self._binary_state(name))
        if self.lock_locked is not None:
            out.append(self._lock_state())
        if self.light_on is not None:
            out.append(
                pb.LightStateResponse(
                    key=self._keys["Garage Light"], state=self.light_on
                )
            )
        return out

    # the connection

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._writers.append(writer)
        try:
            while True:
                if (await reader.readexactly(1))[0] != 0:
                    return
                length = await _read_varuint(reader)
                number = await _read_varuint(reader)
                payload = await reader.readexactly(length)
                message = MESSAGE_TYPE_TO_PROTO[number]()
                message.ParseFromString(payload)
                if not self._handle(message, writer):
                    return
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            if writer in self._writers:
                self._writers.remove(writer)
            if writer in self._subscribed:
                self._subscribed.remove(writer)

    def _handle(self, message: Any, writer: asyncio.StreamWriter) -> bool:
        """Answer one request. False ends the connection."""
        if isinstance(message, pb.HelloRequest):
            writer.write(
                frame(
                    pb.HelloResponse(
                        api_version_major=1,
                        api_version_minor=10,
                        server_info="loopback board",
                        name="loopback-board",
                    )
                )
            )
        elif isinstance(message, pb.AuthenticationRequest):
            writer.write(
                frame(pb.AuthenticationResponse(invalid_password=self.auth_fails))
            )
            return not self.auth_fails
        elif isinstance(message, pb.DeviceInfoRequest):
            writer.write(
                frame(
                    pb.DeviceInfoResponse(
                        name="loopback-board", mac_address="02:00:00:00:00:01"
                    )
                )
            )
        elif isinstance(message, pb.ListEntitiesRequest):
            for entity in self._entity_messages():
                writer.write(frame(entity))
            writer.write(frame(pb.ListEntitiesDoneResponse()))
        elif isinstance(message, pb.SubscribeStatesRequest):
            self._subscribed.append(writer)
            for state in self._state_messages():
                writer.write(frame(state))
        elif isinstance(message, pb.CoverCommandRequest):
            self.commands.append(
                {
                    "has_position": message.has_position,
                    "position": message.position,
                    "stop": message.stop,
                    "has_legacy_command": message.has_legacy_command,
                }
            )
        elif isinstance(message, pb.PingRequest):
            writer.write(frame(pb.PingResponse()))
        elif isinstance(message, pb.DisconnectRequest):
            writer.write(frame(pb.DisconnectResponse()))
            return False
        return True
