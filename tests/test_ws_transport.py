"""Exercise the real WebSocketConnection against a loopback controller.

A tiny in-process WebSocket server speaks the controller's wire protocol: it
sends a ServerInfoMessage on connect, replies to each command by ``message_id``,
and deliberately interleaves a stateless event and a mismatched-id message to
prove the client ignores them while waiting for its reply. This covers the wire
layer (framing, id correlation, error results) that the fake-controller tests in
test_matter.py do not, without any CHIP SDK.
"""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from homebody import matter


async def _serve(websocket) -> None:
    # The controller sends its ServerInfoMessage before anything else.
    await websocket.send(json.dumps({"sdk_version": "test", "fabric_id": 1}))
    async for raw in websocket:
        msg = json.loads(raw)
        mid = msg["message_id"]
        command = msg["command"]
        if command == "get_nodes":
            # An unrelated event and a mismatched id arrive first; the client
            # must skip both and still return this command's result.
            await websocket.send(json.dumps({"event": "node_updated", "data": {}}))
            await websocket.send(
                json.dumps({"message_id": "other", "result": ["wrong"]})
            )
            await websocket.send(
                json.dumps({"message_id": mid, "result": [{"node_id": 1}]})
            )
        elif command == "boom":
            await websocket.send(
                json.dumps(
                    {"message_id": mid, "error_code": 7, "details": "node not ready"}
                )
            )
        else:
            await websocket.send(json.dumps({"message_id": mid, "result": None}))


async def _run_against_server(body) -> None:
    async with websockets.serve(_serve, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        url = f"ws://127.0.0.1:{port}/ws"
        async with matter.WebSocketConnection(url) as conn:
            await body(conn)


def test_connection_reads_server_info_and_correlates_replies():
    async def body(conn: matter.WebSocketConnection) -> None:
        assert conn.server_info is not None
        assert conn.server_info["sdk_version"] == "test"
        result = await conn.command("get_nodes")
        assert result == [{"node_id": 1}]

    asyncio.run(_run_against_server(body))


def test_connection_raises_command_error_on_error_result():
    async def body(conn: matter.WebSocketConnection) -> None:
        with pytest.raises(matter.CommandError) as excinfo:
            await conn.command("boom")
        assert excinfo.value.error_code == 7

    asyncio.run(_run_against_server(body))


def test_client_reads_through_the_real_connection():
    async def body(conn: matter.WebSocketConnection) -> None:
        client = matter.MatterClient(conn)
        nodes = await client.get_nodes()
        assert nodes == [{"node_id": 1}]

    asyncio.run(_run_against_server(body))


def test_connect_failure_raises_matter_connection_error():
    async def body() -> None:
        # Nothing is listening on this port, so the connect must fail cleanly.
        conn = matter.WebSocketConnection("ws://127.0.0.1:1/ws")
        with pytest.raises(matter.MatterConnectionError):
            await conn.connect()

    asyncio.run(body())


def test_command_on_a_dropped_connection_raises_matter_connection_error():
    async def body(conn: matter.WebSocketConnection) -> None:
        # The server closes the socket on this command, so recv() raises a
        # websockets ConnectionClosed; callers must see MatterConnectionError.
        with pytest.raises(matter.MatterConnectionError):
            await conn.command("hangup")

    async def serve(websocket) -> None:
        await websocket.send(json.dumps({"sdk_version": "test"}))
        await websocket.recv()
        await websocket.close()

    async def run() -> None:
        async with websockets.serve(serve, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with matter.WebSocketConnection(
                f"ws://127.0.0.1:{port}/ws"
            ) as conn:
                await body(conn)

    asyncio.run(run())
