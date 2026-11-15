"""Tests for the garage link doubles and the pinned blaQ entity fixture."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from garage_fakes import FakeGarageLink, FakeNtfy
from homebody.garage_link import DEFAULT_ENTITY_NAMES, LinkError, LinkState

FIXTURE = Path(__file__).parent / "fixtures" / "blaq_entities.json"

TYPE_FOR_ROLE = {
    "cover": "cover",
    "obstruction": "binary_sensor",
    "lockout": "lock",
    "light": "light",
    "motion": "binary_sensor",
}


def _collect(link: FakeGarageLink) -> tuple[list[LinkState], list[bool]]:
    states: list[LinkState] = []
    connected: list[bool] = []
    link.bind(states.append, connected.append)
    return states, connected


def test_blaq_fixture_names_match_defaults() -> None:
    data = json.loads(FIXTURE.read_text())
    pairs = {(e["type"], e["name"]) for e in data["entities"]}
    assert data["source"]
    for role, name in DEFAULT_ENTITY_NAMES.items():
        assert (TYPE_FOR_ROLE[role], name) in pairs, role


def test_fake_link_position_shape_reports_intermediate_positions() -> None:
    link = FakeGarageLink(shape="position")
    states, _ = _collect(link)
    link.connect()
    link.push(operation="idle", position=0.0)
    link.push(operation="opening", position=0.4)
    assert states[-1].position == 0.4
    assert states[-1].has_position is True
    assert states[-1].operation == "opening"


def test_fake_link_binary_shape_reports_only_0_or_1() -> None:
    link = FakeGarageLink(shape="binary")
    states, _ = _collect(link)
    link.connect()
    link.push(operation="idle", position=0.0)
    link.push(operation="opening", position=0.4)
    assert states[-1].has_position is False
    assert states[-1].position in (0.0, 1.0)


def test_fake_link_drop_calls_on_connected_false_and_clears_nothing_silently() -> None:
    link = FakeGarageLink()
    states, connected = _collect(link)
    link.connect()
    link.push(operation="idle", position=0.0, obstruction=False)
    link.drop()
    assert connected == [True, False]
    # A drop resets the merged state: the next push does not carry stale fields.
    link.connect()
    link.push(obstruction=True)
    assert states[-1].operation is None
    assert states[-1].position is None
    assert states[-1].obstruction is True


def test_fake_link_send_when_dropped_raises_link_error() -> None:
    link = FakeGarageLink()
    _collect(link)
    with pytest.raises(LinkError):
        asyncio.run(link.send("close"))
    link.connect()
    link.drop()
    with pytest.raises(LinkError):
        asyncio.run(link.send("open"))


def test_fake_link_follow_commands_pushes_closing_then_closed() -> None:
    link = FakeGarageLink()
    states, _ = _collect(link)
    link.connect()
    link.push(operation="idle", position=1.0)
    asyncio.run(link.send("close"))
    assert link.sent == ["close"]
    assert states[-1].operation == "closing"
    link.settle()
    assert states[-1].operation == "idle"
    assert states[-1].position == 0.0


def test_fake_link_swallow_and_fail_send() -> None:
    link = FakeGarageLink()
    states, _ = _collect(link)
    link.connect()
    link.push(operation="idle", position=1.0)
    link.swallow = True
    asyncio.run(link.send("close"))
    assert link.sent == ["close"]
    assert states[-1].operation == "idle"
    link.fail_send = True
    with pytest.raises(LinkError):
        asyncio.run(link.send("close"))


def _post(url: str, body: bytes = b"hi", timeout: float = 5.0) -> int:
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as err:
        return err.code


def test_fake_ntfy_records_request_and_can_fail_then_succeed() -> None:
    with FakeNtfy() as ntfy:
        ntfy.fail_next(1, status=503)
        assert _post(ntfy.url + "/example-topic") == 503
        assert _post(ntfy.url + "/example-topic", b"door open") == 200
        assert len(ntfy.requests) == 2
        last = ntfy.requests[-1]
        assert last.method == "POST"
        assert last.path == "/example-topic"
        assert last.body == b"door open"
        assert "Content-Length" in last.headers or "content-length" in last.headers
