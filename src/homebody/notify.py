"""The ntfy client: one POST per alert, bounded retries, never raises.

Failures log a status or an error class only. The URL, topic and token are
never written to a log line.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from homebody.garage_alerts import Alert

log = logging.getLogger(__name__)

# Waits before the second and third attempts: about a minute in all.
_BACKOFF_SECONDS = (20.0, 40.0)


@dataclass(frozen=True)
class NtfyConfig:
    url: str
    topic: str
    token: str | None = None
    click_url: str | None = None


class Notifier:
    def __init__(
        self,
        config: NtfyConfig,
        *,
        timeout: float = 5.0,
        retries: int = 3,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._config = config
        self._timeout = timeout
        self._retries = max(1, retries)
        self._sleep = sleep

    def _request(self, alert: Alert) -> urllib.request.Request:
        cfg = self._config
        headers = {"Title": alert.title, "Priority": alert.priority}
        if cfg.click_url:
            headers["Click"] = cfg.click_url
            headers["Actions"] = f"view, Open garage, {cfg.click_url}"
        if cfg.token:
            headers["Authorization"] = f"Bearer {cfg.token}"
        url = f"{cfg.url.rstrip('/')}/{cfg.topic}"
        return urllib.request.Request(
            url, data=alert.body.encode("utf-8"), headers=headers, method="POST"
        )

    def _attempt(self, alert: Alert) -> bool:
        try:
            request = self._request(alert)
            with urllib.request.urlopen(request, timeout=self._timeout) as resp:
                status = resp.status
        except urllib.error.HTTPError as err:
            status = err.code
            err.close()
        except Exception as err:  # noqa: BLE001 - never raise, never log the text
            log.warning("ntfy send failed: %s", type(err).__name__)
            return False
        if 200 <= status < 300:
            return True
        log.warning("ntfy send failed: status %s", status)
        return False

    def send_blocking(
        self, alert: Alert, cancelled: threading.Event | None = None
    ) -> bool:
        """Send with bounded retries. When ``cancelled`` is set the backoff
        ends at once and no further attempt is made."""
        stop = cancelled or threading.Event()
        for attempt in range(self._retries):
            if attempt:
                index = min(attempt - 1, len(_BACKOFF_SECONDS) - 1)
                wait = _BACKOFF_SECONDS[index]
                if self._sleep is not None:
                    self._sleep(wait)
                else:
                    stop.wait(wait)
            if stop.is_set():
                return False
            if self._attempt(alert):
                return True
        log.warning(
            "ntfy alert %s dropped after %d attempts", alert.kind, self._retries
        )
        return False

    async def send(self, alert: Alert) -> bool:
        """Send on a daemon thread. Cancelling stops further retries, and the
        thread never holds up process exit (unlike the default executor)."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        cancelled = threading.Event()

        def deliver(ok: bool) -> None:
            if not future.done():
                future.set_result(ok)

        def work() -> None:
            try:
                ok = self.send_blocking(alert, cancelled)
            except Exception as err:  # noqa: BLE001 - never raise
                log.warning("ntfy send failed: %s", type(err).__name__)
                ok = False
            try:
                loop.call_soon_threadsafe(deliver, ok)
            except RuntimeError:
                pass  # the loop is already closed

        threading.Thread(target=work, name="ntfy-send", daemon=True).start()
        try:
            return await future
        except asyncio.CancelledError:
            cancelled.set()
            raise
