"""Drive deploy/update.sh against a temporary git repo with a shimmed installer.

update.sh fetches origin/main, checks it out, re-runs the installer, and keeps
the newest commit that verifies, falling back to the last known-good commit when
a commit fails. The installer is shimmed through HOMEBODY_INSTALL: it reads a
per-commit INSTALL_OK file (committed at that commit), records the commit as
last_good on success, and exits non-zero on failure, exactly as a real failed
verify would. git is real; systemctl is shimmed.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "update.sh"


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


def _commit(repo: Path, install_ok: str, message: str) -> str:
    (repo / "INSTALL_OK").write_text(install_ok)
    (repo / "MARKER").write_text(message)
    _git(repo, "add", "INSTALL_OK", "MARKER")
    _git(repo, "commit", "--quiet", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _write_shim(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _make_shims(tmp_path: Path) -> tuple[Path, Path, Path]:
    bin_dir = tmp_path / "bin"
    log = tmp_path / "shim.log"
    install = tmp_path / "install-shim.sh"
    state = tmp_path / "state" / "update.state"

    _write_shim(bin_dir / "systemctl", f'echo "systemctl $*" >> "{log}"\nexit 0\n')
    # The installer shim: succeed unless the checked-out INSTALL_OK says "fail".
    # On success it records HEAD as last_good (line 1), preserving the failed
    # line (line 2), just as deploy/install.sh does.
    _write_shim(
        install,
        f'echo "install [$(git rev-parse HEAD)]" >> "{log}"\n'
        'if [ "$(cat INSTALL_OK 2>/dev/null)" = fail ]; then exit 1; fi\n'
        f'state="{state}"\n'
        'mkdir -p "$(dirname "$state")"\n'
        'failed="$(sed -n 2p "$state" 2>/dev/null)"\n'
        'printf "%s\\n%s\\n" "$(git rev-parse HEAD)" "$failed" > "$state"\n'
        'exit 0\n',
    )
    return bin_dir, log, state


def _env(tmp_path: Path, repo: Path, bin_dir: Path, log: Path, state: Path) -> dict:
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["HOMEBODY_REPO"] = str(repo)
    env["HOMEBODY_STATE"] = str(state)
    env["HOMEBODY_INSTALL"] = str(tmp_path / "install-shim.sh")
    env["HOMEBODY_MATTER_SERVICE"] = "homebody-matter"
    env["SHIM_LOG"] = str(log)
    return env


def _origin(tmp_path: Path) -> tuple[Path, str]:
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "--quiet", "--initial-branch", "main")
    _git(origin, "config", "user.email", "ci@example.invalid")
    _git(origin, "config", "user.name", "CI")
    a = _commit(origin, "pass", "A")
    return origin, a


def _clone(origin: Path, dest: Path) -> Path:
    _git(dest.parent, "clone", "--quiet", str(origin), dest.name)
    _git(dest, "config", "user.email", "ci@example.invalid")
    _git(dest, "config", "user.name", "CI")
    return dest


def _run(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True
    )


def test_new_good_commit_becomes_last_good(tmp_path):
    origin, _a = _origin(tmp_path)
    repo = _clone(origin, tmp_path / "repo")
    bin_dir, log, state = _make_shims(tmp_path)
    env = _env(tmp_path, repo, bin_dir, log, state)

    b = _commit(origin, "pass", "B")
    result = _run(env)

    assert result.returncode == 0, result.stderr
    assert _git(repo, "rev-parse", "HEAD") == b
    assert state.read_text().splitlines()[0] == b
    assert "systemctl restart homebody-matter" in log.read_text()
    assert "systemctl restart homebody\n" in log.read_text()


def test_bad_commit_rolls_back_and_is_not_retried_until_main_moves(tmp_path):
    origin, _a = _origin(tmp_path)
    repo = _clone(origin, tmp_path / "repo")
    bin_dir, log, state = _make_shims(tmp_path)
    env = _env(tmp_path, repo, bin_dir, log, state)

    b = _commit(origin, "pass", "B")
    _run(env)
    assert _git(repo, "rev-parse", "HEAD") == b

    c = _commit(origin, "fail", "C")
    result = _run(env)
    assert result.returncode == 0, result.stderr
    # Rolled back to the last good commit B.
    assert _git(repo, "rev-parse", "HEAD") == b
    assert state.read_text().splitlines()[1] == c

    # Running again with main still at C does not re-checkout or retry C.
    log.write_text("")
    _run(env)
    assert _git(repo, "rev-parse", "HEAD") == b
    assert f"[{c}]" not in log.read_text()

    # Once main moves past C to a good D, updates resume.
    d = _commit(origin, "pass", "D")
    _run(env)
    assert _git(repo, "rev-parse", "HEAD") == d
    assert state.read_text().splitlines()[0] == d


def test_unreachable_origin_changes_nothing(tmp_path):
    origin, a = _origin(tmp_path)
    repo = _clone(origin, tmp_path / "repo")
    bin_dir, log, state = _make_shims(tmp_path)
    env = _env(tmp_path, repo, bin_dir, log, state)

    _git(repo, "remote", "set-url", "origin", str(tmp_path / "does-not-exist"))
    result = _run(env)

    assert result.returncode == 0, result.stderr
    assert _git(repo, "rev-parse", "HEAD") == a
    assert "install [" not in (log.read_text() if log.exists() else "")


def test_no_change_when_already_on_main(tmp_path):
    origin, a = _origin(tmp_path)
    repo = _clone(origin, tmp_path / "repo")
    bin_dir, log, state = _make_shims(tmp_path)
    env = _env(tmp_path, repo, bin_dir, log, state)

    result = _run(env)

    assert result.returncode == 0, result.stderr
    assert _git(repo, "rev-parse", "HEAD") == a
    # Nothing new on main, so the installer is not run.
    assert "install [" not in (log.read_text() if log.exists() else "")


def test_tampered_state_shas_are_ignored(tmp_path):
    origin, _a = _origin(tmp_path)
    repo = _clone(origin, tmp_path / "repo")
    bin_dir, log, state = _make_shims(tmp_path)
    env = _env(tmp_path, repo, bin_dir, log, state)

    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("not-a-sha\n$(touch pwned)\n")

    b = _commit(origin, "pass", "B")
    result = _run(env)

    assert result.returncode == 0, result.stderr
    assert not (repo / "pwned").exists()
    assert _git(repo, "rev-parse", "HEAD") == b
