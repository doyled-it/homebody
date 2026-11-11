"""Tests for the thin Matter client (src/homebody/matter.py).

The client talks to the Matter controller only over the controller's WebSocket
command protocol, so it is exercised here against an in-memory fake controller
that stores node attributes and answers the same commands the real
`matter-server` does (get_nodes, get_node, commission_with_code, read_attribute,
write_attribute). No CHIP SDK and no network are involved, so these run on any
platform.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from fakes import FakeController
from homebody import matter


def test_fahrenheit_to_centi_celsius_matches_pinned_setpoints():
    # The global constraints pin these exact Matter values.
    assert matter.fahrenheit_to_centi_celsius(72) == 2222
    assert matter.fahrenheit_to_centi_celsius(82) == 2778


def test_centi_celsius_to_fahrenheit_rounds_to_whole_degrees():
    assert matter.centi_celsius_to_fahrenheit(2222) == 72.0
    assert matter.centi_celsius_to_fahrenheit(2778) == 82.0


def test_fahrenheit_round_trips_through_centi_celsius():
    for f in (68, 70, 72, 75, 78, 82, 85):
        assert matter.centi_celsius_to_fahrenheit(
            matter.fahrenheit_to_centi_celsius(f)
        ) == float(f)


def test_get_nodes_returns_commissioned_nodes():
    controller = FakeController()
    controller.add_thermostat(node_id=7)
    client = matter.MatterClient(controller)

    nodes = asyncio.run(client.get_nodes())

    assert [n["node_id"] for n in nodes] == [7]


def test_find_thermostat_locates_node_and_endpoint():
    controller = FakeController()
    controller.add_thermostat(node_id=4, endpoint=1)
    client = matter.MatterClient(controller)

    nodes = asyncio.run(client.get_nodes())
    found = matter.find_thermostat(nodes)

    assert found == (4, 1)


def test_find_thermostat_returns_none_without_a_thermostat():
    nodes = [{"node_id": 9, "available": True, "attributes": {"1/6/0": True}}]
    assert matter.find_thermostat(nodes) is None


def test_read_thermostat_reads_mode_temperature_and_setpoint():
    controller = FakeController()
    controller.add_thermostat(
        node_id=1,
        endpoint=1,
        system_mode=int(matter.SystemMode.COOL),
        local_c100=2345,
        cooling_c100=2222,
    )
    client = matter.MatterClient(controller)

    state = asyncio.run(client.read_thermostat(1, endpoint=1))

    assert state.system_mode == matter.SystemMode.COOL
    assert state.local_temperature_c100 == 2345
    assert state.occupied_cooling_setpoint_c100 == 2222
    assert state.cooling_setpoint_f == 72.0


def test_read_thermostat_discovers_endpoint_when_not_given():
    controller = FakeController()
    controller.add_thermostat(node_id=2, endpoint=1)
    client = matter.MatterClient(controller)

    state = asyncio.run(client.read_thermostat(2))

    assert state.endpoint == 1
    assert state.system_mode == matter.SystemMode.COOL


def test_set_cooling_setpoint_writes_hundredths_of_celsius():
    controller = FakeController()
    controller.add_thermostat(node_id=1, endpoint=1, cooling_c100=2222)
    client = matter.MatterClient(controller)

    asyncio.run(client.set_cooling_setpoint_f(1, 82, endpoint=1))

    path = f"1/{matter.THERMOSTAT_CLUSTER_ID}/{matter.ATTR_OCCUPIED_COOLING_SETPOINT}"
    assert controller.nodes[1]["attributes"][path] == 2778
    # Confirm the exact write command and args reached the controller.
    write = [c for c in controller.calls if c[0] == "write_attribute"][-1]
    assert write[1] == {"node_id": 1, "attribute_path": path, "value": 2778}


def test_set_system_mode_writes_the_mode_value():
    controller = FakeController()
    controller.add_thermostat(node_id=1, endpoint=1)
    client = matter.MatterClient(controller)

    asyncio.run(client.set_system_mode(1, matter.SystemMode.COOL, endpoint=1))

    path = f"1/{matter.THERMOSTAT_CLUSTER_ID}/{matter.ATTR_SYSTEM_MODE}"
    assert controller.nodes[1]["attributes"][path] == int(matter.SystemMode.COOL)


def test_commission_returns_the_new_node():
    controller = FakeController()
    client = matter.MatterClient(controller)

    node = asyncio.run(client.commission("1234-567-8901"))

    assert node["node_id"] == 1
    # Commissioning the already-networked Nest must not require Bluetooth.
    commission = [c for c in controller.calls if c[0] == "commission_with_code"][0]
    assert commission[1]["network_only"] is True


def test_read_thermostat_tolerates_missing_attributes():
    controller = FakeController()
    node_id = controller.add_thermostat(node_id=1, endpoint=1)
    # A node that answers but has not reported the setpoint yet.
    path = f"1/{matter.THERMOSTAT_CLUSTER_ID}/{matter.ATTR_OCCUPIED_COOLING_SETPOINT}"
    del controller.nodes[node_id]["attributes"][path]
    client = matter.MatterClient(controller)

    state = asyncio.run(client.read_thermostat(1, endpoint=1))

    assert state.occupied_cooling_setpoint_c100 is None
    assert state.cooling_setpoint_f is None


def test_command_error_is_raised_on_error_result():
    class FailingConnection:
        async def command(self, command: str, args: dict | None = None) -> Any:
            raise matter.CommandError(1, "node not ready")

    client = matter.MatterClient(FailingConnection())
    with pytest.raises(matter.CommandError):
        asyncio.run(client.read_thermostat(1, endpoint=1))
