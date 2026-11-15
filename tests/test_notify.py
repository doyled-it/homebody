import logging

import pytest

from garage_fakes import FakeNtfy
from homebody.garage_alerts import Alert
from homebody.notify import Notifier, NtfyConfig

ALERT = Alert("left_open", "Garage door open", "Open for 15 minutes.", "high")
CLICK = "https://example.invalid/#garage"


@pytest.fixture
def ntfy():
    with FakeNtfy() as fake:
        yield fake


def notifier(ntfy, sleeps=None, **config):
    cfg = NtfyConfig(
        url=ntfy.url, topic="example-topic", click_url=CLICK, **config
    )
    log = sleeps if sleeps is not None else []
    return Notifier(cfg, timeout=0.5, sleep=log.append)


def test_send_posts_title_priority_click_and_view_action(ntfy):
    assert notifier(ntfy).send_blocking(ALERT) is True
    (req,) = ntfy.requests
    assert req.method == "POST"
    assert req.path == "/example-topic"
    assert req.body == b"Open for 15 minutes."
    assert req.headers["Title"] == "Garage door open"
    assert req.headers["Priority"] == "high"
    assert req.headers["Click"] == CLICK
    assert req.headers["Actions"] == f"view, Open garage, {CLICK}"


def test_send_uses_bearer_token_only_when_configured(ntfy):
    notifier(ntfy).send_blocking(ALERT)
    assert "Authorization" not in ntfy.requests[-1].headers
    notifier(ntfy, token="example-token").send_blocking(ALERT)
    assert ntfy.requests[-1].headers["Authorization"] == "Bearer example-token"


def test_retries_three_times_then_returns_false(ntfy):
    sleeps = []
    ntfy.fail_next(5)
    assert notifier(ntfy, sleeps).send_blocking(ALERT) is False
    assert len(ntfy.requests) == 3
    assert 40 <= sum(sleeps) <= 70


def test_recovers_when_a_retry_succeeds(ntfy):
    ntfy.fail_next(2)
    assert notifier(ntfy).send_blocking(ALERT) is True
    assert len(ntfy.requests) == 3


def test_hung_server_times_out_and_returns_false(ntfy):
    for _ in range(3):
        ntfy.hang_next()
    assert notifier(ntfy).send_blocking(ALERT) is False
    assert len(ntfy.requests) == 3


def test_failure_logs_status_only_no_url_topic_or_token(ntfy, caplog):
    ntfy.fail_next(3, status=503)
    with caplog.at_level(logging.DEBUG):
        notifier(ntfy, token="example-token").send_blocking(ALERT)
    text = caplog.text
    assert "503" in text
    assert ntfy.url not in text
    assert "127.0.0.1" not in text
    assert "example-topic" not in text
    assert "example-token" not in text


def test_connection_failure_logs_no_url_topic_or_token(caplog):
    cfg = NtfyConfig("http://127.0.0.1:9", "example-topic", "example-token")
    with caplog.at_level(logging.DEBUG):
        assert Notifier(cfg, timeout=0.5, sleep=lambda s: None).send_blocking(
            ALERT
        ) is False
    assert "example-topic" not in caplog.text
    assert "example-token" not in caplog.text
    assert "127.0.0.1" not in caplog.text


def test_action_is_a_view_link_never_http_post(ntfy):
    notifier(ntfy).send_blocking(ALERT)
    actions = ntfy.requests[0].headers["Actions"]
    assert actions.startswith("view,")
    assert "http," not in actions


def test_no_click_headers_without_click_url(ntfy):
    cfg = NtfyConfig(ntfy.url, "example-topic")
    Notifier(cfg, timeout=0.5, sleep=lambda s: None).send_blocking(ALERT)
    headers = ntfy.requests[0].headers
    assert "Click" not in headers
    assert "Actions" not in headers


def test_async_send_runs_off_the_loop(ntfy):
    import asyncio

    assert asyncio.run(notifier(ntfy).send(ALERT)) is True
    assert len(ntfy.requests) == 1


def test_async_send_does_not_block_the_event_loop(ntfy):
    import asyncio

    ntfy.hang_next()

    async def run():
        ticks = 0
        done = asyncio.Event()

        async def ticker():
            nonlocal ticks
            while not done.is_set():
                ticks += 1
                await asyncio.sleep(0.01)

        task = asyncio.create_task(ticker())
        ok = await notifier(ntfy).send(ALERT)
        done.set()
        await task
        return ok, ticks

    ok, ticks = asyncio.run(run())
    assert ok is True
    assert ticks > 5


def test_shutdown_does_not_wait_for_a_send_stuck_in_backoff():
    import asyncio
    import threading
    import time

    gate = threading.Event()

    class Failing(Notifier):
        def _attempt(self, alert):
            return False

    cfg = NtfyConfig(url="http://127.0.0.1:1", topic="t")

    async def run():
        n = Failing(cfg, retries=3, sleep=lambda s: gate.wait(20))
        task = asyncio.create_task(n.send(ALERT))
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    started = time.monotonic()
    try:
        asyncio.run(run())
        elapsed = time.monotonic() - started
    finally:
        gate.set()
    assert elapsed < 3.0


def test_cancelled_send_stops_retrying():
    import asyncio

    calls = []

    class Failing(Notifier):
        def _attempt(self, alert):
            calls.append(1)
            return False

    cfg = NtfyConfig(url="http://127.0.0.1:1", topic="t")

    async def run():
        n = Failing(cfg, retries=3)
        task = asyncio.create_task(n.send(ALERT))
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0.2)

    asyncio.run(run())
    assert len(calls) == 1
