# Garage door: status, alerts and remote control

Status: design. Hardware is not installed yet.

Labels used below: **Owner** marks a decision the owner made. **Proposed** and
**builder's call** mark things the design author chose; the owner has not seen them.

## Intent and why

The owner's words: "Wondering whether I closed it mostly. And I'd like all of the home
automation in one place."

So the order of value is: (1) a status you can trust, (2) alerts when the door is open
when it should not be, (3) remote open and close, and all of it on the existing house
page, not a new app.

The hub already runs homebody (presence, Nest thermostat) and serves the house page. The
garage joins them. This document covers both repos and says which part lands where:

- **homebody** (this repo): the garage module, the ESPHome link, the ntfy notifier, the
  API endpoints, the status JSON, the settings, the audit log, tests, docs.
- **snaphouse** (hub repo): the Garage tab on the house page, nginx changes (the
  LAN-or-tunnel marker), ansible and docs changes, and the cross-repo contract entry.

## Goals

1. A Garage card showing Closed, Open, Opening, Closing or Stopped, and for how long.
2. The state comes from the opener over the wire, not from counting button presses.
3. When the board cannot be reached the card says "Unknown, last seen N min ago".
4. Open and close from the page at home, and from away through the tunnel with a confirm.
5. ntfy alerts: left open N minutes; opened while nobody is home; the last person left
   and the door is still open.
6. A tap-to-close path from the alert, never an automatic close.
7. Every remote open or close also sends a note, so nothing moves silently.
8. A garage fault never stops the thermostat automation.
9. The whole feature is testable before the board arrives.

## Non-goals

- No automatic closing, ever. Nothing moves unless a person taps.
- No new app, no Home Assistant, no second dashboard.
- No camera, no licence plates, no vehicle detection.
- No control of the opener's remote lockout from homebody (it is read, not written).
- No replacement of myQ. It stays connected in parallel.
- No push channel other than ntfy. No email or SMS.
- No streaming updates to the page (it polls, like Climate). Builder's call to revisit.

## Anti-goals (a failure even if it works)

- A door that moves with nobody choosing it (a retry, a replayed request, a cached POST,
  an ntfy button, a page reload that re-sends a command).
- "Closed" shown when the truth is unknown (stale state, board offline, homebody restarted).
- Alert spam: repeats every minute, a flapping board, one open episode paging five times.
- A garage bug that blocks, slows or crashes the climate loop.
- Identifying data in the repo, in logs the repo ships, or in web requests: no names,
  emails, IPs, MACs, serials, SSIDs, ntfy topic or token, or the tunnel hostname.

## Constraints

- Hardware: a LiftMaster opener made January 2024, logic board 050DCTBF MC (Security+ 2.0,
  myQ and Bluetooth built in). The owner will buy a Konnected GDO blaQ (ESPHome firmware,
  Security+ 2.0 wireline on the wall-button terminals, Wi-Fi).
- The hub is a ZimaBlade on Debian. homebody runs there as `homebody.service` and
  self-updates from main.
- The page is served at `http://house.local` at home. Away, it is reached through a
  Cloudflare Tunnel behind Cloudflare Access (email one-time PIN; the owner and their
  partner are the allowed users). Tunnel requests carry `Cf-Connecting-Ip`.
- ntfy is self-hosted by the owner. Topic, server URL and token are local config in
  `/etc/homebody/local.toml`, never in a repo.
- Dependencies are managed with uv. No em dashes anywhere.
- snaphouse's AGENTS.md currently says "No inbound access from the internet. No tunnels".
  The tunnel exists, so that rule is out of date. Reconciling it is part of the snaphouse
  work (see "snaphouse changes").

## Approaches

