"""A thin client over the Matter controller's WebSocket API.

homebody does not embed a Matter stack. The open-source controller
(`python-matter-server`, run as the `homebody-matter` service) holds the fabric
and talks to the Nest; homebody drives it over the controller's local WebSocket
command protocol. This module speaks that protocol directly over a plain
WebSocket, so it imports no CHIP SDK and runs and tests on any platform.

The protocol (from the controller):

- Connect to ``ws://<host>:<port>/ws``. The server sends one ServerInfoMessage
  (it carries ``sdk_version``) before any command.
- A command is ``{"message_id", "command", "args"}``; a reply is either
  ``{"message_id", "result"}`` or ``{"message_id", "error_code", "details"}``.
  Stateless ``{"event", "data"}`` messages may arrive at any time and are
  ignored while waiting for a command's reply.

The Thermostat cluster (0x0201) carries the system mode, the local temperature,
and the occupied cooling setpoint. Matter temperatures are signed int16 in
hundredths of a degree Celsius, so 72 F is 2222 and 82 F is 2778.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Protocol, runtime_checkable

import websockets

DEFAULT_MATTER_URL = "ws://127.0.0.1:5580/ws"

# Thermostat cluster (0x0201) and the three attributes homebody uses.
THERMOSTAT_CLUSTER_ID = 513
ATTR_LOCAL_TEMPERATURE = 0  # 0x0000
ATTR_OCCUPIED_COOLING_SETPOINT = 17  # 0x0011
ATTR_SYSTEM_MODE = 28  # 0x001C


class SystemMode(IntEnum):
    """Matter Thermostat SystemMode values (the ones homebody cares about)."""

    OFF = 0
    AUTO = 1
    COOL = 3
    HEAT = 4


# The subset of SystemMode names the CLI accepts and prints.
SYSTEM_MODE_NAMES = {
    SystemMode.OFF: "off",
    SystemMode.AUTO: "auto",
    SystemMode.COOL: "cool",
    SystemMode.HEAT: "heat",
}


def fahrenheit_to_centi_celsius(fahrenheit: float) -> int:
    """Fahrenheit to Matter's hundredths of a degree Celsius (72 F -> 2222)."""
    return round((fahrenheit - 32) * 5 / 9 * 100)


def centi_celsius_to_fahrenheit(centi_celsius: int) -> float:
    """Matter's hundredths of a degree Celsius to Fahrenheit, to one decimal."""
    return round(centi_celsius / 100 * 9 / 5 + 32, 1)


class MatterError(Exception):
    """Any failure talking to the Matter controller."""


class CommandError(MatterError):
    """The controller answered a command with an error result."""

    def __init__(self, error_code: int, details: str | None = None) -> None:
        self.error_code = error_code
        self.details = details
        super().__init__(f"matter command failed ({error_code}): {details}")


class MatterConnectionError(MatterError):
    """The WebSocket connection to the controller could not be established."""


@runtime_checkable
class MatterConnection(Protocol):
    """The seam between the client and the controller: one command, one reply.

    The real implementation is :class:`WebSocketConnection`; tests pass a fake
    that stores node attributes in memory.
    """

    async def command(
        self, command: str, args: dict[str, Any] | None = None
    ) -> Any: ...


@dataclass
class ThermostatState:
    """A snapshot of the thermostat's mode, temperature, and cooling setpoint.

    Temperatures are Matter hundredths of a degree Celsius, or ``None`` when the
    controller has not reported that attribute yet.
    """

    node_id: int
    endpoint: int
    system_mode: int | None
    local_temperature_c100: int | None
    occupied_cooling_setpoint_c100: int | None

    @property
    def local_temperature_f(self) -> float | None:
        if self.local_temperature_c100 is None:
            return None
        return centi_celsius_to_fahrenheit(self.local_temperature_c100)

    @property
    def cooling_setpoint_f(self) -> float | None:
        if self.occupied_cooling_setpoint_c100 is None:
            return None
        return centi_celsius_to_fahrenheit(self.occupied_cooling_setpoint_c100)

    @property
    def system_mode_name(self) -> str:
        if self.system_mode is None:
            return "unknown"
        try:
            return SYSTEM_MODE_NAMES[SystemMode(self.system_mode)]
        except ValueError:
            return str(self.system_mode)


def _attribute_path(endpoint: int, attribute_id: int) -> str:
    return f"{endpoint}/{THERMOSTAT_CLUSTER_ID}/{attribute_id}"


def find_thermostat(nodes: list[dict[str, Any]]) -> tuple[int, int] | None:
    """The (node_id, endpoint) of the first node exposing the Thermostat cluster.

    ``None`` when no commissioned node has a thermostat, so a caller can report
    "no thermostat" rather than guess.
    """
    for node in nodes:
        endpoint = _thermostat_endpoint(node)
        if endpoint is not None:
            return node["node_id"], endpoint
    return None


