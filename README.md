# homebody

Presence-based air-conditioning for a Google Nest thermostat, decided from the
house's own network and applied over Matter. The air conditioning runs on
comfort when either person is home and on eco (a warmer setpoint) once everyone
has been gone for a while, the way Google Home's presence automation works,
without sending any location to the cloud and without location services on the
phones. Nothing leaves the LAN.

This repository is placeholder-only: every real address, name, and pairing code
lives in an untracked local config on the hub, never here.

## Status

Early. The Matter controller client, presence, the decision loop, the status
file, and the local settings API are in. The room-picker Climate screen comes in
a later task.

## How it fits together

homebody runs on the snaphouse hub as two services:

- `homebody-matter`: the open-source Matter controller
  ([`python-matter-server`](https://github.com/home-assistant/python-matter-server)),
  bound to loopback, that holds the Matter fabric and talks to the Nest.
- `homebody`: the presence-and-decision daemon (`homebody run`) that watches the
  phones and sets the thermostat through the controller.

snaphouse installs and self-updates homebody on the hub (a `homebody_bootstrap`
role), creates its service user and local config, and serves its status file and
API through nginx. The thermostat is a Google Nest Thermostat (2020), shared to
the controller once through Google Home's "Linked Matter apps & services".

## Development

The project uses [uv](https://docs.astral.sh/uv/). Python 3.12+.

- `uv sync` installs dependencies into `.venv`.
- `uv run pytest` runs the test suite.
- `uv run ruff check` lints. Add `--fix` to apply safe fixes.
- `uv run homebody` runs the CLI entry point.

The Matter controller (`python-matter-server`) is a Linux-only runtime
dependency: its CHIP SDK wheels do not cover every platform, so on a dev machine
it is skipped and the tests run against a fake controller. See `AGENTS.md`.

## CLI

```
homebody matter status                 # live mode, temperature, and setpoint
homebody matter commission <code>      # commission the Nest with a sharing code
homebody matter set-setpoint <°F>      # write the occupied cooling setpoint
homebody matter set-mode <off|cool>    # write the system mode
```

`homebody matter status` prints the thermostat's current system mode, local
temperature, and occupied cooling setpoint, read live from the controller.

```
homebody run [--config PATH]           # the service loop (what the unit runs)
```

`homebody run` reads `/etc/homebody/local.toml` (or `--config`, or
`HOMEBODY_CONFIG`) and loops until SIGTERM. Anyone home means Cool at the
comfort setpoint; everyone gone for the away delay means Cool at the away
setpoint. It acts once per home/away change, at most one command a minute, and
leaves the thermostat alone after a hand change until the next home/away
change. If the controller or thermostat is unreachable it keeps tracking
presence and retries with backoff, then acts on the latest state only.

It keeps a small state file, `state.json`, in the state directory
(`/var/lib/homebody/state`, which deploy/install.sh creates for the service user, unless `[daemon] state_dir` says otherwise): what it last
set, the override flag, the last change and transition times, and any runtime
enable flags, and any settings changed through the API (see below). The file is
replaced atomically, so a restart resumes where it left off and a damaged file
just starts fresh.

### Status file and settings API

After every tick, and right after a settings change, homebody writes
`homebody.json` (atomically, mode 0644) into `[status] dir` (default
`/var/lib/snaphouse/public/homebody`); the hub's nginx serves it. It holds the
enabled people (display names, home or away, last seen, which signal), whether
the house is occupied, the thermostat's mode, setpoint and temperature, the
override flag, the last change homebody made, the automation switch, and the
three settings. Every key is always present and is null when unknown.

`homebody run` also listens on `127.0.0.1:8783` (`[api] port`). The hub's nginx
proxies `/api/homebody/` to it. `POST /api/homebody/settings` takes a JSON object
with any of `enabled` (true or false), `comfort_f` (60 to 85), `away_f` (65 to
90) and `away_minutes` (5 to 120), with comfort kept below away, and needs the
header `X-Snaphouse: 1`. It answers with the full status document, or a 400, 403,
404 or 405 with `{"error": "..."}`. With `enabled` false, presence is still
tracked and published but no thermostat commands are sent.

Three more endpoints let a phone on the home Wi-Fi count itself as home.
`GET /api/homebody/people/this-device` answers `{"can_add", "counted_as",
"reason"}` (`reason` is `remote`, `unknown` or null). `POST
/api/homebody/people/add` takes `{"name"}` and `POST /api/homebody/people/remove`
takes `{"id"}`; both need `X-Snaphouse: 1` and answer with the status document or
`{"error": "..."}`. The device is found from the `X-Real-IP` header nginx sets,
looked up in the kernel neighbour table (`ip -4 neigh show`), so adding works only
from the home Wi-Fi: a request carrying `CF-Connecting-IP` came through the tunnel
and gets a 403 with `reason` `remote`. Removing is allowed through the tunnel.
Added people are tracked by Wi-Fi only.

Settings changed this way are saved in the daemon state and win over the
`[climate]` values in `local.toml`, which homebody never writes.

## Deployment

Like nightstand: `deploy/install.sh` (run as root) ensures the service user, the
local config from `local.example.toml`, the working directories, and the uv
venv, then runs `deploy/verify.sh`. `deploy/update.sh` self-updates from `main`
on a timer with a verify step and a last-good fallback, so a bad commit never
sticks. The systemd units live in the snaphouse `homebody_bootstrap` role.

## This is a multi-repo system

homebody is one repo among several (the hub is `snaphouse`). A feature usually
spans repos, and the seams (the install interface, the status and API contracts,
and each repo's docs) are where a change ships half-wired. The repo map and the
definition of done are in the hub repo: `snaphouse/docs/cross-repo-features.md`.
