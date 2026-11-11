"""Shared test doubles for the Matter controller.

``FakeController`` is an in-memory stand-in for the matter-server WebSocket
command handler: it keeps nodes and their attributes (keyed by the real
"endpoint/cluster/attribute" path) and answers the same commands the controller
does, recording every call so a test can assert what the client sent.
"""

from __future__ import annotations

from typing import Any

from homebody import matter


class FakeController:
    def __init__(self) -> None:
        self.nodes: dict[int, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._next_node_id = 1

    def add_thermostat(
        self,
        *,
        node_id: int = 1,
        endpoint: int = 1,
        system_mode: int = int(matter.SystemMode.COOL),
        local_c100: int = 2300,
        cooling_c100: int = 2222,
        available: bool = True,
    ) -> int:
        cluster = matter.THERMOSTAT_CLUSTER_ID

        def path(attribute_id: int) -> str:
            return f"{endpoint}/{cluster}/{attribute_id}"

        attributes = {
            path(matter.ATTR_SYSTEM_MODE): system_mode,
            path(matter.ATTR_LOCAL_TEMPERATURE): local_c100,
            path(matter.ATTR_OCCUPIED_COOLING_SETPOINT): cooling_c100,
        }
        self.nodes[node_id] = {
            "node_id": node_id,
            "available": available,
            "attributes": attributes,
        }
        self._next_node_id = max(self._next_node_id, node_id + 1)
        return node_id

    async def command(self, command: str, args: dict[str, Any] | None = None) -> Any:
        args = args or {}
        self.calls.append((command, args))
        if command == "get_nodes":
            return list(self.nodes.values())
        if command == "get_node":
            return self.nodes[args["node_id"]]
        if command == "commission_with_code":
            node_id = self._next_node_id
            self.add_thermostat(node_id=node_id)
            return self.nodes[node_id]
        if command == "read_attribute":
            node = self.nodes[args["node_id"]]
            paths = args["attribute_path"]
            if isinstance(paths, str):
                paths = [paths]
            return {path: node["attributes"].get(path) for path in paths}
        if command == "write_attribute":
            node = self.nodes[args["node_id"]]
            node["attributes"][args["attribute_path"]] = args["value"]
            return None
        raise AssertionError(f"unexpected command: {command}")


class FakeConnectionContext:
    """An async context manager that yields a given connection, for the CLI."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    async def __aenter__(self) -> Any:
        return self._connection

    async def __aexit__(self, *exc: object) -> bool:
        return False