def _thermostat_endpoint(node: dict[str, Any]) -> int | None:
    """The endpoint carrying the Thermostat cluster on a node, or ``None``."""
    for path in node.get("attributes", {}):
        parts = path.split("/")
        if len(parts) == 3 and parts[1] == str(THERMOSTAT_CLUSTER_ID):
            return int(parts[0])
    return None


class MatterClient:
    """High-level thermostat operations over a :class:`MatterConnection`."""

    def __init__(self, connection: MatterConnection) -> None:
        self._conn = connection

    async def get_nodes(self) -> list[dict[str, Any]]:
        """Every node the controller knows about."""
        return await self._conn.command("get_nodes")

    async def get_node(self, node_id: int) -> dict[str, Any]:
        return await self._conn.command("get_node", {"node_id": node_id})

    async def commission(
        self, code: str, network_only: bool = True
    ) -> dict[str, Any]:
        """Commission a device with a QR or manual pairing code.

        ``network_only`` defaults to ``True``: the Nest is already on Wi-Fi and
        reachable over the LAN, so discovery stays on the network and never needs
        Bluetooth commissioning (which the hub may not offer).
        """
        return await self._conn.command(
            "commission_with_code", {"code": code, "network_only": network_only}
        )

    async def read_thermostat(
        self, node_id: int, endpoint: int | None = None
    ) -> ThermostatState:
        """Read the thermostat's mode, local temperature, and cooling setpoint.

        With no ``endpoint`` the thermostat endpoint is discovered from the
        node's reported clusters.
        """
        if endpoint is None:
            node = await self.get_node(node_id)
            endpoint = _thermostat_endpoint(node)
            if endpoint is None:
                raise MatterError(f"node {node_id} has no thermostat cluster")

        paths = [
            _attribute_path(endpoint, ATTR_SYSTEM_MODE),
            _attribute_path(endpoint, ATTR_LOCAL_TEMPERATURE),
            _attribute_path(endpoint, ATTR_OCCUPIED_COOLING_SETPOINT),
        ]
        values = await self._conn.command(
            "read_attribute", {"node_id": node_id, "attribute_path": paths}
        )
        return ThermostatState(
            node_id=node_id,
            endpoint=endpoint,
            system_mode=values.get(paths[0]),
            local_temperature_c100=values.get(paths[1]),
            occupied_cooling_setpoint_c100=values.get(paths[2]),
        )

    async def set_system_mode(
        self, node_id: int, mode: SystemMode | int, endpoint: int = 1
    ) -> None:
        """Write the thermostat's system mode."""
        await self._conn.command(
            "write_attribute",
            {
                "node_id": node_id,
                "attribute_path": _attribute_path(endpoint, ATTR_SYSTEM_MODE),
                "value": int(mode),
            },
        )

    async def set_cooling_setpoint_centi_c(
        self, node_id: int, centi_celsius: int, endpoint: int = 1
    ) -> None:
        """Write the occupied cooling setpoint in hundredths of a degree Celsius."""
        await self._conn.command(
            "write_attribute",
            {
                "node_id": node_id,
                "attribute_path": _attribute_path(
                    endpoint, ATTR_OCCUPIED_COOLING_SETPOINT
                ),
                "value": int(centi_celsius),
            },
        )

    async def set_cooling_setpoint_f(
        self, node_id: int, fahrenheit: float, endpoint: int = 1
    ) -> None:
        """Write the occupied cooling setpoint from a Fahrenheit value."""
        await self.set_cooling_setpoint_centi_c(
            node_id, fahrenheit_to_centi_celsius(fahrenheit), endpoint=endpoint
        )


class WebSocketConnection:
    """A real :class:`MatterConnection` over the controller's WebSocket.

    One command at a time: send the command, then read messages until the reply
    with the matching ``message_id`` arrives, skipping any events in between.
    """

    def __init__(self, url: str = DEFAULT_MATTER_URL) -> None:
        self._url = url
        self._ws: Any | None = None
        self.server_info: dict[str, Any] | None = None

    async def __aenter__(self) -> WebSocketConnection:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def connect(self) -> None:
        try:
            self._ws = await websockets.connect(self._url, max_size=None)
        except OSError as err:
            raise MatterConnectionError(
                f"cannot connect to {self._url}: {err}"
            ) from err
        # The controller sends its ServerInfoMessage first; read it so it does
        # not get mistaken for a command reply.
        raw = await self._ws.recv()
        self.server_info = json.loads(raw)

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def command(
        self, command: str, args: dict[str, Any] | None = None
    ) -> Any:
        if self._ws is None:
            raise MatterConnectionError("not connected")
        message_id = uuid.uuid4().hex
        message: dict[str, Any] = {"message_id": message_id, "command": command}
        if args:
            message["args"] = args
        await self._ws.send(json.dumps(message))

        while True:
            reply = json.loads(await self._ws.recv())
            if reply.get("message_id") != message_id:
                # A ServerInfo update or a stateless event; not our reply.
                continue
            if "error_code" in reply:
                raise CommandError(reply["error_code"], reply.get("details"))
            return reply.get("result")
