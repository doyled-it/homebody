"""Drive deploy/verify.sh with a shimmed venv python and systemctl.

verify.sh is the check install.sh and the snaphouse role run: it exits 0 when the
venv imports homebody and, when the homebody-matter service is active, its
WebSocket answers on the loopback port. The venv python and systemctl are shimmed
onto a temp tree so the two branches can be driven without a real install or a
running controller.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "verify.sh"
BASH = shutil.which("bash") or "/bin/bash"


def _write_shim(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _env(
    tmp_path: Path,
    *,
    service_active: bool,
    ws_ok: bool,
    state: str | None = None,
    answers_after: int | None = None,
) -> dict:
    bin_dir = tmp_path / "bin"
    venv_bin = tmp_path / "venv" / "bin"
    log = tmp_path / "shim.log"

    # The venv python: `import homebody` always succeeds here; the WebSocket probe
    # (any other invocation) succeeds or fails per ws_ok. With answers_after=N the
    # controller is slow to start: probes 1..N-1 fail and probe N onward succeed.
    if answers_after is None:
        probe = f'exit {0 if ws_ok else 1}\n'
    else:
        probe = f'[ "$(wc -l < "{log}")" -ge {answers_after} ]\n'
    _write_shim(
        venv_bin / "python",
        'if [ "$1" = "-c" ]; then exit 0; fi\n'
        f'echo "ws-probe" >> "{log}"\n' + probe,
    )
    # systemctl is-active prints the unit state and exits 0 only for "active",
    # like the real one. state overrides the state implied by service_active.
    unit_state = state or ("active" if service_active else "inactive")
    _write_shim(
        bin_dir / "systemctl",
        f'echo "systemctl $*" >> "{log}.systemctl"\n'
        f'echo "{unit_state}"\n'
        f'exit {0 if unit_state == "active" else 3}\n',
    )

    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOMEBODY_VENV": str(tmp_path / "venv"),
        "HOMEBODY_MATTER_SERVICE": "homebody-matter",
        "SHIM_LOG": str(log),
        # Short deadline and interval so the retry tests stay fast.
        "HOMEBODY_VERIFY_DEADLINE": "2",
        "HOMEBODY_VERIFY_INTERVAL": "0.1",
    }
    return env


def _run(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [BASH, str(SCRIPT)], env=env, capture_output=True, text=True
    )


def test_passes_when_import_ok_and_service_inactive(tmp_path):
    env = _env(tmp_path, service_active=False, ws_ok=True)
    result = _run(env)
    assert result.returncode == 0, result.stderr
    # With the service inactive the websocket probe must not run.
    log = tmp_path / "shim.log"
    assert "ws-probe" not in (log.read_text() if log.exists() else "")


def test_passes_when_service_active_and_websocket_answers(tmp_path):
    env = _env(tmp_path, service_active=True, ws_ok=True)
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert "ws-probe" in (tmp_path / "shim.log").read_text()


def test_fails_when_service_active_but_websocket_silent(tmp_path):
    env = _env(tmp_path, service_active=True, ws_ok=False)
    result = _run(env)
    assert result.returncode != 0


def test_waits_for_a_slow_controller_that_starts_answering(tmp_path):
    # The unit is active but the controller only listens from the 3rd probe on.
    env = _env(tmp_path, service_active=True, ws_ok=False, answers_after=3)
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "shim.log").read_text().count("ws-probe") == 3


def test_waits_while_the_unit_is_activating(tmp_path):
    env = _env(
        tmp_path, service_active=True, ws_ok=False, state="activating", answers_after=2
    )
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "shim.log").read_text().count("ws-probe") == 2


def test_fails_after_the_deadline_when_controller_never_answers(tmp_path):
    env = _env(tmp_path, service_active=True, ws_ok=False)
    env["HOMEBODY_VERIFY_DEADLINE"] = "1"
    result = _run(env)
    assert result.returncode != 0
    assert "did not answer" in result.stderr
    # It retried rather than probing once.
    assert (tmp_path / "shim.log").read_text().count("ws-probe") > 1


def test_skips_the_probe_when_the_unit_is_inactive_even_with_a_deadline(tmp_path):
    env = _env(tmp_path, service_active=False, ws_ok=False)
    result = _run(env)
    assert result.returncode == 0, result.stderr
    log = tmp_path / "shim.log"
    assert "ws-probe" not in (log.read_text() if log.exists() else "")


def test_fails_when_import_fails(tmp_path):
    env = _env(tmp_path, service_active=False, ws_ok=True)
    # Replace the python shim so `import homebody` fails.
    _write_shim(
        tmp_path / "venv" / "bin" / "python",
        'if [ "$1" = "-c" ]; then exit 1; fi\nexit 0\n',
    )
    result = _run(env)
    assert result.returncode != 0
