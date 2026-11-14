# Count This Phone As Home Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A phone on the home Wi-Fi adds itself to homebody's presence from the house page's Climate tab, and added people can be removed there.

**Architecture:** homebody keeps added people in its own `state.json`, tracks them like configured people, and exposes three endpoints under `/api/homebody/people/`. It identifies the asking device from the `X-Real-IP` nginx sets plus the kernel neighbour table, and refuses tunnel requests (`Cf-Connecting-Ip` present). snaphouse sets the header in nginx and draws the controls on the Climate tab.

**Tech Stack:** homebody: Python 3, stdlib asyncio HTTP, pytest via uv. snaphouse: Ansible templates, vanilla JS modules with `node --test`, pytest for role tests.

**Spec:** `homebody/docs/superpowers/specs/2026-11-13-self-add-presence-design.md` (in the homebody repo, branch `self-add-presence`).

## Global Constraints

- Wi-Fi addresses never appear in the status document, logs at INFO or above, a repo, or a test fixture that looks real (use `02:00:00:00:00:0X` style locally administered addresses in tests).
- `local.toml` is never written by homebody. Added people live only in `state.json` under `people`.
- Added person ids: `added-` plus 8 random lowercase hex characters.
- Names: trimmed, 1 to 30 characters, no control characters, unique among all people ignoring case. At most 8 added people.
- Tunnel requests (header `Cf-Connecting-Ip` present) can never add a person.
- POST endpoints require `X-Snaphouse: 1`, like the settings endpoint.
- No em dashes anywhere (code, comments, copy, commits).
- homebody CI: `uv run ruff check`, `uv run pytest`, `shellcheck deploy/*.sh`. snaphouse CI includes ansible-lint, pytest and `node --test`; check every job's result line, not a tail.

## Review Focus

1. The owner taps the button on a phone already in `local.toml`: answer 409 naming them, and `this-device` reports `counted_as` (Task 1 `test_counted_as_finds_config_people_ignoring_case`, Task 2 `test_add_from_counted_device_is_409`).
2. A tunnel request from the Mac mini's LAN address: refused as `remote` even though its address resolves (Task 2 `test_tunnel_request_cannot_add`).
3. The hub restarts after an add or a remove: the added person is still tracked, a removed one stays gone (Task 1 `test_added_people_survive_restart`).
4. The status document never carries an address (Task 1 `test_status_document_has_no_addresses`).
5. A damaged `people` entry in `state.json` (missing name, bad address, wrong type) is skipped, and the daemon still starts (Task 1 `test_bad_saved_people_are_skipped`).

---

### Task 1: homebody people store, presence and status

**Files:**
- Create: `src/homebody/people.py`
- Modify: `src/homebody/presence.py` (`PersonStatus`, `_Tracked`, `PresenceTracker`)
- Modify: `src/homebody/daemon.py` (load, `_save`, new methods)
- Modify: `src/homebody/status.py` (people entries)
- Test: `tests/test_people.py` (new), `tests/test_presence.py`, `tests/test_daemon.py`, `tests/test_status.py`

**Interfaces:**
- Produces, `homebody.people`:
  - `class PeopleError(ValueError)` with attribute `status: int` (400 or 409); `str(error)` is the message shown to people.
  - `MAX_ADDED = 8`, `MAX_NAME = 30`
  - `clean_name(raw: object, taken: Iterable[str]) -> str` raises `PeopleError(400)`.
  - `new_person_id() -> str` (`added-` + `secrets.token_hex(4)`).
  - `parse_saved(raw: object) -> list[Person]` (skips bad entries; each Person has `enabled=True`, `bluetooth_mac=None`).
  - `to_saved(people: Sequence[Person]) -> list[dict[str, str]]`.
