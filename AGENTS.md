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
controller) and `homebody` (the presence-and-decision daemon, `homebody run`).

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
  climate.py    pure decisions: transitions, override, pacing, backoff
  daemon.py     the service loop, the atomic state file, runtime settings
  status.py     the homebody.json document and its atomic writer
  api.py        the settings HTTP service (asyncio, stdlib only)
  cli.py        the console entry point (homebody matter ..., homebody run)
  garage.py          the door model: states, open episodes, garage.json store
  garage_link.py     the board link types and the GarageLink Protocol
  garage_alerts.py   pure alert rules (left open, opened away, offline, ...)
  garage_commands.py the command gate: nonce, origin rules, pacing, confirm
  garage_service.py  the supervised garage task and its build() from config
  notify.py          the ntfy client (one POST per alert, bounded retries)
  audit.py           garage-audit.log: JSON lines, rotated, actor tags only
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

## Decisions and the daemon

`climate.py` is pure logic with an injected clock. Occupied is Cool at the comfort
setpoint, unoccupied is Cool at the away setpoint, and presence "unknown" never
triggers anything. A command is owed once per occupied/unoccupied transition.
A transition always takes over: its target is sent whatever the thermostat
reads (a schedule or a person may have moved it). Before a command owed for
another reason (a settings change), the thermostat is read and compared with
what homebody last set (setpoints within 0.25 C count as equal, because the Nest
rounds to half degrees); a difference is a hand change, so an override is
recorded and nothing is sent until the next transition. Transitions that pile up during an outage collapse, since the target is
always computed from the latest occupancy. Commands are paced to one a minute
(the mode and setpoint writes are one command) and failures back off from 60 s
to 15 min. A write that failed part way is retried without the hand-change
check, so a half-applied command is not mistaken for a person.

`daemon.py` ticks every 15 s: poll presence (in a thread, the probes block), feed
the occupancy to the controller, and when a command is owed read the thermostat
and write the target. Any Matter or thermostat failure (including
`MatterConnectionError`, which `WebSocketConnection.command()` now raises for a
dropped socket) drops the connection, backs off, and reconnects later; presence
keeps tracking and the loop never raises. `homebody run` starts it; it handles
SIGTERM.

State is saved to `state.json` in the state dir (`[daemon] state_dir`, default
`/var/lib/homebody/state`, which deploy/install.sh creates owned by the service user;
the parent `/var/lib/homebody` is root-owned) with a write-temp, fsync,
replace: `climate` (last commanded mode and setpoint in Matter units, the override
flag, whether a transition is pending, the last transition and last change
times) and `enabled` (runtime enable flags per person id, applied over the
local.toml defaults at start). A missing or damaged file starts fresh. The daemon
reads `[matter] url` and `node_id` from the config (node id 0 finds the first
thermostat); `HOMEBODY_MATTER_URL` overrides the URL. The `homebody` systemd
unit lands with the snaphouse role in the next task; `deploy/update.sh` already
restarts it alongside `homebody-matter`.

## Status file and settings API

`status.py` builds the status document (shape in the hub repo's
`docs/cross-repo-features.md`; every key always present, null when unknown,
enabled people only, display names, UTC timestamps in whole seconds) and
`StatusWriter` replaces `homebody.json` atomically (temp in the same directory,
fsync, rename, mode 0644). `[status] dir` (default
`/var/lib/snaphouse/public/homebody`) must be owned by the service user. The
daemon writes it after every tick and right after a settings change. To keep
the thermostat fields fresh it also reads the thermostat once a minute when no
command is owed (read only; a failed read marks `reachable` false but does not
touch the command backoff).

`api.py` serves `POST /api/homebody/settings` on 127.0.0.1 (`[api] port`,
default 8783) inside `homebody run`, with a pure `handle()` function and a
minimal asyncio HTTP server. Header guard `X-Snaphouse: 1` (403), validation
(400), other paths 404, other methods 405. A port that cannot be bound is
logged and the daemon runs without the API.

Settings live in the daemon state, not in `local.toml`: `state.json` has a
`settings` object holding only the keys changed through the API, which override
the `[climate]` defaults (`local.toml` stays read-only to homebody). `enabled`
is one global automation switch: off keeps presence tracking and publishing but
sends no commands; transitions that happen meanwhile stay pending and are acted
on after it is turned back on (subject to pacing and the override rule). This is
separate from the per-person runtime flags (`Daemon.set_enabled`, the `enabled`
map in state), which the API does not expose. A changed `away_minutes` applies
to the presence window at once. A changed `comfort_f` or `away_f` makes the
climate controller owe a command (`ClimateController.set_setpoints`) unless a
hand-change override is active, which still holds until the next transition.

