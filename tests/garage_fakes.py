"""Shared doubles for the garage tests: a fake board link and a fake ntfy server."""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Literal

from homebody.garage_link import LinkError, LinkState


class FakeGarageLink:
    """An in-memory GarageLink driven by the test.

    The test calls ``connect``/``drop``/``push``; ``bind`` stands in for the
    callbacks ``run`` would receive. A drop resets the merged state, as the
    Protocol requires.
    """

    def __init__(self, shape: Literal["position", "binary"] = "position") -> None:
        self.shape = shape
        self.state = LinkState(has_position=shape == "position")
        self.connected = False
        self.sent: list[str] = []
        self.swallow = False
        self.fail_send = False
        self.follow_commands = True
        self._pending: str | None = None
        self._on_state: Callable[[LinkState], None] = lambda _s: None
        self._on_connected: Callable[[bool], None] = lambda _c: None

    def bind(
        self,
        on_state: Callable[[LinkState], None],
        on_connected: Callable[[bool], None],
    ) -> None:
        self._on_state = on_state
        self._on_connected = on_connected

    async def run(
        self,
        on_state: Callable[[LinkState], None],
        on_connected: Callable[[bool], None],
    ) -> None:
        import asyncio

        self.bind(on_state, on_connected)
        await asyncio.Event().wait()

    def connect(self) -> None:
        self.connected = True
        self._on_connected(True)

    def drop(self) -> None:
        self.connected = False
        self._pending = None
        self.state = LinkState(has_position=self.shape == "position")
        self._on_connected(False)

    def push(self, **fields: Any) -> None:
        if self.shape == "binary" and fields.get("position") is not None:
            fields["position"] = 0.0 if fields["position"] <= 0.0 else 1.0
        self.state = dataclasses.replace(self.state, **fields)
        self._on_state(self.state)

    async def send(self, action: Literal["open", "close"]) -> None:
        if not self.connected:
            raise LinkError("not connected")
        if self.fail_send:
            raise LinkError("send failed")
        self.sent.append(action)
        if self.swallow or not self.follow_commands:
            return
        self._pending = action
        self.push(operation="closing" if action == "close" else "opening")

    def settle(self) -> None:
        """Finish the movement a followed command started."""
        action, self._pending = self._pending, None
        if action is None:
            return
        self.push(operation="idle", position=0.0 if action == "close" else 1.0)


@dataclass
class RecordedRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


class FakeNtfy:
    """A loopback HTTP server that records requests and can fail or hang."""

    def __init__(self) -> None:
        self.requests: list[RecordedRequest] = []
        self._failures: list[int] = []
        self._hang = 0
        self._lock = threading.Lock()
        self._release = threading.Event()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                with fake._lock:
                    fake.requests.append(
                        RecordedRequest(
                            self.command, self.path, dict(self.headers.items()), body
                        )
                    )
                    hang = fake._hang > 0
                    if hang:
                        fake._hang -= 1
                    status = fake._failures.pop(0) if fake._failures else 200
                if hang:
                    fake._release.wait(timeout=30)
                    return
                self.send_response(status)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            do_POST = do_PUT = do_GET = _handle

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def fail_next(self, n: int = 1, status: int = 500) -> None:
        with self._lock:
            self._failures.extend([status] * n)

    def hang_next(self) -> None:
        with self._lock:
            self._hang += 1

    def close(self) -> None:
        self._release.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> FakeNtfy:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

