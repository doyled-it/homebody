"""The live garage wiring in ``homebody run``, end to end over the fake link.

``cli._serve_async`` is the real startup path: it builds the daemon, builds the
garage service, attaches the snapshot, starts the settings API with the garage
target, and runs both tasks. Only the controller connection and the board link
are fakes. Requests go over a real loopback socket.
"""

from __future__ import annotations

import asyncio
import json

from fakes import FakeConnectionContext, FakeController
from garage_fakes import FakeGarageLink
from homebody import cli, daemon
from homebody.config import Config, GarageConfig
from homebody.presence import PresenceTracker
from test_daemon import A, Clock, NoBluetooth, Wifi

HEADERS = {"X-Snaphouse": "1", "X-Snaphouse-Origin": "home"}


async def post(port: int, path: str, body: dict) -> tuple[int, dict]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    payload = json.dumps(body).encode()
    head = f"POST {path} HTTP/1.1\r\nHost: x\r\nContent-Length: {len(payload)}\r\n"
    head += "".join(f"{k}: {v}\r\n" for k, v in HEADERS.items()) + "\r\n"
    writer.write(head.encode() + payload)
    await writer.drain()
    raw = await reader.read()
    writer.close()
    status_line, _, rest = raw.partition(b"\r\n")
    _, _, body_bytes = rest.partition(b"\r\n\r\n")
    return int(status_line.split()[1]), json.loads(body_bytes)


async def until(check, timeout=3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.005)


def test_homebody_run_wires_the_garage_through_api_publish_and_status(tmp_path):
    clock = Clock()
    controller = FakeController()
    controller.add_thermostat(cooling_c100=2778)
    link = FakeGarageLink()
    settings = Config(
        people=(A,),
        state_dir=tmp_path / "state",
        status_dir=tmp_path / "status",
        api_port=0,
        garage=GarageConfig(host="house.local"),
    )

    def build_daemon(config, *, url=None):
        presence = PresenceTracker(
            config.people,
            wifi=Wifi(),
            bluetooth=NoBluetooth(),
            clock=clock,
            away_window=config.away_minutes * 60.0,
        )
        return daemon.Daemon(
            config,
            presence=presence,
            connect=lambda: FakeConnectionContext(controller),
            clock=clock,
            status_writer=daemon.status.StatusWriter(config.status_dir),
        )

    def document() -> dict:
        return json.loads((tmp_path / "status" / "homebody.json").read_text())

    async def scenario():
        stop = asyncio.Event()
        ready: dict = {}
        task = asyncio.create_task(
            cli._serve_async(
                settings,
                stop,
                build_daemon=build_daemon,
                link_factory=lambda cfg: link,
                on_started=lambda server, service: ready.update(
                    port=server.sockets[0].getsockname()[1], service=service
                ),
            )
        )
        await until(lambda: "port" in ready and link._on_state.__name__ != "<lambda>")
        port = ready["port"]
        await until(lambda: (tmp_path / "status" / "homebody.json").exists())
        assert document()["garage"]["door"]["state"] == "unknown"

        link.connect()
        link.push(operation="idle", position=0.0)
        # a door change publishes early, without waiting for the 15 s tick
        await until(lambda: document()["garage"]["door"]["state"] == "closed")

        code, intent = await post(
            port, "/api/homebody/garage/intent", {"action": "open"}
        )
        assert code == 200 and intent["confirm_required"] is False
        code, doc = await post(
            port,
            "/api/homebody/garage/command",
            {"action": "open", "nonce": intent["nonce"]},
        )
        assert code == 200, doc
        assert link.sent == ["open"]
        assert doc["garage"]["door"]["state"] == "opening"
        link.settle()
        await until(lambda: document()["garage"]["door"]["state"] == "open")
        recent = document()["garage"]["recent"]
        assert recent[-1]["event"] == "opened" and recent[-1]["origin"] == "home"

        stop.set()
        await asyncio.wait_for(task, 5)
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(scenario())


def test_homebody_run_starts_as_today_when_garage_is_not_configured(tmp_path):
    clock = Clock()
    controller = FakeController()
    controller.add_thermostat(cooling_c100=2778)
    settings = Config(
        people=(A,),
        state_dir=tmp_path / "state",
        status_dir=tmp_path / "status",
        api_port=0,
    )

    def build_daemon(config, *, url=None):
        presence = PresenceTracker(
            config.people,
            wifi=Wifi(),
            bluetooth=NoBluetooth(),
            clock=clock,
            away_window=config.away_minutes * 60.0,
        )
        return daemon.Daemon(
            config,
            presence=presence,
            connect=lambda: FakeConnectionContext(controller),
            clock=clock,
            status_writer=daemon.status.StatusWriter(config.status_dir),
        )

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(
            cli._serve_async(settings, stop, build_daemon=build_daemon)
        )
        await until(lambda: (tmp_path / "status" / "homebody.json").exists())
        assert json.loads((tmp_path / "status" / "homebody.json").read_text())[
            "garage"
        ] is None
        stop.set()
        await asyncio.wait_for(task, 5)

    asyncio.run(scenario())
