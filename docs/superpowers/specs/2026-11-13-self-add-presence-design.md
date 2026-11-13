# Count this phone as home (self-add presence)

Short design. Spans homebody (the API and presence) and snaphouse (the hub's
nginx route and the house page's Climate tab).

## Why

The owner's partner should be able to count their own phone toward "someone is
home" without anyone reading a Wi-Fi address off a settings screen and editing
`/etc/homebody/local.toml` by hand. Today only people in that file count, so the
Nest goes to the away temperature whenever the owner leaves, even if the partner
is still home.

## Goals

- On the house page's Climate tab, a phone on the home Wi-Fi can add itself
  with a name in one step. From then on homebody counts it like any person.
- Anyone added this way can be removed from the same tab.
- Addresses never leave the hub: not in the status document, not in a repo.

## Non-goals

- Bluetooth presence for added people (a browser cannot read a Bluetooth
  address). They are Wi-Fi only.
- Adding someone from outside the house. Cloudflare Access sign-in stays a
  dashboard step for the owner.
- Editing or removing people from `local.toml` through the page.

## Anti-goals

- Registering the wrong device. Through the tunnel every request reaches the hub
  from the Mac mini, so a naive lookup would add the Mac mini. That must be
  refused by the server, not merely hidden in the page.

## Approaches

1. **Learn the address from the visit (picked).** The hub sees the request's
   client IP and reads the matching Wi-Fi address from its neighbour table, the
   same table presence already watches. Zero typing beyond a name.
2. Paste the Wi-Fi address into a form. Works through the tunnel too, but asks
   the partner to find a buried setting and invites typos. Rejected: the point is
   to skip that step.
3. Sign-in identity from Cloudflare Access. Tells the hub who someone is, not
   which device is theirs. Rejected: does not solve presence.

The owner picked 1 ("Yes, build it").

## Design

### homebody

- **People store.** Added people live in `state.json` under a new `people` key:
  a list of `{"id", "name", "wifi_mac"}`. `local.toml` stays read-only. Ids are
  `added-` plus 8 random hex characters (opaque, as `Person.id` requires).
  (call)
- **Presence.** `Presence.add_person(person)` starts tracking an enabled person,
  observed afresh (unknown until seen). `Presence.remove_person(id) -> bool`
  stops tracking. Only added people can be removed.
- **Status document.** Each entry in `people` gains `"id"` and
  `"removable": bool`. Never an address.
- **Which device is asking.** nginx passes the client address as `X-Real-IP`.
  homebody looks it up with `ip -4 neigh show <ip>` and takes the `lladdr`.
  A request carrying a `Cf-Connecting-Ip` header came through the Cloudflare
  tunnel and is refused for adding (the edge always sets it; a client cannot
  remove it). No `X-Real-IP`, a non-IPv4 address or no neighbour entry also
  refuse. (call)
- **API** (all under `/api/homebody/people/`, POSTs need `X-Snaphouse: 1`):
  - `GET this-device` answers `{"can_add": bool, "counted_as": str|null,
    "reason": "remote"|"unknown"|null}`. `counted_as` is the name of whoever
    (config or added) already has this device's Wi-Fi address.
  - `POST add` with `{"name": str}` answers 200 and the status document; 403
    `remote` or `unknown` as above; 409 when this device is already counted
    (message names who); 400 for a bad name.
  - `POST remove` with `{"id": str}` answers 200 and the status document; 400
    for an unknown id or a `local.toml` person.
- **Names.** Trimmed, 1 to 30 characters, no control characters, unique among
  all people ignoring case. At most 8 added people. (call)

### snaphouse

- **nginx.** The `/api/homebody/` location sets
  `proxy_set_header X-Real-IP $remote_addr;` (overriding anything a client
  sends) and leaves `Cf-Connecting-Ip` passed through.
- **Climate tab.** Under the people lines:
  - On home Wi-Fi with this phone not counted: a "Count this phone as home"
    button that opens a name field and an Add button.
  - Already counted: "This phone counts as <name>."
  - Through the tunnel (`reason: "remote"`): a quiet line, "To count a phone,
    open this page on home Wi-Fi."
  - `reason: "unknown"`: nothing.
  - Each removable person gets a Remove button, with a confirm step.
  Rules (what to show from the two documents, request bodies, error copy) live
  in `climate-model.js` as pure functions with tests, like the existing
  settings rules. (call)

## Left to the builder

Exact markup and CSS within the page's existing Climate styles; module and test
names within each repo's conventions; how `this-device` is refreshed (on Climate
tab open and after add or remove is enough).