**Board.** Konnected GDO blaQ (Owner's pick) versus ratgdo. Both run ESPHome and both talk
the same native API to the same hub software, so the homebody code is identical. The blaQ
is a finished, supported board at about $89. The ratgdo is cheaper but a DIY build.
Because the software does not change, the board stays swappable.

**Where the logic lives.** Inside homebody, next to climate (Owner's pick), versus a
separate garage service that polls `homebody.json` for presence. Inside wins because it
reuses presence, the settings API, the status JSON, the state file and self-update, and
the "last person left" alert needs the live occupancy transition, not a polled copy. The
cost is shared process fate, which the isolation design below removes.

**Home Assistant.** Rejected by the owner: it would create two "one places", and the house
page already exists.

## Design

### Hardware link

The board exposes the ESPHome native API on TCP 6053. homebody uses `aioesphomeapi`
(added with `uv add aioesphomeapi`).

How the client works (verify the exact signatures against the version uv resolves, since
the library moves): create `APIClient(address, port, password=None, noise_psk=<key>)`,
connect, call `list_entities_services()` to learn entity keys, then
`subscribe_states(callback)` for pushed state. Commands go out with
`cover_command(key, ...)`. `ReconnectLogic` handles reconnects, with the library's own
keepalive pings detecting a dead link.

Encryption (Owner decision, 2026-11-14): the stock blaQ firmware has a bare `api:` with no
key, so anything on the home Wi-Fi could command the door. On install day the board is
reflashed once with Konnected's own ESPHome config plus a native API encryption key (32
bytes, base64), built with ESPHome on the Mac. The key lives only in local config. homebody
requires the key; `allow_plaintext = true` exists only for tests and the loopback board.
Later firmware updates go through ESPHome, not Konnected's one-click updater.

Entities used, matched by name from `list_entities_services` (names are config, with
defaults from the public konnected-io ESPHome config; confirm on install day):

| Need | Entity | Use |
|---|---|---|
| Door | cover | position and current operation (idle, opening, closing) |
| Obstruction | binary sensor | block a close, tell the person |
| Remote lockout | lock | block remote commands, tell the person (read only) |
| Light | light | shown on the card, no control in v1 |
| Motion | binary sensor | read and ignored in v1 |

The cover may or may not report an intermediate position (builder's call to plan for
both shapes). The plan's first task verifies the real blaQ entity list against the public
konnected-io ESPHome config and pins it in a test fixture; the fake board serves both
shapes. A small adapter class (`GarageLink`, a Protocol) hides the library. The module talks only
to the Protocol, so tests use a fake.

### homebody garage module

New files (proposed layout, builder's call on names):

```
src/homebody/
  garage.py        pure logic: state model, episodes, alert rules, command checks
  garage_link.py   the aioesphomeapi adapter behind the GarageLink Protocol
  notify.py        the ntfy client (stdlib HTTP, run in a thread)
  audit.py         the append-only audit log with rotation
```

`garage.py` is pure and takes an injected clock, like `climate.py`. It holds no I/O.

**Wiring and isolation.** `homebody run` starts the garage as its own asyncio task,
separate from `Daemon.tick`. The tick never awaits the garage and never calls into the
link. The two meet in three places only, all non-blocking:

1. The garage task reads occupancy with `Daemon.occupied()` (a cheap read) and watches for
   transitions itself, by comparing the value each pass.
2. The tick's `publish()` includes `garage.snapshot()`, a plain dict copy built under
   try/except; if it fails the key is `null` and the climate fields still publish. When
   the door changes, the garage task sets an event that asks the daemon to publish early;
   the garage never writes `homebody.json` itself, so there is one writer (controller's
   call).
3. The API handler calls `garage.command(...)`, which has its own timeout.

The garage task runs inside a supervisor: it catches every exception, logs it, and
restarts the garage loop with backoff (5 s doubling to 5 min). It never re-raises. Every
network call (ESPHome connect and commands, ntfy) has a timeout, and ntfy sends run in a
worker thread so a hung server cannot stall the loop. The ESPHome and ntfy code share no
locks with climate. Garage state lives in its own file, `garage.json` next to
`state.json`, written with the same atomic replace, so a garage write failure cannot
touch climate state. If garage config is missing or invalid, the module is off, the
status key is `null`, and homebody starts as it does today. A test asserts that a garage
task that raises on every call leaves climate ticking (see Testing).

### State model and transitions

Door state, as shown and published:

`closed`, `open`, `opening`, `closing`, `stopped`, `unknown`.

Derivation from the cover: operation `opening` or `closing` wins; otherwise position 0 is
`closed`, position 1 is `open`, and anything between at rest is `stopped`. `unknown` is
used whenever the link is down or no state has arrived since connect. The module never
carries a door state across a link drop: the moment the link drops the published state is
`unknown` and `last_seen` is the time of the last pushed state.

Each state has a `since` timestamp, set from the time homebody received the change. After
a homebody restart the saved state is not trusted. On connect the board pushes the current
state; if it equals the saved state, the saved `since` is kept (the door has not been seen
to change), otherwise `since` is the connect time and the card shows "at least" wording
(`since_approximate: true`). Builder's call on the exact wording.

An **open episode** starts when the door leaves `closed` (any of opening, open, stopped,
closing) and ends when it reaches `closed`. Closing and then reopening within the same
state change still ends the episode at `closed`. `unknown` does not end an episode and
does not start one; an episode that was open when the link dropped is still open when it
returns unless the door reports `closed`. Alert de-duplication is keyed on the episode.

### Alert rules

All rules run only when alerts are on (setting `garage_alerts`, default on) and ntfy is
configured. Presence `occupied` of `None` (unknown) never triggers an away rule, matching
climate. Alerts have a title, a body, a priority and a click URL (the Garage tab).

| Kind | Fires when | De-dup | Priority (proposed) |
|---|---|---|---|
| `left_open` | open episode age reaches N minutes (default 15, Owner's placeholder, editable on the page) | once per episode, one reminder at +60 min if still open | default; high if occupied is false |
| `opened_away` | door left `closed` while occupied was false, and nobody became home within the arrival grace (default 5 min, Owner decision) | once per episode | high |
| `left_open_away` | occupied goes true to false while the door is not closed | once per episode | high |

Notes:

- `occupied` goes false only after the presence window (20 minutes today), so the
  "last person left" alert arrives about that long after they leave. That is correct, not
  a bug, and the page copy should not promise "instantly when I drive off".
- If `left_open_away` and `left_open` would both fire in one episode, only the first sends;
  the second is folded into it. The body of `left_open_away` says how long the door has
  been open.
- **Arrival grace (Owner decision).** A household member arriving home may open the door
  before their phone has rejoined Wi-Fi. The owner asked for a grace longer than two
  minutes ("5-10"), so `opened_away` waits `garage_away_open_grace_minutes` (default 5,
  range 0 to 15, editable on the page). If occupied becomes true within the grace, no
  alert. So by the owner's choice this alert arrives about 5 minutes after the opening.
- An episode started by an accepted tunnel command does not fire `opened_away`; the
  remote-command note covers it (controller's call).
- A low-priority "Garage closed" note follows an alert for that episode, once, so the
  owner knows it resolved. It is not sent after a commanded close, since the command note
  already says so (controller's call).
- **Link alerts.** One "Garage board offline" alert after the link has been down 10
  minutes (proposed), once per outage, with a "back online" note. Short drops (under the
  10 minutes) never page.
- **Remote command notes** (Owner decision 5): every open or close accepted from the tunnel
  sends a note ("Garage open requested from away"), then a second line in the same topic
  only if the board refuses or does not confirm. Commands from home do not notify.
  Controller's call: tunnel commands are refused (409, clear message) when ntfy is not
  configured. The note is started first, asynchronously, and the command goes to the board
  once it is sent or after about 5 s, whichever is first. If delivery then fails after
  retries the command still proceeds (the person tapping knows) and the audit log records
  "note failed".
- **Flap guard.** A global cap of 6 notifications per hour (proposed). Past it, one
  "Garage alerts paused for the hour" note goes out, and the rest are dropped and logged.
  The cap does not apply to remote command notes (those are the audit trail).
- **Restart.** The fired kinds per episode, and the episode start, are saved in
  `garage.json`. A restart must not re-send an alert already sent for the same episode.
  If the saved state says an episode was open and the board still reports non-closed after
  the restart, the saved episode start and fired kinds are kept; otherwise a new episode
  starts at connect time with `since_approximate` true (controller's call).
- ntfy action buttons are links to the page only. They never perform the close (the ntfy
  app has no Cloudflare Access session). The notification click target is the Garage tab
  URL from config. A `view` action and the default click both open it.
- Delivery failure: retry up to 3 times over about a minute, then log and drop. A failed
  ntfy must not block the garage loop, and alert state is marked sent only after success
  so a transient failure retries on the next pass without duplicating.

### API endpoints

All under `/api/homebody/garage/`, served by the existing loopback API (`api.py`), POST
only, JSON bodies, 64 KB cap (nginx already sets it). The existing `X-Snaphouse: 1` guard
header applies. Each response is the full status document, like the people endpoints.

| Method and path | Body | Purpose |
|---|---|---|
| `POST /api/homebody/garage/intent` | `{"action": "open" or "close"}` | Start a command. Returns `{nonce, expires_in, confirm_required}` (30 s) |
| `POST /api/homebody/garage/command` | `{"action", "nonce"}` | Run it, once, if the nonce is valid |
| `POST /api/homebody/settings` | `garage_left_open_minutes`, `garage_away_open_grace_minutes`, `garage_alerts`, `garage_remote` | Existing endpoint, new keys |

Errors (proposed): 403 (missing guard header, remote control off, tunnel request without
the Access marker, or remote enabling of remote control), 409 (obstruction reported,
unknown state, or a door already moving the other way), 423 (opener's remote lockout is
on), 429 (rate limit), 503 (board unreachable), 504 (the board did not confirm movement).
Each carries a plain sentence for the page.

Command semantics:

- A command is a request for an end state, not a toggle. `open` when already `open` is a
  200 no-op. This is what makes a double tap or a replay harmless.
- Refused when the door state is `unknown`. Never guess.
- `close` is refused while the board reports obstruction. `open` with obstruction active
  is allowed (opening is the safe direction); builder's call to refuse it too.
- Refused while remote lockout is on. homebody does not clear it. The card says why.
- After sending, homebody waits for the board to report `opening` or `closing`: up to 30 s
  for close (Security+ 2.0 sounds a pre-close warning first) and 10 s for open, both config
  keys (controller's call). No confirmation returns 504. A movement that shows up after
  the timeout is audited as "late confirm" and published; the page says "No movement seen
  yet. Check the card before trying again." on a timeout. **There is no automatic retry**, because a retry
  could move a door a person already stopped.
- The nonce is single use, tied to the action, held in memory, and dies on restart. The
  command endpoint takes no action without it.

### Status JSON

`homebody.json` gets one new key. Everything else is unchanged and `version` stays 1
(adding a key is compatible; builder's call to bump). The key is `null` when the garage
module is off, and the page hides the tab then.

```json
"garage": {
  "link": {"connected": true, "last_seen": "2026-11-14T18:02:11Z"},
  "door": {
    "state": "closed",
    "since": "2026-11-14T17:40:03Z",
    "since_approximate": false,
    "position": 0
  },
  "obstruction": false,
  "lockout": false,
  "light": false,
  "settings": {"left_open_minutes": 15, "away_open_grace_minutes": 5, "alerts": true, "remote": true},
  "recent": [
    {"at": "2026-11-14T17:40:03Z", "event": "closed", "origin": "home", "who": null}
  ]
}
```

Rules: every key always present, null when unknown (same rule as today); timestamps are
UTC whole seconds; `state` is `unknown` when `link.connected` is false; `position` is 0 to
100 or null; `recent` holds the last 10 audit entries, `who` is null for home commands (the card shows "from home") and, for tunnel commands, a label from local config
or a short tag, never an email or address. The "N min ago" and "for how long" text is
computed by the page from `updated` and the timestamps.

### Settings

Stored in `state.json` under `settings`, exactly like the climate settings (local config
supplies defaults; the API overrides). New keys:

| Key | Default | Range | Notes |
|---|---|---|---|
| `garage_left_open_minutes` | 15 | 0 to 240 | 0 turns the rule off |
| `garage_away_open_grace_minutes` | 5 | 0 to 15 | arrival grace for `opened_away` (Owner decision) |
| `garage_alerts` | true | bool | master switch for ntfy alerts, not for remote notes |
| `garage_remote` | false | bool | allow commands from the tunnel; turning it ON is refused from the tunnel |

`garage_remote` defaults off. The owner already wants remote control; the default is a safe
install state, not a product limit, and it is switched on at home during install-day
bring-up, after the Access check passes. Turning it off is allowed from anywhere, so it works as a kill switch.

### Page card behavior (lands in snaphouse)

A Garage tab sits beside Climate, hash `#garage`, hidden when `garage` is null, with a
pure `garage-model.js` (like `climate-model.js`) and its tests.

```
 Garage
 +------------------------------------------+
 |  CLOSED                                  |
 |  for 2 h 14 min                          |
 |                                          |
 |  [ Open ]                                |
 |  Opener link: connected                  |
 +------------------------------------------+
 Alert me if left open for [ 15 ] min   (saved)
 Alert if opened while away, after [ 5 ] min
 Allow open and close from away   [ on ]
 Recent
   17:40  Closed   (from home)
   07:52  Opened   (from away)
```

Unknown:

```
 |  UNKNOWN                                 |
 |  Last seen 12 min ago (was closed)       |
 |  [ Open ]  [ Close ]  disabled           |
```

- The big word is the door state; underneath, how long. While `opening` or `closing` the
  buttons are disabled. `stopped` shows both buttons.
- Buttons show only the sensible one (Open when closed, Close when open or stopped).
- At home, a tap sends intent then command with no extra step. Away (the intent response
  says `confirm_required`), the tap opens an inline "Open the garage door? Open / Cancel"
  with the plain words, and the command is sent only on the second tap. The page uses
  the intent response, not its own guess about where it is.
- When the page learns the request came through the tunnel it shows "You are away. This
  moves the door." above the confirm.
- Obstruction and lockout show as a line with a reason, and the close button is disabled
  ("The opener reports something in the way" or "Remote lockout is on at the opener").
- After a command the card shows "Sent. Waiting for the door." and polls every 2 s for
  up to 15 s, then falls back to normal. A failed command shows the error sentence and
  never retries by itself.
- The page polls `homebody.json` every 5 s while the Garage tab shows, and every minute
  from other tabs (Climate polls every 15 s today).
- UI copy never says ESPHome, board, ntfy or API. The link line reads
  "Opener link: connected" (builder's call on exact words).
- The ntfy click target is `<page url>#garage`.

### snaphouse changes

1. **Garage tab**: `roles/room_picker/files/site/` gets `garage-model.js`, the card in
   `index.html`/`rooms.js`, `nav-model.js` gets the tab (hidden when `garage` is null),
   styles, tests. The nav bar becomes up to six tabs; check the 360 px layout.
2. **nginx** (ships and deploys before the homebody garage PR merges; controller's call.
   The garage uses only the new `X-Snaphouse-Origin` header, and a missing header means
   tunnel): in `roles/homebody_bootstrap/templates/homebody.conf.j2`, mark each request
   as home or tunnel and overwrite any client-sent marker:

   ```
   geo $remote_addr $snaphouse_lan { default 0; 10.0.0.0/8 1; 172.16.0.0/12 1; 192.168.0.0/16 1; }
   map "$http_cf_connecting_ip:$snaphouse_lan" $snaphouse_origin { default tunnel; ":1" home; }
   ... proxy_set_header X-Snaphouse-Origin $snaphouse_origin;
   ```

   (The `geo` and `map` go in the http-level include; the exact placement is the
   builder's call.) A request is `home` only if it has no `Cf-Connecting-Ip` and comes from
   a private LAN address. Loopback is not LAN, so a tunnel daemon on the hub itself is
   `tunnel`. Anything unexpected is `tunnel`. The block also passes through
   `Cf-Access-Authenticated-User-Email` and `Cf-Connecting-Ip` (nginx forwards request
   headers by default; keep it that way and add a test).
3. **Outbound rules**: AGENTS.md lists the hub's allowed outbound targets. Add the board
   (LAN, TCP 6053) and the ntfy server (named only in local config) to that list. The
   hostname is not written in the repo.
4. **AGENTS.md and docs**: replace the "no tunnels" rule with the real one (a tunnel
   exists, behind Access, and what it may reach). Add the Garage tab to the page docs and
   the new `garage` key and endpoints to `docs/cross-repo-features.md` (a pinned
   contract), and note the `X-Snaphouse-Origin` header.
5. **Unit and install**: `homebody.service` needs no new capability. It already reaches
   the LAN. If the unit later gains `RestrictAddressFamilies`, it must allow `AF_INET` and
   `AF_INET6` for the board and ntfy. `ReadWritePaths` already covers the state dir.

### homebody config keys

In `/etc/homebody/local.toml` (and the generic placeholders in `local.example.toml`):

```toml
# [garage]
# host = "<board-address-or-mdns-name>"
# port = 6053
# noise_psk = "<base64-32-byte-key>"          # the ESPHome API encryption key
# page_url = "<url-of-the-house-page>"        # the tunnel hostname, for ntfy clicks
# entity_names = { cover = "Garage Door", obstruction = "Obstruction", lockout = "Lock remotes" }
#
# [garage.ntfy]
# url = "<ntfy-server-url>"
# topic = "<ntfy-topic>"
# token = "<ntfy-access-token>"               # optional
#
# [garage.actors]                              # optional labels for audit tags
# "<tag>" = "<display label>"
```

Absent `[garage]` means the module is off. `page_url` is the one place the tunnel hostname
appears, and only in local config. Config errors disable the garage (logged once), not
homebody.

## Security

Remote opening of a door is the one sensitive action here. The design uses several
independent layers, so no single failure is enough.

**Who is allowed.** Authentication is Cloudflare Access at the edge (email one-time PIN,
two allowed users, Owner's setup). homebody does no logins of its own. Access protects
the whole tunnel hostname, so install day checks there is no Access "bypass" rule and that
an anonymous request to `/api/homebody/garage/` gets the Access login, not homebody.

**LAN or tunnel.** nginx decides and writes `X-Snaphouse-Origin`; homebody trusts only that
header, and its API listens on loopback, so nothing else reaches it. A request counts as
`home` only with no `Cf-Connecting-Ip` and a private LAN source; everything else is
`tunnel`. A missing header is treated as `tunnel`. A LAN user can fake neither, since
the header is overwritten by nginx, and an internet user cannot remove Cloudflare's
`Cf-Connecting-Ip`.

**Defense in depth on the tunnel.** A tunnel command also needs the Access identity header
(`Cf-Access-Authenticated-User-Email`) to be present. Honest limit: a header is not proof,
since a tunnel with no Access policy would let a client forge it. The real proof is the
signed `Cf-Access-Jwt-Assertion`. Verifying that signature needs the Access team's public
keys, which means an outbound fetch (not on the hub's allowed list) or pinning keys in
local config (they rotate). Proposed: v1 ships the header check plus the install-day Access
test; the builder decides whether to add JWT verification with keys cached in local
config. Not doing it is a recorded, accepted gap.

**Confirm and CSRF/replay.** The confirm step is enforced twice. The page asks the person
(UI), and the server will not run a command without a fresh single-use nonce from the
intent endpoint (30 s, bound to the action). A forged cross-site POST fails on the custom
`X-Snaphouse: 1` header (a browser cannot send it cross-origin without a CORS preflight,
which homebody never approves) and on the missing nonce. A replayed command fails because
the nonce is spent. Commands are end-state requests, so even a repeat is harmless.

**Rate limiting.** Proposed: at most 1 command per 10 s overall, and 6 per hour from the
tunnel; beyond that, 429 and an audit line. There is no lockout state to brute force
since there is no password, but the limits cap damage from a stolen session.

**Audit log.** `garage-audit.log` in the state dir, JSON lines, append-only, rotated at
256 KB with 2 old files (proposed). Each line: UTC time, action or event, origin
(`home` or `tunnel`), a short actor tag, door state before, result (sent, confirmed, or
the refusal reason). The tag is the first 4 hex characters of an HMAC of the Access email
using a random key generated once and kept in the state dir; the log never stores the email
or any IP. Optional labels map tags to display labels in local config, so the page can say
who without the repo knowing. Events from the board itself (door moved by a wall button or
myQ) are logged with origin `opener`.

**Notifications.** The ntfy topic is the only secret a leak would expose, and it carries
door state and times. Topic and token live in local config only, the topic should be
long and unguessable, and the token (if the server needs one) is write-only for that
topic. Notification bodies carry no names, addresses or the tunnel hostname other than
the click URL. ntfy buttons are links only.

**Obstruction and lockout.** Obstruction blocks a close and says so. Remote lockout blocks
every remote command and says so; homebody never clears it (that is the opener owner's
physical safety control). A tunnel command is refused when `garage_remote` is off.

**Moving a door you cannot see.** A remote close with nobody watching is a real safety
risk (a person, a pet, a child). Installer duty on install day: confirm the opener sounds
and flashes before a close commanded through the board, as it does for myQ. If it does
not, do not enable `garage_remote` until that is resolved. This is unverified today.

## Failure modes

| Failure | Behavior |
|---|---|
| Board offline or Wi-Fi down | State becomes `unknown`, last seen shown, commands refused (503), offline alert after 10 min |
| ESPHome key wrong or changed | Connect fails; same as offline; the log names "authentication failed" without printing the key |
| Link flaps | Debounced by the 10 min offline alert and the notification cap; the card goes `unknown` each drop |
| homebody restarts | Door state unknown until the board pushes it; fired alert kinds restored from `garage.json` |
| ntfy server down | Retries, then drops with a log line; alert state not marked sent until success; climate unaffected |
| Garage task raises | Supervisor logs and restarts with backoff; climate ticks continue; `garage` key null if the snapshot fails |
| Cloudflare or tunnel down | Away control gone, home control works; alerts unaffected if ntfy is reachable |
| Hub down | No alerts and no status. A dead-man check is out of scope for v1 (a known gap) |
| Presence unknown (after restart) | Away rules do not fire; `left_open` still does |
| Power loss and return | Opener reports state on reconnect; `since` becomes approximate |
| Opener unresponsive after a command | 504 after the confirm window; no retry; audit line "no confirmation" |
| Obstruction mid-close | The opener itself reverses; the card shows the new state and the obstruction line |
| Two people tap at once | Second sees a 200 no-op or a 409 "already moving" |
| State file damaged | Starts fresh; alert de-dup resets, so at most one repeat alert per open episode |
| Disk full | Audit and state writes log and fail soft; commands still run, with the failure logged |

## Testing

All tests run on any dev machine with `uv run pytest`, no hardware, no network.

- **Fake ESPHome device (proposed).** A `FakeGarageLink` implementing the Protocol, driven
  by the test: push cover states, flip obstruction and lockout, drop and restore the
  link, delay or swallow a command. It records commands sent. Unit tests use this.
- **Loopback ESPHome server (builder's call, strongly suggested).** A tiny server that
  speaks enough of the native API (plaintext and noise) for the real `garage_link.py` to
  connect, list entities, subscribe and send a cover command. `aioesphomeapi` may offer
  test helpers; otherwise the ESPHome project can run a host-platform fake device. This
  covers the adapter, the only code that cannot be covered with the in-memory fake, the
  same split as the Matter fake and the loopback WebSocket test.
- **Fake ntfy.** A loopback HTTP server that records requests, and can fail or hang.
- **Grace tests**: `opened_away` fires after the grace only if nobody became home, never
  when occupied flips true inside it; grace 0 fires at once; a tunnel-commanded episode
  does not fire it.
- **Pure logic tests** on `garage.py` with an injected clock: state derivation, `since`,
  episodes, every alert rule, de-dup, reminder timing, restart restore, flap cap,
  command checks (obstruction, lockout, unknown, no-op, nonce reuse and expiry).
- **API tests**: guard header, origin handling (`home`, `tunnel`, missing, forged),
  the Access marker, the nonce flow, rate limits, each error code, the settings keys,
  `garage_remote` enable refused from the tunnel.
- **Isolation test**: a garage module that raises on every call and one that hangs, with
  the climate loop proven to tick and publish on schedule.
- **Status tests**: the `garage` key shape, `null` when off, `unknown` when the link is
  down, never "closed" without a live link.
- **Audit tests**: the log has no email or address, rotates, and tags are stable.
- **Config tests**: absent, partial and bad `[garage]` sections; no identifying data in
  `local.example.toml`.
- **snaphouse**: `garage-model.js` unit tests (card text for each state, "last seen N min",
  confirm flow, hidden when null) and an nginx config test that the origin header is
  overwritten and the Access headers pass through.
- **Cross-repo**: the status contract test in snaphouse reads a sample document from
  homebody's tests so the shape cannot drift.

## Install-day bring-up

1. Install the blaQ per Konnected's instructions, on the opener's wall-button terminals.
   Confirm the wall button, myQ and the remotes still work.
2. Reflash the board with Konnected's ESPHome config plus an `api: encryption: key:`
   (Owner decision), and note the board's name on the LAN. Give it a fixed address reservation on the router.
3. Put `[garage]` in `/etc/homebody/local.toml` (board, key, page URL, ntfy). `garage_remote`
   stays off. Restart `homebody`; the log should show the garage connecting.
4. Open the page at home. The Garage tab should appear with the right state and "for how
   long". Open and close by wall button and by myQ; the card follows both.
5. Pull the board's power. Within a minute the card says "Unknown, last seen"; after 10
   minutes an offline note arrives. Restore power; it recovers.
6. Test each alert: set left-open to 1 minute, leave it open; open it with every phone
   marked away (disable them via config for the test); leave with the door open.
7. Check the ntfy click opens the Garage tab at home and through the tunnel, and that no
   ntfy button moves the door.
8. Safety: from the page, close the door while watching it. Confirm the beeps and flashing
   lights happen before it moves, and test the obstruction sensor and the remote
   lockout behavior (both should block the command with a clear message).
9. Tunnel classification, before `garage_remote` is switched on: from a phone on mobile
   data (away from the house), confirm an anonymous request to the API gets the Access
   login, then log in and open the Garage card. A real request through the tunnel must be
   classified as tunnel: the card shows the away confirm note, and the `garage-audit.log`
   line for that request records origin `tunnel`. If the card treats the request as home,
   stop: the nginx origin header is not being set from the Cloudflare headers, so do not
   enable remote control until that is fixed.
10. Only then turn `garage_remote` on at home, and run an open and close from the phone
    with the confirm. Each should produce a note and an audit line, with origin `tunnel`.
11. Read `garage-audit.log` and `homebody.json`; confirm there are no emails or addresses.
    `aioesphomeapi` can log warnings or errors that carry the board's address. These go to
    the hub's local journal only (ruling R-T7); they never reach the repo or the status
    file. Do not paste journal output into a ticket or a chat without checking it first.

## What is left to the builder

The builder may decide, and should record the decision in the PR:

- File and class names, and the split between `garage.py`, `garage_link.py`, `notify.py`
  and `audit.py`.
- The exact `aioesphomeapi` calls and version, and entity-name defaults (verify against
  the blaQ's real entity list).
- Whether the link may run unencrypted if the firmware forces it.
- The notification cap, retry counts, reminder timing, offline threshold and rate-limit
  numbers (the values here are proposals).
- Whether `open` with an obstruction is refused; whether to bump the status `version`.
- Whether to add Access JWT verification, and where to keep the keys.
- The exact alert wording.
- How the nginx `geo`/`map` is laid out and where it is included.
- Page copy, the tab icon and the exact layout, within the house page's design rules.
- Audit rotation sizes and the actor-tag scheme.
- The loopback ESPHome server versus a library test helper.

The builder may not decide, without asking the owner: automatic closing of any kind;
moving a door from an ntfy button; putting any topic, token, hostname, address or label in
the repo; letting a remote command skip the confirm; or letting garage code share a lock
or a failure path with climate.
