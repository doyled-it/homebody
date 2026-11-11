"""Drive the unprivileged, shimmable parts of deploy/install.sh.

The script guards its ``main`` call so it can be sourced and its functions
exercised here without the real (root-only) install. The venv sync, config
seeding, and last-good recording are driven with git and shimmed chown so they
run without root: the parts that actually need root (user and directory
ownership) are covered by the snaphouse role's container-apply test, not here.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "install.sh"
BASH = shutil.which("bash") or "/bin/bash"


def _write_shim(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


def test_sync_venv_uses_frozen_no_dev_and_shared_python(tmp_path):
    bin_dir = tmp_path / "bin"
    log = tmp_path / "shim.log"
    _write_shim(
        bin_dir / "uv",
        f'echo "uv $* [py=$UV_PYTHON_INSTALL_DIR]" >> "{log}"\nexit 0\n',
    )
    repo = tmp_path / "repo"
    repo.mkdir()

    env = {"PATH": str(bin_dir), "SHIM_LOG": str(log)}
    result = subprocess.run(
        [BASH, "-c", f'source "{SCRIPT}"; HOMEBODY_REPO="{repo}"; sync_venv'],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    uv_line = next(
        line for line in log.read_text().splitlines() if line.startswith("uv sync")
    )
    assert "--frozen" in uv_line
    assert "--no-dev" in uv_line
    # The managed Python must land where the unprivileged service user can reach
    # it, not root's private uv directory.
    assert "[py=/opt/uv/python]" in uv_line


def test_seed_config_creates_local_toml_with_restrictive_mode(tmp_path):
    bin_dir = tmp_path / "bin"
    log = tmp_path / "shim.log"
    # chown needs root in reality; shim it so the mode/copy logic runs as a user.
    _write_shim(bin_dir / "chown", f'echo "chown $*" >> "{log}"\nexit 0\n')

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "local.example.toml").write_text("# example\n")
    config_dir = tmp_path / "etc"

    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "SHIM_LOG": str(log)}
    result = subprocess.run(
        [
            BASH,
            "-c",
            f'source "{SCRIPT}"; HOMEBODY_REPO="{repo}"; '
            f'HOMEBODY_CONFIG_DIR="{config_dir}"; seed_config',
        ],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    local = config_dir / "local.toml"
    assert local.exists()
    assert oct(local.stat().st_mode & 0o777) == "0o640"
    chown_line = next(
        line for line in log.read_text().splitlines() if line.startswith("chown")
    )
    assert "root:homebody" in chown_line


def test_seed_config_does_not_overwrite_existing_local_toml(tmp_path):
    bin_dir = tmp_path / "bin"
    log = tmp_path / "shim.log"
    _write_shim(bin_dir / "chown", f'echo "chown $*" >> "{log}"\nexit 0\n')

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "local.example.toml").write_text("# example\n")
    config_dir = tmp_path / "etc"
    config_dir.mkdir()
    (config_dir / "local.toml").write_text("real = true\n")

    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "SHIM_LOG": str(log)}
    result = subprocess.run(
        [
            BASH,
            "-c",
            f'source "{SCRIPT}"; HOMEBODY_REPO="{repo}"; '
            f'HOMEBODY_CONFIG_DIR="{config_dir}"; seed_config',
        ],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (config_dir / "local.toml").read_text() == "real = true\n"


def test_record_last_good_writes_the_current_commit(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch", "main")
    _git(repo, "config", "user.email", "ci@example.invalid")
    _git(repo, "config", "user.name", "CI")
    (repo / "x").write_text("1")
    _git(repo, "add", "x")
    _git(repo, "commit", "--quiet", "-m", "one")
    head = _git(repo, "rev-parse", "HEAD")

    state = tmp_path / "state" / "update.state"
    env = {"PATH": "/usr/bin:/bin", "SHIM_LOG": str(tmp_path / "shim.log")}
    result = subprocess.run(
        [
            BASH,
            "-c",
            f'source "{SCRIPT}"; HOMEBODY_REPO="{repo}"; '
            f'HOMEBODY_STATE="{state}"; record_last_good',
        ],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert state.read_text().splitlines()[0] == head
