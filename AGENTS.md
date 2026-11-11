# AGENTS.md

Guidance for coding agents working in `homebody`. Read this before making
changes.

## What this is

homebody runs the house air conditioning from presence: comfort (72 F) when
anyone is home, eco (a warmer 82 F setpoint) once everyone has been gone for a
while. It decides presence from the house's own network (which phones are seen)
and applies the setpoint to a Google Nest thermostat over Matter. Nothing leaves
the LAN, and there is nothing identifying in this repo. The full design is in
`docs/superpowers/specs/2026-11-11-homebody-design.md`.

It runs on the snaphouse hub as two services: `homebody-matter` (the Matter
controller) and, in a later task, `homebody` (the presence-and-decision daemon).

## Commands

The project uses [uv](https://docs.astral.sh/uv/). Python 3.12+.

- `uv sync` installs dependencies into `.venv`.
- `uv run pytest` runs the test suite.
- `uv run ruff check` lints. Add `--fix` to apply safe fixes.
- `uv run homebody` runs the CLI entry point.

CI runs `uv sync`, `uv run ruff check`, `shellcheck deploy/*.sh`, and
`uv run pytest` on every push and pull request. Keep all four green.

## Layout

```
src/homebody/
  __init__.py   the version
  matter.py     the thin Matter controller client (WebSocket), unit conversions
  cli.py        the console entry point (homebody matter ...)
deploy/
  install.sh    root installer: user, config, dirs, uv venv, verify, last-good
  update.sh     self-update from main with a verify step and last-good fallback
  verify.sh     the health check: the venv imports, and the controller answers
local.example.toml   the config shape; install.sh seeds /etc/homebody/local.toml
tests/          pytest tests, including the deploy-script tests
```

The systemd units live in the snaphouse `homebody_bootstrap` role, not here.

## The Matter controller

homebody does not embed a Matter stack. The open-source controller,
[`python-matter-server`](https://github.com/home-assistant/python-matter-server),
runs as the `homebody-matter` service from this repo's venv
(`/opt/homebody/.venv/bin/matter-server`, bound to 127.0.0.1:5580). homebody
drives it over the controller's local WebSocket command protocol
(`src/homebody/matter.py`), so homebody imports no CHIP SDK.

`python-matter-server` is a Linux-only runtime dependency, gated in
`pyproject.toml` with `sys_platform == 'linux'`. Its CHIP SDK wheels
(`home-assistant-chip-core`) cover Linux x86_64 and aarch64 (the hub) and macOS
arm64, but not macOS x86_64 or Windows. Keeping it Linux-only lets `uv sync` and
the tests run on any dev machine; the tests never import it and run against a
fake controller instead. The hub (and CI, on Linux) installs it and gets the
`matter-server` entry point. If you touch the Matter dependency, check the
current package name and wheel platforms on PyPI before changing the marker, and
keep the tests on the fake controller.

Thermostat values: Matter temperatures are signed int16 in hundredths of a
degree Celsius, so 72 F is 2222 and 82 F is 2778. `matter.py` has the conversions
and the Thermostat cluster (0x0201) constants.

## Deployment and self-update

`deploy/install.sh` runs as root (the snaphouse role runs it): it ensures the
`homebody` system user, seeds `/etc/homebody/local.toml` from the example at mode
0640 root:homebody, creates the working directories under `/var/lib/homebody`,
syncs the venv with `uv sync --frozen --no-dev`, runs `deploy/verify.sh`, and
records the current commit as last-good. `deploy/update.sh` fetches `main` on a
timer, checks it out, re-runs the installer, and falls back to the last-good
commit when a commit fails verify, so a bad commit never sticks. The update state
lives root-owned in `/var/lib/homebody/update` and is trusted only when it is.

The scripts take `HOMEBODY_*` environment overrides so the tests can drive them
against temp trees with shimmed commands; the real defaults are the production
paths.

## Config and identifying data

The only identifying data on the hub (phone hardware addresses, the Nest's node
id, display names) lives in the untracked `/etc/homebody/local.toml`, seeded from
`local.example.toml`. Never put a name, email, address, home location, MAC, IP,
or pairing code in this repo, in a commit message, in a test, or in any web
request or search query. The example file uses generic placeholders only.

## Testing

Test-driven development: write the failing test, watch it fail, then implement.
Run the focused test while iterating and the full suite before committing. The
Matter client is tested against an in-memory fake controller (`tests/fakes.py`),
so no controller process or network is needed; the real WebSocket transport is
covered against a loopback server. The deploy scripts are tested by sourcing
their functions or running them against a temp git repo with shimmed `uv`,
`systemctl`, `chown`, and installer commands.

## No em dashes

Do not use em dashes anywhere: not in code, comments, docs, UI copy, commit
messages, or pull request text. Use a period, a colon, a semicolon, commas, or
parentheses instead.

## This is a multi-repo system

`homebody` is one repo among several; the hub is `snaphouse`, which installs and
self-updates homebody (the `homebody_bootstrap` role), runs its services, and
serves its status file and API through nginx. A feature usually spans repos, and
the seams (the install interface, the status and API contracts, and each repo's
docs) are where a change ships half-wired. Before calling a change done, cover
its full cross-repo surface. The repo map, the seams, and the definition of done
are in the hub repo: `snaphouse/docs/cross-repo-features.md`.
