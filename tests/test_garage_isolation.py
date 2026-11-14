"""A broken garage must never stop climate.

A real ``Daemon`` runs against the in-memory fake controller, fake probes and a
fake clock (as in ``test_daemon.py``) next to a real ``GarageService`` whose
link is made to raise or hang. Climate must keep ticking and publishing.
"""

from __future__ import annotations

import ast
import asyncio
import json
import time
from pathlib import Path

from garage_fakes import FakeGarageLink, FakeNtfy
from homebody import status
from homebody.audit import AuditLog
from homebody.config import GarageConfig
from homebody.daemon import Daemon
from homebody.garage import GarageSettings, GarageStore
from homebody.garage_service import GarageService
from homebody.notify import Notifier, NtfyConfig
from test_daemon import COMFORT, A, Rig
from test_garage_service import TICK, settle, until

ROOT = Path(__file__).resolve().parent.parent


class PublishingRig(Rig):
    """The daemon rig, with a status writer so ``homebody.json`` is real."""

    def new_daemon(self) -> Daemon:
        daemon = super().new_daemon()
        daemon._status = status.StatusWriter(self.tmp_path / "status")
        return daemon

    def document(self) -> dict:
        return json.loads((self.tmp_path / "status" / "homebody.json").read_text())


class RaisingLink(FakeGarageLink):
    """Every call raises."""

    async def run(self, on_state, on_connected):
        raise RuntimeError("link run failed")

    async def send(self, action):
        raise RuntimeError("link send failed")


class HangingLink(FakeGarageLink):
    """Every call waits forever."""

    async def run(self, on_state, on_connected):
        self.bind(on_state, on_connected)
        await asyncio.Event().wait()

    async def send(self, action):
        await asyncio.Event().wait()


def make_service(rig: PublishingRig, link, *, notifier=None, pause=None):
    service = GarageService(
        GarageConfig(host="house.local"),
        link=link,
        notifier=notifier,
        audit=AuditLog(rig.tmp_path / "garage", rig.clock),
        store=GarageStore(rig.tmp_path / "garage"),
        occupied=rig.daemon.occupied,
        settings=lambda: GarageSettings(),
        request_publish=rig.daemon.request_publish,
        clock=rig.clock,
        tick_seconds=TICK,
        pause=pause,
    )
    rig.daemon.attach_garage(service.snapshot)
    return service


async def fast_pause(stop: asyncio.Event, seconds: float) -> None:
    await asyncio.sleep(0.001)


async def tick(rig: PublishingRig, seconds: float = 15.0) -> None:
    rig.clock.advance(seconds)
    await rig.daemon.tick()


def test_link_raising_on_every_call_leaves_climate_ticking_and_publishing(tmp_path):
    rig = PublishingRig(tmp_path)
    service = make_service(rig, RaisingLink(), pause=fast_pause)
    stop = asyncio.Event()

    async def scenario():
        task = asyncio.create_task(service.run(stop))
        await settle(5)
        rig.wifi.home = {A.wifi_mac}
        await tick(rig)
        await tick(rig)
        assert rig.setpoint() == COMFORT
        doc = rig.document()
        assert doc["occupied"] is True
        assert doc["garage"]["door"]["state"] == "unknown"
        stop.set()
        await asyncio.wait_for(task, 3)
        assert task.exception() is None

    asyncio.run(scenario())


def test_link_that_hangs_forever_does_not_delay_a_tick(tmp_path):
    rig = PublishingRig(tmp_path)
    service = make_service(rig, HangingLink())
    stop = asyncio.Event()

    async def scenario():
        task = asyncio.create_task(service.run(stop))
        await settle(5)
        rig.wifi.home = {A.wifi_mac}
        started = time.monotonic()
        await asyncio.wait_for(tick(rig), 1.0)
        assert time.monotonic() - started < 1.0
        assert rig.setpoint() == COMFORT
        assert rig.document()["garage"] is not None
        stop.set()
        await asyncio.wait_for(task, 3)

    asyncio.run(scenario())


def test_snapshot_raising_publishes_garage_null_and_climate_fields(tmp_path):
    rig = PublishingRig(tmp_path)

    def boom():
        raise RuntimeError("snapshot failed")

    rig.daemon.attach_garage(boom)

    async def scenario():
        rig.wifi.home = {A.wifi_mac}
        await tick(rig)
        await tick(rig)

    asyncio.run(scenario())
    doc = rig.document()
    assert doc["garage"] is None
    assert doc["occupied"] is True
    assert doc["settings"]["comfort_f"] == 72
    assert rig.setpoint() == COMFORT


def test_ntfy_hanging_does_not_stall_the_garage_loop_or_climate(tmp_path):
    with FakeNtfy() as ntfy:
        ntfy.hang_next()
        rig = PublishingRig(tmp_path)
        notifier = Notifier(
            NtfyConfig(url=ntfy.url, topic="example-topic"),
            timeout=2.0,
            retries=1,
            sleep=lambda _s: None,
        )
        link = FakeGarageLink()
        service = make_service(rig, link, notifier=notifier)
        stop = asyncio.Event()

        async def scenario():
            task = asyncio.create_task(service.run(stop))
            await until(lambda: link._on_state.__name__ != "<lambda>")
            rig.wifi.home = {A.wifi_mac}
            await tick(rig)
            link.connect()
            link.push(operation="idle", position=0.0)
            link.push(operation="opening")
            link.push(operation="idle", position=1.0)
            await settle(5)
            rig.clock.advance(16 * 60)
            await until(lambda: len(ntfy.requests) == 1)  # the send is now hung
            link.push(operation="closing")
            link.push(operation="idle", position=0.0)
            await until(
                lambda: service.snapshot()["door"]["state"] == "closed"
            )
            await asyncio.wait_for(tick(rig), 1.0)
            assert rig.document()["garage"]["door"]["state"] == "closed"
            assert rig.setpoint() == COMFORT
            ntfy.close()
            stop.set()
            await asyncio.wait_for(task, 5)

        asyncio.run(scenario())


def test_garage_task_cancel_on_stop_is_clean(tmp_path):
    rig = PublishingRig(tmp_path)
    link = FakeGarageLink()
    service = make_service(rig, link)

    async def scenario():
        for how in ("stop", "cancel"):
            stop = asyncio.Event()
            task = asyncio.create_task(service.run(stop))
            await until(lambda: link._on_state.__name__ != "<lambda>")
            link.connect()
            link.push(operation="idle", position=0.0)
            await settle(3)
            if how == "stop":
                stop.set()
                await asyncio.wait_for(task, 3)
                assert task.exception() is None
            else:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                assert task.cancelled()
            await asyncio.sleep(0)
            others = asyncio.all_tasks() - {asyncio.current_task()}
            assert others == set(), others
            link._on_state = lambda _s: None

    asyncio.run(scenario())


def _top_level_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            found.add(module)
            found.update(f"{module}.{alias.name}" for alias in node.names)
    return found


def test_garage_code_imports_no_climate_module_and_takes_no_shared_lock():
    files = sorted(
        {
            *(ROOT / "src/homebody").glob("garage*.py"),
            ROOT / "src/homebody/notify.py",
            ROOT / "src/homebody/audit.py",
        }
    )
    assert len(files) >= 7
    for path in files:
        imports = _top_level_imports(path)
        assert "homebody.climate" not in imports, path.name
        assert "homebody.daemon" not in imports, path.name
        tree = ast.parse(path.read_text())
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert "tick" not in names, path.name  # never calls Daemon.tick
        assert not names & {"Lock", "RLock", "climate"}, path.name
