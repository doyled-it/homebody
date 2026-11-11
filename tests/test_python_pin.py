"""Keep the Python pin and the CI Python in step.

uv picks the newest managed Python when the host's own Python is older than
requires-python allows. On the hub that was a release with no aiohttp wheels, so
the venv stayed empty. A committed .python-version pins uv to one minor series,
and CI must test on that same series, so a green CI means the hub installs from
wheels. These tests fail if the pin is missing, malformed, outside
requires-python, or if the CI workflow selects a different Python.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIN_FILE = ROOT / ".python-version"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _pin() -> str:
    assert PIN_FILE.is_file(), ".python-version is missing; uv floats to the newest"
    return PIN_FILE.read_text().strip()


def _minor(version: str) -> str:
    match = re.fullmatch(r"(\d+)\.(\d+)(?:\.\d+)?", version)
    assert match, f"not a plain X.Y or X.Y.Z version: {version!r}"
    return f"{match.group(1)}.{match.group(2)}"


def test_pin_is_a_plain_version() -> None:
    _minor(_pin())


def test_pin_satisfies_requires_python() -> None:
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text())
    floor = re.fullmatch(r">=\s*(\d+)\.(\d+)", meta["project"]["requires-python"])
    assert floor, "expected requires-python of the form >=X.Y"
    major, minor = (int(p) for p in _minor(_pin()).split("."))
    assert (major, minor) >= (int(floor.group(1)), int(floor.group(2)))


def test_ci_installs_the_pinned_python() -> None:
    text = WORKFLOW.read_text()
    bare_install = re.search(r"^\s*run:\s*uv python install\s*$", text, re.M)
    version_file = re.search(r"python-version-file:\s*['\"]?\.python-version", text)
    reads_pin_file = bare_install or version_file
    explicit = re.search(
        r"(?:python-version:\s*['\"]?|uv python install\s+)(\d+(?:\.\d+)*)",
        text,
    )
    if explicit:
        assert _minor(explicit.group(1)) == _minor(_pin()), (
            f"CI selects Python {explicit.group(1)} but .python-version pins {_pin()}"
        )
    else:
        assert reads_pin_file, "CI does not install the pinned Python"


def test_ci_does_not_select_any_other_python() -> None:
    text = WORKFLOW.read_text()
    pinned = _minor(_pin())
    versions = re.findall(
        r"(?:python-version:\s*['\"]?|uv python install\s+|--python\s+)(\d+\.\d+)",
        text,
    )
    assert all(v == pinned for v in versions), f"CI uses {versions}, pin is {pinned}"
