"""The audit log: JSON lines, rotated, tagged, never raising."""

from __future__ import annotations

import json
import os
import re

import pytest

from homebody.audit import AuditLog


class Clock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def door(log: AuditLog, event: str, **extra: object) -> None:
    fields: dict = {"origin": "home", "actor": None, "door": "closed", "result": "ok"}
    fields.update(extra)
    log.record(event, kind="door", **fields)


def test_record_and_recent_roundtrip_newest_last_limited_to_10(tmp_path):
    clock = Clock()
    log = AuditLog(tmp_path, clock)
    for i in range(12):
        clock.now += 1
        door(log, "opened" if i % 2 else "closed", actor=f"a{i}")
    recent = log.recent()
    assert len(recent) == 10
    assert [r["actor"] for r in recent] == [f"a{i}" for i in range(2, 12)]
    assert set(recent[0]) == {"at", "event", "origin", "actor", "result"}
    assert recent[-1]["event"] == "opened"
    assert recent[-1]["at"] == "2023-11-14T22:13:32Z"
    assert len(log.recent(limit=3)) == 3


def test_recent_holds_door_changes_only_command_lines_stay_in_the_log(tmp_path):
    log = AuditLog(tmp_path, Clock())
    log.record("close", origin="tunnel", actor="ab12", door="open", result="sent")
    door(log, "closed", origin="tunnel", actor="ab12")
    log.record(
        "close", origin="tunnel", actor="ab12", door="open", result="late confirm"
    )
    assert [r["event"] for r in log.recent()] == ["closed"]
    lines = (tmp_path / "garage-audit.log").read_text().splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["kind"] == "command"


def test_tag_is_stable_and_four_hex_and_differs_per_identity(tmp_path):
    log = AuditLog(tmp_path, Clock())
    first = log.tag("person-a@example.invalid")
    assert re.fullmatch(r"[0-9a-f]{4}", first)
    assert log.tag("person-a@example.invalid") == first
    assert AuditLog(tmp_path, Clock()).tag("person-a@example.invalid") == first
    tags = {log.tag(f"person-{i}@example.invalid") for i in range(20)}
    assert len(tags) > 10
    key = tmp_path / "garage-audit.key"
    assert key.exists()
    assert (key.stat().st_mode & 0o777) == 0o600


def test_log_contains_no_email_or_ip(tmp_path):
    log = AuditLog(tmp_path, Clock())
    email = "person-a@example.invalid"
    ip = "192.0.2.77"
    log.record(
        f"close from {email}",
        origin="tunnel",
        actor=log.tag(email),
        door="open",
        result=f"sent to {ip} for {email}",
    )
    raw = (tmp_path / "garage-audit.log").read_text()
    assert email not in raw
    assert ip not in raw
    assert "@" not in raw
    assert not re.search(r"\d+\.\d+\.\d+\.\d+", raw)


def test_rotates_at_max_bytes_keeping_two_old_files(tmp_path):
    log = AuditLog(tmp_path, Clock(), max_bytes=400, keep=2)
    for i in range(60):
        door(log, "opened", actor=f"n{i:03d}")
    names = sorted(
        p.name for p in tmp_path.iterdir() if p.name.startswith("garage-audit.log")
    )
    assert names == ["garage-audit.log", "garage-audit.log.1", "garage-audit.log.2"]
    for name in names:
        assert (tmp_path / name).stat().st_size <= 400
    assert log.recent()[-1]["actor"] == "n059"


def test_disk_error_is_swallowed_and_logged(tmp_path, caplog):
    target = tmp_path / "state"
    target.mkdir()
    log = AuditLog(target, Clock())
    os.chmod(target, 0o500)
    try:
        if os.access(target, os.W_OK):
            pytest.skip("directory permissions are not enforced here")
        with caplog.at_level("WARNING"):
            log.record("close", origin="home", actor=None, door="open", result="sent")
        assert "audit" in caplog.text.lower()
        assert log.tag("x") != ""
        assert log.recent() == []
    finally:
        os.chmod(target, 0o700)
