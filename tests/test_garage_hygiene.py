"""Garage files must stay free of identifying data and em dashes."""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EM_DASH = chr(0x2014)

# Garage-owned files only. Older tests hold their own placeholder data.
GLOBS = [
    "src/homebody/garage*.py",
    "src/homebody/notify.py",
    "src/homebody/audit.py",
    "tests/garage_*",
    "tests/test_garage_*",
    "tests/test_notify.py",
    "tests/test_audit.py",
    "tests/loopback_esphome.py",
    "tests/fixtures/blaq*",
    "tests/fixtures/garage*",
    "local.example.toml",
]

MAC = re.compile(r"\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b")
IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
EMAIL = re.compile(r"[\w.+-]+@([\w-]+\.)+[A-Za-z]{2,}")

ALLOWED_IPS = {"127.0.0.1", "0.0.0.0"}
DOC_NET = ipaddress.ip_network("192.0.2.0/24")


def _files() -> list[Path]:
    found: set[Path] = set()
    for pattern in GLOBS:
        found.update(p for p in ROOT.glob(pattern) if p.is_file())
    return sorted(found)


def _mac_ok(text: str) -> bool:
    return text.lower().replace("-", ":").startswith("02:00:00:00:00:")


def _ip_ok(text: str) -> bool:
    if text in ALLOWED_IPS:
        return True
    try:
        return ipaddress.ip_address(text) in DOC_NET
    except ValueError:
        return True  # not an address (a version string such as 1.2.3.4)


def _email_ok(text: str) -> bool:
    return text.lower().endswith(".invalid") and "example" in text.lower()


def test_hygiene_no_em_dash_or_identifying_patterns() -> None:
    files = _files()
    assert any(p.name == "garage_link.py" for p in files)
    problems: list[str] = []
    for path in files:
        if path.name == Path(__file__).name:
            continue
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(ROOT)
        if EM_DASH in text:
            problems.append(f"{rel}: em dash")
        for m in MAC.finditer(text):
            if not _mac_ok(m.group()):
                problems.append(f"{rel}: MAC address")
        for m in IPV4.finditer(text):
            if not _ip_ok(m.group()):
                problems.append(f"{rel}: IPv4 address")
        for m in EMAIL.finditer(text):
            if not _email_ok(m.group()):
                problems.append(f"{rel}: email address")
    assert not problems, problems
