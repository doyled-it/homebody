"""The supervised garage service: loop, audit, alerts, state file, supervisor.

Each test runs the real service on a loop under ``asyncio.run`` against the fake
link, a fake clock and (for delivery) the loopback fake ntfy. The loop wakes on
every link push and on a short timer, so a test advances the clock and waits
for the effect rather than sleeping a real second.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from types import SimpleNamespace

from garage_fakes import FakeGarageLink, FakeNtfy
from homebody import garage_service
from homebody.audit import AuditLog
from homebody.config import Config, GarageConfig
from homebody.garage import GarageSettings, GarageStore
from homebody.garage_service import GarageService
from homebody.notify import Notifier, NtfyConfig

MIN = 60.0
TICK = 0.01


class Clock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make(
    tmp_path,
    *,
    ntfy: FakeNtfy | None = None,
    occupied: bool | None = True,
    link: FakeGarageLink | None = None,
    clock: Clock | None = None,
    settings: GarageSettings | None = None,
    pause=None,
):
    clock = clock or Clock()
    link = link or FakeGarageLink()
    audit = AuditLog(tmp_path, clock)
    store = GarageStore(tmp_path)
    notifier = None
    if ntfy is not None:
        notifier = Notifier(
            NtfyConfig(url=ntfy.url, topic="example-topic"),
            timeout=2.0,
            retries=1,
            sleep=lambda _s: None,
        )
    env = SimpleNamespace(
        clock=clock,
        link=link,
        audit=audit,
        store=store,
        occupied=[occupied],
        settings=settings or GarageSettings(),
        publishes=[0],
        stop=asyncio.Event(),
    )

    def request_publish() -> None:
        env.publishes[0] += 1

    env.service = GarageService(
        GarageConfig(host="house.local"),
        link=link,
        notifier=notifier,
        audit=audit,
        store=store,
        occupied=lambda: env.occupied[0],
        settings=lambda: env.settings,
        request_publish=request_publish,
        clock=clock,
        tick_seconds=TICK,
        pause=pause,
    )
    return env


async def until(check: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.005)


async def settle(cycles: int = 20) -> None:
    for _ in range(cycles):
        await asyncio.sleep(TICK)


def bound(link: FakeGarageLink) -> bool:
    """True once ``run`` has handed the link its callbacks."""
    return link._on_state.__name__ != "<lambda>"


async def start(env) -> asyncio.Task:
    task = asyncio.create_task(env.service.run(env.stop))
    await until(lambda: bound(env.link))
    return task


async def finish(env, task: asyncio.Task) -> None:
    env.stop.set()
    await asyncio.wait_for(task, 3)


def open_the_door(link: FakeGarageLink) -> None:
    """Closed on connect, then opens by itself (the opener's own remote)."""
    link.connect()
    link.push(operation="idle", position=0.0)
    link.push(operation="opening")
    link.push(operation="idle", position=1.0)


def door_lines(env) -> list[dict]:
    return env.audit.recent()


def test_door_change_publishes_and_audits_with_opener_origin(tmp_path):
    env = make(tmp_path)

    async def scenario():
        task = await start(env)
        before = env.publishes[0]
        open_the_door(env.link)
        await until(lambda: any(e["event"] == "opened" for e in door_lines(env)))
        await finish(env, task)
        opened = [e for e in door_lines(env) if e["event"] == "opened"][-1]
        assert opened["origin"] == "opener"
        assert opened["actor"] is None
        assert env.publishes[0] > before

    asyncio.run(scenario())


def test_commanded_close_audited_with_command_origin_not_opener(tmp_path):
    env = make(tmp_path)

    async def scenario():
        task = await start(env)
        open_the_door(env.link)
        await until(lambda: any(e["event"] == "opened" for e in door_lines(env)))
        gate = env.service.gate
        nonce = gate.intent("close", "home", None)["nonce"]
        assert await gate.command("close", nonce, "home", None) == "sent"
        before = sum(e["event"] == "closed" for e in door_lines(env))
        env.link.settle()
        await until(
            lambda: sum(e["event"] == "closed" for e in door_lines(env)) > before
        )
        await finish(env, task)
        closed = [e for e in door_lines(env) if e["event"] == "closed"][-1]
        assert closed["origin"] == "home"
        assert closed["origin"] != "opener"

    asyncio.run(scenario())


def test_left_open_alert_reaches_fake_ntfy_once(tmp_path):
    with FakeNtfy() as ntfy:
        env = make(tmp_path, ntfy=ntfy)

        async def scenario():
            task = await start(env)
            open_the_door(env.link)
            await settle(5)
            env.clock.advance(16 * MIN)
            await until(lambda: len(ntfy.requests) == 1)
            env.clock.advance(5 * MIN)
            await settle(30)
            await finish(env, task)

        asyncio.run(scenario())
        assert len(ntfy.requests) == 1
        assert ntfy.requests[0].headers["Title"] == "Garage door left open"
        assert ntfy.requests[0].path.endswith("/example-topic")


def test_ntfy_failure_retries_next_pass_without_duplicate(tmp_path):
    with FakeNtfy() as ntfy:
        ntfy.fail_next(1)
        env = make(tmp_path, ntfy=ntfy)

        async def scenario():
            task = await start(env)
            open_the_door(env.link)
            await settle(5)
            env.clock.advance(16 * MIN)
            await until(lambda: len(ntfy.requests) == 1)
            await settle(30)
            assert len(ntfy.requests) == 1  # backing off, not hammering
            env.clock.advance(garage_service.RETRY_SECONDS + 1)
            await until(lambda: len(ntfy.requests) == 2)
            env.clock.advance(5 * MIN)
            await settle(30)
            await finish(env, task)

        asyncio.run(scenario())
        assert len(ntfy.requests) == 2
        assert ntfy.requests[0].body == ntfy.requests[1].body


def test_occupied_true_to_false_with_open_door_sends_left_open_away(tmp_path):
    with FakeNtfy() as ntfy:
        env = make(tmp_path, ntfy=ntfy, occupied=True)

        async def scenario():
            task = await start(env)
            open_the_door(env.link)
            await settle(10)
            assert ntfy.requests == []
            env.occupied[0] = False
            await until(lambda: len(ntfy.requests) == 1)
            await settle(20)
            await finish(env, task)

        asyncio.run(scenario())
        assert len(ntfy.requests) == 1
        request = ntfy.requests[0]
        assert request.headers["Priority"] == "high"
        assert b"Everyone has left" in request.body


def test_opened_away_fires_after_the_grace_with_a_timely_send(tmp_path):
    with FakeNtfy() as ntfy:
        env = make(tmp_path, ntfy=ntfy, occupied=False)

        async def scenario():
            task = await start(env)
            open_the_door(env.link)
            await settle(10)
            assert ntfy.requests == []
            env.clock.advance(5 * MIN)
            await until(lambda: len(ntfy.requests) == 1)
            await settle(20)
            await finish(env, task)

        asyncio.run(scenario())
        assert len(ntfy.requests) == 1
        assert ntfy.requests[0].headers["Title"] == "Garage door opened while away"


def test_restart_does_not_resend_an_alert_for_the_same_episode(tmp_path):
    with FakeNtfy() as ntfy:
        clock = Clock()
        first = make(tmp_path, ntfy=ntfy, clock=clock)

        async def run_first():
            task = await start(first)
            open_the_door(first.link)
            await settle(5)
            clock.advance(16 * MIN)
            await until(lambda: len(ntfy.requests) == 1)
            await settle(20)
            await finish(first, task)

        asyncio.run(run_first())
        assert "left_open" in json.loads((tmp_path / "garage.json").read_text())[
            "episode"
        ]["fired"]

        second = make(tmp_path, ntfy=ntfy, clock=clock)

        async def run_second():
            task = await start(second)
            second.link.connect()
            second.link.push(operation="idle", position=1.0)
            clock.advance(10 * MIN)
            await settle(30)
            await finish(second, task)

        asyncio.run(run_second())
        assert len(ntfy.requests) == 1


def test_garage_json_write_failure_is_logged_and_service_keeps_running(
    tmp_path, caplog
):
    env = make(tmp_path)

    def broken(_data):
        raise OSError("disk full: secret-detail")

    env.store.save = broken  # type: ignore[method-assign]

    async def scenario():
        task = await start(env)
        env.link.connect()
        env.link.push(operation="idle", position=0.0)
        await until(lambda: any(e["event"] == "closed" for e in door_lines(env)))
        await settle(10)
        assert not task.done()
        env.link.push(operation="opening")
        env.link.push(operation="idle", position=1.0)
        await until(lambda: any(e["event"] == "opened" for e in door_lines(env)))
        assert not task.done()
        await finish(env, task)

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "garage state could not be saved" in text
    assert "OSError" in text
    assert "secret-detail" not in text


class ExplodingLink(FakeGarageLink):
    """A link whose run raises a set number of times before it behaves."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures
        self.runs = 0

    async def run(self, on_state, on_connected):
        self.runs += 1
        if self.runs <= self.failures:
            raise RuntimeError("secret-detail")
        await super().run(on_state, on_connected)


def test_supervisor_restarts_after_exception_with_backoff_and_never_raises(
    tmp_path, caplog
):
    delays: list[float] = []

    async def pause(stop: asyncio.Event, seconds: float) -> None:
        delays.append(seconds)
        await asyncio.sleep(0)

    link = ExplodingLink(failures=7)
    env = make(tmp_path, link=link, pause=pause)

    async def scenario():
        task = asyncio.create_task(env.service.run(env.stop))
        await until(lambda: link.runs >= 8)
        await until(lambda: bound(link))
        assert not task.done()
        env.stop.set()
        await asyncio.wait_for(task, 3)
        assert task.exception() is None

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    assert delays[:7] == [5, 10, 20, 40, 80, 160, 300]
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in text
    assert "secret-detail" not in text


def test_supervisor_survives_a_failing_occupancy_source(tmp_path, caplog):
    env = make(tmp_path)

    def boom():
        raise ValueError("secret-detail")

    env.service._occupied = boom

    async def scenario():
        task = await start(env)
        open_the_door(env.link)
        await until(lambda: any(e["event"] == "opened" for e in door_lines(env)))
        assert not task.done()
        await finish(env, task)

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    assert "secret-detail" not in " ".join(r.getMessage() for r in caplog.records)


def test_service_exposes_the_api_target_surface(tmp_path):
    env = make(tmp_path)
    expected = GarageConfig(host="house.local").timeout_seconds
    assert env.service.timeout_seconds == expected
    assert len(env.service.actor_tag("person@example.invalid")) == 4
    assert "@" not in env.service.actor_tag("person@example.invalid")
    snapshot = env.service.snapshot()
    assert snapshot is not None
    assert snapshot["door"]["state"] == "unknown"
    assert snapshot["door"]["last_known_state"] is None


def test_build_returns_none_without_garage_config_and_logs_the_error_once(caplog):
    daemon = SimpleNamespace()
    bad = Config(garage=None, garage_error="garage host: is required.")
    with caplog.at_level(logging.WARNING):
        assert garage_service.build(bad, daemon) is None
    messages = [r.getMessage() for r in caplog.records if "host" in r.getMessage()]
    assert len(messages) == 1
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert garage_service.build(Config(), daemon) is None
    assert caplog.records == []


def test_build_wires_the_daemon_hooks_and_a_notifier(tmp_path):
    published = []
    daemon = SimpleNamespace(
        occupied=lambda: True,
        garage_settings=lambda: GarageSettings(),
        request_publish=lambda: published.append(1),
    )
    config = Config(
        state_dir=tmp_path,
        garage=GarageConfig(
            host="house.local",
            ntfy=NtfyConfig(url="http://127.0.0.1:9", topic="example-topic"),
        ),
    )
    link = FakeGarageLink()
    service = garage_service.build(config, daemon, link_factory=lambda cfg: link)
    assert isinstance(service, GarageService)
    assert service.timeout_seconds == config.garage.timeout_seconds
    service._request_publish()
    assert published == [1]
    assert service.snapshot() is not None


def test_build_without_a_link_adapter_yields_none_not_a_crash(tmp_path, caplog):
    daemon = SimpleNamespace(
        occupied=lambda: True,
        garage_settings=lambda: GarageSettings(),
        request_publish=lambda: None,
    )
    config = Config(state_dir=tmp_path, garage=GarageConfig(host="house.local"))

    def no_adapter(_cfg):
        raise ImportError("no adapter")

    with caplog.at_level(logging.WARNING):
        assert garage_service.build(config, daemon, link_factory=no_adapter) is None
    assert any("garage link" in r.getMessage() for r in caplog.records)

