"""The ntfy client: one POST per alert, bounded retries, never raises.

Failures log a status or an error class only. The URL, topic and token are
never written to a log line.
"""

from __future__ import annotations

import asyncio
import logging
import time
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
        sleep: Callable[[float], None] = time.sleep,
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

    def send_blocking(self, alert: Alert) -> bool:
        for attempt in range(self._retries):
            if attempt:
                index = min(attempt - 1, len(_BACKOFF_SECONDS) - 1)
                self._sleep(_BACKOFF_SECONDS[index])
            if self._attempt(alert):
                return True
        log.warning(
            "ntfy alert %s dropped after %d attempts", alert.kind, self._retries
        )
        return False

    async def send(self, alert: Alert) -> bool:
        return await asyncio.to_thread(self.send_blocking, alert)
