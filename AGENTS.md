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

The project uses [uv](https://docs.astral.sh/uv/). Python 3.12+, pinned to 3.12 by
`.python-version` (see below).

- `uv sync` installs dependencies into `.venv`.
- `uv run pytest` runs the test suite.
- `uv run ruff check` lints. Add `--fix` to apply safe fixes.
- `uv run homebody` runs the CLI entry point.

CI runs `uv python install` (which reads `.python-version`), `uv sync --frozen`, `uv run ruff check`, `shellcheck deploy/*.sh`, and
`uv run pytest` on every push and pull request. Keep all four green.

## Layout

```
src/homebody/
  __init__.py   the version
  matter.py     the thin Matter controller client (WebSocket), unit conversions
  config.py     reads /etc/homebody/local.toml into typed settings
  presence.py   who is home: Wi-Fi and Bluetooth probes, the 20 minute window
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

## Presence

`presence.py` decides who is home from injected probes and an injected clock.
Wi-Fi (`NeighbourWifiProbe`) reads `ip -4 neigh show` and confirms a non-REACHABLE
entry with one `arping` of the last known IP. Bluetooth (`L2pingBluetoothProbe`)
runs one `l2ping` through the adapter from `[bluetooth] adapter` in the local
config, only after 5 minutes without Wi-Fi and at most once a minute. A person is
home for 20 minutes after the last sighting. After a restart presence is unknown,
not away, until someone is seen or a full window has been watched. Disabled people
are skipped by every probe. `set_enabled(id, flag)` flips a person at runtime (the
daemon persists the flag in its state and applies it over the local.toml default);
off stops probing and counting at once, on restarts observation from unknown.

Privilege: `arping` and `l2ping` open raw sockets, which need root or
`CAP_NET_RAW`. The `homebody` service user is not root, so the systemd unit (owned
by the snaphouse `homebody_bootstrap` role) must set `AmbientCapabilities=CAP_NET_RAW`
and `CapabilityBoundingSet=CAP_NET_RAW`, and if it uses `RestrictAddressFamilies`
allow `AF_INET AF_NETLINK AF_BLUETOOTH`. The hub also needs `iproute2`,
`iputils-arping`, and `bluez` (for `l2ping`) installed. The unit lands with the
daemon task; until then nothing runs these probes.

## The Python pin

`.python-version` pins uv to 3.12. Without it, uv on a host whose system Python is
older than `requires-python` allows picks the newest managed Python, which once
was a release where aiohttp (via python-matter-server) has no wheels, so the hub
venv stayed empty. CI installs the same pin, and `tests/test_python_pin.py` fails
if the pin is missing or CI selects a different Python. Move the pin and CI
together, after checking that the dependencies publish wheels for the new series.

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
