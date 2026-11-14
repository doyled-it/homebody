# homebody: design

Date: 2026-11-11. Repos: `homebody` (new) and `snaphouse` (hub deploy and the
room picker's Climate screen).

## Why

The owner wants the air conditioning on comfort when either person is home and on
eco when both are away, the way Google Home's presence automation works, without
sending location to Google and without location services on the phones. The
house already sees both phones on its own network; the thermostat is a Google
Nest Thermostat (2020), which supports Matter for local control.

## Decided by the owner

- Comfort 72°F when anyone is home; away 82°F once both have been gone for 20
  minutes. Cooling only for now (heating later).
- Comfort starts when the first phone is seen at home; no pre-cooling schedule.
- A manual change on the thermostat (on the device or in Google Home) is respected
  until the next home/away switch, after which homebody takes over again.
- The partner's phone is configured but disabled until they agree.
- A new public repo, `homebody`; nothing identifying in it.

## Presence

Each person has one or more devices, configured in an untracked local file on the
hub (`/etc/homebody/local.toml`): a Wi-Fi hardware address (the phone's fixed
private address for this network) and optionally a Bluetooth address (the owner's
Pixel is already paired with the hub). A person is **seen** when any of their
devices answers:

- Wi-Fi: the device's address appears in the hub's neighbour table with a reachable
  state, confirmed by an active probe (ARP ping of its last known IP every 60 s).
- Bluetooth: an `l2ping` of a paired device, tried only when Wi-Fi has not seen the
  person for 5 minutes (phones drop Wi-Fi while asleep), at most once a minute,
  through the hub's Bluetooth receive adapter (call: the adapter that already
  receives from the phone; a ping is tiny next to audio).

A person is **home** while they have been seen within the last 20 minutes, **away**
after that. The house is **occupied** when any enabled person is home. Nothing
leaves the LAN.

## Thermostat over Matter

A local Matter controller (`python-matter-server`, the open-source controller Home
Assistant uses, run as a service from the repo's uv environment) commissions the
Nest through Google Home's "Linked Matter apps & services" sharing code (a one-time
owner step). homebody talks to it over its local WebSocket API and uses the
Thermostat cluster: system mode Cool and the occupied cooling setpoint.

- Occupied: Cool at 72°F. Unoccupied: Cool at 82°F (Matter does not expose the
  Nest's own Eco mode, so eco is this setpoint).
- A home/away transition always sets its target, whatever the thermostat reads.
  Before any other change (a settings change), homebody reads the current mode and
  setpoint; if they differ from what homebody last set, a person changed them by
  hand: record an override and change nothing until the next occupied/unoccupied
  transition.
  Changed 2026-11-14 (owner decision): a transition always takes over; a hand
  change holds only until the next transition.
- Transitions are acted on once; failures retry with backoff and are logged; the
  thermostat is never sent a command more than once a minute.

Open risk to confirm first: Matter over Wi-Fi relies on IPv6 link-local and mDNS.
The hub was changed to advertise only IPv4 through avahi; the Matter controller
does its own discovery, so this should not matter, but the first task proves it by
commissioning before anything else is built.

## Status and the room picker

homebody publishes `/homebody.json` (served by the hub's nginx from a
service-owned subdirectory): each enabled person's state (home/away, last seen
time, which signal), occupied or not, the thermostat's mode, setpoint, current
temperature, whether an override is active, and when the last change was made.
Names are the display names from the local config. A small API
(`/api/homebody/`, header guard `X-Snaphouse: 1`, LAN only) toggles automation on
and off and edits the two temperatures and the delay.

The room picker gains a **Climate** screen: who is home, the thermostat now, the
automation switch, and the three settings. Same visual system; the
frontend-design skill applies.

## Deployment

Like nightstand: a `homebody_bootstrap` role in snaphouse installs and updates
homebody on the hub (self-updating from main with a verify step and last-good
fallback), creates its service user, the local config from an example, and the
nginx locations. Units: `homebody` (presence and decisions), `homebody-matter`
(the Matter controller).

## Non-goals

Heating, multiple thermostats, geofencing, pre-cooling schedules, anything in the
cloud beyond what the Nest itself does with Google.