- Produces, `PresenceTracker`: `add_person(person: Person) -> None` (tracked as removable, observed afresh), `remove_person(person_id: str) -> bool` (False for unknown or configured people), `person_for_mac(mac: str) -> Person | None` (case-insensitive, over all tracked people, enabled or not). `PersonStatus` gains `removable: bool = False`.
- Produces, `Daemon`: `counted_as(wifi_mac: str) -> str | None` (the person's name), `add_person(name: object, wifi_mac: str) -> dict[str, Any]` (status document; `PeopleError(409)` "This phone is already counted as {name}." when the address is taken, `PeopleError(400)` "Up to 8 people can be added here." past the limit), `remove_person(person_id: object) -> dict[str, Any]` (`PeopleError(400)` "That person cannot be removed here." for unknown or configured ids).
- Status document people entries: `{"id", "name", "state", "last_seen", "via", "removable"}`.

- [ ] **Step 1: Write the failing tests**

`tests/test_people.py`:
```python
def test_clean_name_trims_and_accepts(): assert clean_name("  Sam ", ["Alex"]) == "Sam"
def test_clean_name_rejects_blank_long_control_and_duplicates():
    for bad in ["", "   ", "x" * 31, "Sa\nm", "alex", 5, None]:
        with pytest.raises(PeopleError) as e: clean_name(bad, ["Alex"])
        assert e.value.status == 400
def test_new_person_id_shape(): assert re.fullmatch(r"added-[0-9a-f]{8}", new_person_id())
def test_bad_saved_people_are_skipped():
    raw = [{"id": "added-0000000a", "name": "Sam", "wifi_mac": "02:00:00:00:00:0a"},
           {"id": "added-0000000b", "wifi_mac": "02:00:00:00:00:0b"},
           {"id": "added-0000000c", "name": "Al", "wifi_mac": "nope"}, "junk"]
    assert [p.name for p in parse_saved(raw)] == ["Sam"]
    assert parse_saved({"not": "a list"}) == []
```
`tests/test_presence.py`: `test_added_person_is_tracked_and_removable` (add, appears in `statuses()` with `removable=True`, state unknown until a probe sees them), `test_configured_person_cannot_be_removed` (`remove_person(config_id) is False`), `test_counted_as_finds_config_people_ignoring_case` (`person_for_mac("02:00:00:00:00:0A")` returns the configured person whose mac is lowercase).
`tests/test_daemon.py`: `test_add_person_counts_and_saves`, `test_add_person_twice_is_409`, `test_ninth_added_person_is_400`, `test_remove_configured_person_is_400`, `test_added_people_survive_restart` (add two, remove one, build a new Daemon over the same state dir: exactly the remaining one is tracked).
`tests/test_status.py`: `test_status_document_has_no_addresses` (after adding a person, `json.dumps(doc)` contains no match for `[0-9a-f]{2}(:[0-9a-f]{2}){5}`), and people entries carry `id` and `removable`.

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_people.py tests/test_presence.py tests/test_daemon.py tests/test_status.py -q`
Expected: FAIL (import error for `homebody.people`, missing methods).

- [ ] **Step 3: Implement `homebody/people.py`, the tracker methods, the daemon methods and the status fields**

Daemon loads `parse_saved(saved.get("people"))` before applying saved `enabled` flags and calls `presence.add_person` for each; `_save` writes `"people": to_saved(added)`. Address validation reuses `config._mac` (or its regex) so saved and configured addresses normalise the same way. `add_person` and `remove_person` save, then `return self.publish()`; a failed save logs like `apply_settings` does.

- [ ] **Step 4: Run the whole suite and lint**

Run: `uv run pytest -q && uv run ruff check`
Expected: all pass, no lint errors.

- [ ] **Step 5: Commit**

```bash
git add src/homebody tests docs/superpowers
git commit -m "Keep added people in state and track them like configured ones"
```

---

### Task 2: homebody people API and device lookup

**Files:**
- Modify: `src/homebody/presence.py` (new `neighbour_mac`)
- Modify: `src/homebody/api.py` (routes, `handle` signature, `start`)
- Modify: `src/homebody/cli.py` (`_start_api` passes the lookup)
- Modify: `AGENTS.md`, `README.md` (API section: the three endpoints, the tunnel rule, Wi-Fi only)
- Test: `tests/test_presence.py`, `tests/test_api.py`

**Interfaces:**
- Consumes: Task 1's `Daemon.counted_as`, `Daemon.add_person`, `Daemon.remove_person`, `PeopleError.status`.
- Produces:
  - `presence.neighbour_mac(ip: str, runner: Runner = run_command) -> str | None`: `None` unless `ip` is a valid IPv4 address; runs `["ip", "-4", "neigh", "show", ip]` with a 5 s timeout; returns the lowercase `lladdr` of an entry not in `FAILED`/`INCOMPLETE`; `None` on any error.
  - `api.PEOPLE_DEVICE = "/api/homebody/people/this-device"`, `api.PEOPLE_ADD = "/api/homebody/people/add"`, `api.PEOPLE_REMOVE = "/api/homebody/people/remove"`.
  - `api.resolve_device(headers: Mapping[str, str], lookup: Callable[[str], str | None]) -> tuple[str | None, str | None]` returning `(mac, reason)`: `(None, "remote")` if `cf-connecting-ip` is present (checked first), `(None, "unknown")` if `x-real-ip` is missing or the lookup gives `None`, else `(mac, None)`.
  - `api.handle(target, method, path, headers, body, lookup=presence.neighbour_mac)`; the target protocol gains the three Task 1 methods.
  - `GET this-device` -> 200 `{"can_add": bool, "counted_as": str | None, "reason": "remote" | "unknown" | None}`; `can_add` is true only with a resolved, uncounted address. No guard header needed.
  - `POST add` `{"name": str}` -> 200 status document; 403 `{"error": "Count a phone from the home Wi-Fi, not from away.", "reason": "remote"}`; 403 `{"error": "The hub could not tell which device this is.", "reason": "unknown"}`; `PeopleError` -> its status and `{"error": str(e)}`.
  - `POST remove` `{"id": str}` -> 200 status document or `PeopleError` as above. Remove is allowed through the tunnel.
  - Wrong method on any people path -> 405; unknown body keys -> 400.

- [ ] **Step 1: Write the failing tests**

`tests/test_presence.py`: `test_neighbour_mac_reads_lladdr` (runner output `"192.0.2.5 dev eth0 lladdr 02:00:00:00:00:0A STALE"` -> `"02:00:00:00:00:0a"`), `test_neighbour_mac_rejects_failed_and_non_ipv4` (`FAILED` entry -> None; `"fe80::1"` and `"not-an-ip"` -> None without running anything).
`tests/test_api.py` (fake target and fake lookup): `test_tunnel_request_cannot_add` (headers `x-real-ip` resolvable plus `cf-connecting-ip`, POST add -> 403 with `reason == "remote"`, target untouched), `test_this_device_reports_remote_unknown_counted_and_addable` (four cases), `test_add_from_counted_device_is_409`, `test_add_needs_guard_header` (403), `test_remove_through_tunnel_is_allowed`, `test_people_paths_reject_wrong_method` (405), `test_add_rejects_unknown_keys` (400).

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_api.py tests/test_presence.py -q`
Expected: FAIL on the new tests only.

- [ ] **Step 3: Implement `neighbour_mac`, `resolve_device`, the routes and the wiring**

`_serve_one` and `start` take the lookup and pass it to `handle`; `cli._start_api` passes `presence.neighbour_mac`. Log adds and removes at INFO with the id only, never the address or name.

- [ ] **Step 4: Run the whole suite and lint**

Run: `uv run pytest -q && uv run ruff check`
Expected: all pass.

- [ ] **Step 5: Commit, push, open the PR**

```bash
git add src tests AGENTS.md README.md
git commit -m "Let a phone on the home Wi-Fi add itself through the API"
```
Open one homebody PR covering Tasks 1 and 2.

---

### Task 3: snaphouse nginx header and Climate tab controls

Repo: snaphouse, branch `self-add-presence`.

**Files:**
- Modify: `roles/homebody_bootstrap/templates/homebody.conf.j2` (`proxy_set_header X-Real-IP $remote_addr;` in `/api/homebody/`)
- Modify: `roles/room_picker/files/site/climate-model.js`, `rooms.js` (climate section near `refreshClimate`/`applyClimate`), `index.html` (`#screen-climate` people area), `style.css`
- Modify: `AGENTS.md` (Climate tab notes: the people controls, home Wi-Fi only)
- Test: `tests/python/test_homebody_role.py`, `tests/js/climate-model.test.js`

**Interfaces:**
- Consumes: Task 2's three endpoints and the status document's `id` and `removable` per person.
- Produces, `climate-model.js`:
  - `parseClimate` keeps `id` (string) and `removable` (boolean, default false) on each person.
  - `parseThisDevice(raw) -> { canAdd: boolean, countedAs: string | null, reason: "remote" | "unknown" | null } | null` (null for anything malformed).
  - `deviceLine(device) -> { kind: "add" | "counted" | "remote" | "none", text: string }`: add -> `"Count this phone as home"`; counted -> `"This phone counts as ${countedAs}."`; remote -> `"To count a phone, open this page on home Wi-Fi."`; unknown or null -> kind none, text `""`.
  - `checkName(text, people) -> string | null`: the same rules as homebody's `clean_name` (trim, 1 to 30, no control characters, case-insensitive unique against `people` names); returns the message or null.
  - `addBody(name) -> string` (`JSON.stringify({ name: name.trim() })`), `removeBody(id) -> string`.

- [ ] **Step 1: Write the failing tests**

Python: `test_homebody_api_route_sets_the_client_address` asserts the rendered template's `/api/homebody/` block contains `proxy_set_header X-Real-IP $remote_addr;`.
JS (`node:test`): `parseThisDevice` (valid doc, missing fields -> null, unknown reason -> null); `deviceLine` for all four kinds with the exact copy above; `checkName` rejecting `"   "`, 31 characters, a newline and `"ALEX"` when Alex is listed, accepting `" Sam "`; `parseClimate` keeping `id`/`removable` and defaulting `removable` to false.

- [ ] **Step 2: Run them to see them fail**

Run: `node --test tests/js/climate-model.test.js` and `uv run pytest tests/python/test_homebody_role.py -q`
Expected: FAIL on the new tests only.

- [ ] **Step 3: Implement the template line, the model functions and the page wiring**

Fetch `api/homebody/people/this-device` when the Climate tab opens and after any add or remove. The add flow: button -> name input + Add; `checkName` before posting; the server's `error` text shown on refusal; the reply's status document goes through `applyClimate`. Remove: each removable person row gets a Remove button that asks for confirmation before posting (reuse the page's existing confirm or undo pattern if one fits). POSTs send `X-Snaphouse: 1` like the settings save.

- [ ] **Step 4: Run every check CI runs**

Run: `node --test tests/js/ && uv run pytest tests/python -q && ansible-lint`
Expected: all pass; read each result line.

- [ ] **Step 5: Commit, push, open the PR**

```bash
git add roles tests AGENTS.md
git commit -m "Count this phone as home from the Climate tab"
```
The PR body says it depends on the homebody PR (deploy homebody first; until then the page shows no people controls because `this-device` 404s, which `parseThisDevice` treats as null).
