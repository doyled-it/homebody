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


def _env(tmp_path: Path, *, service_active: bool, ws_ok: bool) -> dict:
    bin_dir = tmp_path / "bin"
    venv_bin = tmp_path / "venv" / "bin"
    log = tmp_path / "shim.log"

    # The venv python: `import homebody` always succeeds here; the WebSocket probe
    # (any other invocation) succeeds or fails per ws_ok.
    _write_shim(
        venv_bin / "python",
        'if [ "$1" = "-c" ]; then exit 0; fi\n'
        f'echo "ws-probe" >> "{log}"\n'
        f'exit {0 if ws_ok else 1}\n',
    )
    # systemctl is-active reports active or not per service_active.
    _write_shim(
        bin_dir / "systemctl",
        f'echo "systemctl $*" >> "{log}"\nexit {0 if service_active else 3}\n',
    )

    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOMEBODY_VENV": str(tmp_path / "venv"),
        "HOMEBODY_MATTER_SERVICE": "homebody-matter",
        "SHIM_LOG": str(log),
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


def test_fails_when_import_fails(tmp_path):
    env = _env(tmp_path, service_active=False, ws_ok=True)
    # Replace the python shim so `import homebody` fails.
    _write_shim(
        tmp_path / "venv" / "bin" / "python",
        'if [ "$1" = "-c" ]; then exit 1; fi\nexit 0\n',
    )
    result = _run(env)
    assert result.returncode != 0