People endpoints: `GET /api/homebody/people/this-device`, `POST
/api/homebody/people/add` (`{"name"}`) and `POST /api/homebody/people/remove`
(`{"id"}`). `api.resolve_device` finds the caller's address: a request with
`cf-connecting-ip` came through the tunnel from away and cannot add (403,
`reason` `remote`, checked first); otherwise `x-real-ip` (set by the hub's nginx)
is looked up with `presence.neighbour_mac` (`ip -4 neigh show <ip>`, IPv4 only,
`FAILED`/`INCOMPLETE` ignored), and no answer is 403 `unknown`. Remove is allowed
through the tunnel. `handle` takes the lookup as a parameter and `api.start`
passes it from `cli._start_api`, so tests inject a fake. Added people are
tracked by Wi-Fi only, and adds and removes are logged by id, never address or
name. The `X-Real-IP` and `CF-Connecting-IP` headers must be passed by the hub's
nginx (a snaphouse change).

## Garage

The garage door module watches and (from the house or, if switched on, the
tunnel) commands a Konnected blaQ board running ESPHome. It is off unless
`[garage]` is in `local.toml`; a bad `[garage]` turns only the garage off and
logs why (the field, never the value), and homebody starts as it always did.

Isolation rule: a garage failure must never stop climate. The service
(`garage_service.GarageService`) is its own asyncio task, started next to the
daemon in `cli._serve_async`. It never runs inside `Daemon.tick`, takes no lock
shared with climate, imports no climate module, and writes only `garage.json`
and `garage-audit.log` (never `homebody.json`; the daemon is the single
writer). Its supervisor (`run`) catches every exception, logs the exception
type only, and restarts the loop with a backoff of 5 s doubling to 5 min. ntfy
sends run in a worker thread, off the loop's path.

The three meeting points with the daemon, all non-blocking:

1. The service reads `Daemon.occupied()` and `Daemon.garage_settings()`.
2. `Daemon.publish()` includes `garage.snapshot()` as the `garage` key
   (`Daemon.attach_garage`), `null` if it raises. When the door or link changes
   the service calls `Daemon.request_publish()`, which wakes `Daemon.run` to
   publish only (no tick).
3. The API handler calls the command gate (`api.start(garage=service)`), under
   its own cap, `GarageService.timeout_seconds` (close wait plus note wait plus
   headroom).

Files in `[daemon] state_dir`: `garage.json` (the door's last known state, since,
last seen, and the open episode with the alert kinds already sent, so a restart
does not resend; written atomically, a failed write is logged and ignored) and
`garage-audit.log` (JSON lines, 256 KB rotation with 2 old files, a `kind` of
`door` or `command`). The audit log holds only an origin and a four character
HMAC tag of the caller, never an email or an address. The status `recent` list
is the last 10 `door` entries.

Origin: the hub's nginx sets `X-Snaphouse-Origin: home` for LAN requests;
anything else (missing or forged) is treated as the tunnel. A tunnel request
also needs the `Cf-Access-Authenticated-User-Email` header. Decision on the
Access signature: v1 checks that header and relies on the install-day Access
test; it does not verify the `Cf-Access-Jwt-Assertion` signature (that needs the
team's public keys and an outbound fetch). This is a recorded, accepted gap.

Link security: the board link must be encrypted. `[garage] noise_psk` is the
ESPHome API key; without it the config is refused unless `allow_plaintext =
true`, which logs a warning at every start.

Endpoints (POST, loopback, `X-Snaphouse: 1`): `/api/homebody/garage/intent`
(`{"action"}` returns `{nonce, expires_in, confirm_required}`) and
`/api/homebody/garage/command` (`{"action", "nonce"}` returns the full status
document). Settings through `/api/homebody/settings`: `garage_left_open_minutes`,
`garage_away_open_grace_minutes`, `garage_alerts` and `garage_remote` (default
off, and it can only be turned on from the house).

No-retry and no-auto-close rules: nothing moves a door without a person. A
command asks for an end state (never a toggle), is never retried, and there is
no automatic close. A command needs a fresh single-use nonce (30 s), the
tunnel is limited to 1 command per 10 s and 6 per hour, intents are capped at 10 per
minute per caller, and a remote command sends an ntfy note first. ntfy buttons are `view` links only. The state is
`unknown` whenever the link is down or no state has arrived since connect;
`closed` is never shown without a live link.

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
id, display names, the garage board's host and ESPHome key, the ntfy address,
topic and token, and the house page address) lives in the untracked `/etc/homebody/local.toml`, seeded from
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
