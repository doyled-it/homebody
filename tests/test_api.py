"""Tests for the settings API (src/homebody/api.py) and its daemon wiring."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from homebody import api, status
from homebody.people import PeopleError
from test_daemon import AWAY, COMFORT, MIN, SETPOINT_PATH, A, Rig

GUARD = {"x-snaphouse": "1"}
PATH = "/api/homebody/settings"


class StubSettings:
    def __init__(self) -> None:
        self.current = {
            "enabled": True, "comfort_f": 72, "away_f": 82, "away_minutes": 20,
        }
        self.applied: list[dict] = []

    def settings(self):
        return dict(self.current)

    def apply_settings(self, changes):
        self.applied.append(changes)
        self.current.update(changes)
        return {"version": 1, "settings": dict(self.current)}


def call(method="POST", path=PATH, headers=GUARD, body=None, stub=None):
    stub = stub or StubSettings()
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    code, doc = api.handle(stub, method, path, headers, raw)
    return code, doc, stub


def test_missing_guard_header_is_403_and_changes_nothing():
    for headers in ({}, {"x-snaphouse": "0"}, {"x-snaphouse": ""}):
        code, doc, stub = call(headers=headers, body={"comfort_f": 70})
        assert code == 403 and "error" in doc
        assert stub.applied == []


def test_other_paths_are_404_and_other_methods_405():
    assert call(path="/api/homebody/other", body={})[0] == 404
    assert call(path="/", body={})[0] == 404
    for method in ("GET", "PUT", "DELETE", "PATCH"):
        assert call(method=method, body={})[0] == 405


def test_valid_changes_are_applied_and_return_the_status_document():
    code, doc, stub = call(body={"comfort_f": 70, "enabled": False})
    assert code == 200
    assert stub.applied == [{"comfort_f": 70, "enabled": False}]
    assert doc["settings"]["comfort_f"] == 70


def test_an_empty_object_is_a_valid_no_op():
    code, _, stub = call(body={})
    assert code == 200 and stub.applied == [{}]


@pytest.mark.parametrize(
    "body",
    [
        {"comfort_f": 60}, {"comfort_f": 85, "away_f": 90},
        {"away_f": 65, "comfort_f": 64}, {"away_f": 90},
        {"away_minutes": 5}, {"away_minutes": 120},
    ],
)
def test_range_edges_are_accepted(body):
    assert call(body=body)[0] == 200


@pytest.mark.parametrize(
    "body",
    [
        {"comfort_f": 59}, {"comfort_f": 86}, {"away_f": 64}, {"away_f": 91},
        {"away_minutes": 4}, {"away_minutes": 121},
    ],
)
def test_out_of_range_is_400_with_a_plain_sentence(body):
    code, doc, stub = call(body=body)
    assert code == 400 and doc["error"].endswith(".")
    assert stub.applied == []


def test_comfort_must_stay_below_away_after_merging_with_current():
    assert call(body={"comfort_f": 82})[0] == 400  # away is 82 already
    assert call(body={"away_f": 72})[0] == 400  # comfort is 72 already
    assert call(body={"comfort_f": 80, "away_f": 80})[0] == 400
    assert call(body={"comfort_f": 75, "away_f": 80})[0] == 200


@pytest.mark.parametrize(
    "body",
    [
        {"nope": 1}, {"comfort_f": "70"}, {"comfort_f": 70.0}, {"comfort_f": True},
        {"enabled": 1}, {"enabled": "yes"}, {"away_minutes": None},
        [], "text", 5, None,
    ],
)
def test_unknown_keys_wrong_types_and_non_objects_are_400(body):
    code, doc, stub = call(body=body)
    assert code == 400 and "error" in doc
    assert stub.applied == []


@pytest.mark.parametrize("raw", [b"", b"{not json", b"\xff\xfe"])
def test_bad_json_is_400(raw):
    code, doc, _ = call(body=raw)
    assert code == 400 and "error" in doc


# Daemon wiring -------------------------------------------------------------


def settle(rig, minutes=5):
    rig.wifi.home = {A.wifi_mac}
    rig.run_for(minutes * MIN)


def test_settings_persist_across_a_restart_and_beat_local_toml(tmp_path):
    rig = Rig(tmp_path)
    rig.daemon.apply_settings({"comfort_f": 70, "away_minutes": 30})
    assert rig.saved()["settings"] == {"comfort_f": 70, "away_minutes": 30}
    rig.daemon = rig.new_daemon()
    assert rig.daemon.settings() == {
        "enabled": True, "comfort_f": 70, "away_f": 82, "away_minutes": 30,
        "garage_left_open_minutes": 15, "garage_away_open_grace_minutes": 5,
        "garage_alerts": True, "garage_remote": False,
    }
    rig.wifi.home = {A.wifi_mac}
    rig.run_for(2 * MIN)
    assert rig.setpoint() == 2111  # 70 F


def test_away_minutes_takes_effect_on_the_presence_window(tmp_path):
    rig = Rig(tmp_path)
    settle(rig)
    rig.wifi.home = set()
    rig.daemon.apply_settings({"away_minutes": 5})
    rig.run_for(9 * MIN)  # well under the old 20 minute window
    assert rig.daemon.occupied() is False
    assert rig.setpoint() == AWAY


def test_changed_setpoints_are_sent_without_flagging_an_override(tmp_path):
    rig = Rig(tmp_path)
    settle(rig)
    assert rig.setpoint() == COMFORT
    rig.daemon.apply_settings({"comfort_f": 70})
    rig.run_for(2 * MIN)
    assert rig.setpoint() == 2111
    assert rig.saved()["climate"]["override"] is False
    assert rig.daemon.status_document()["last_change"]["setpoint_f"] == 70.0


def test_changed_setpoints_do_not_beat_a_hand_change_override(tmp_path):
    rig = Rig(tmp_path)
    settle(rig)
    rig.controller.nodes[1]["attributes"][SETPOINT_PATH] = 2389
    rig.daemon.apply_settings({"comfort_f": 73})  # finds the hand change
    rig.run_for(5 * MIN)
    assert rig.saved()["climate"]["override"] is True
    writes = len(rig.controller.writes())
    rig.daemon.apply_settings({"away_f": 85})
    rig.run_for(5 * MIN)
    assert len(rig.controller.writes()) == writes


def test_disabled_sends_no_commands_while_presence_keeps_updating(tmp_path):
    rig = Rig(tmp_path)
    rig.daemon.apply_settings({"enabled": False})
    rig.wifi.home = {A.wifi_mac}
    rig.run_for(5 * MIN)
    assert rig.controller.writes() == []
    doc = rig.daemon.status_document()
    assert doc["enabled"] is False
    assert doc["occupied"] is True
    assert doc["people"][0]["state"] == "home"
    # Turning it back on acts on the current occupancy.
    rig.daemon.apply_settings({"enabled": True})
    rig.run_for(2 * MIN)
    assert rig.setpoint() == COMFORT


def test_status_file_is_written_after_ticks_and_settings_changes(tmp_path):
    out = tmp_path / "pub"
    rig = Rig(tmp_path)
    rig.daemon._status = status.StatusWriter(out)
    rig.tick()
    first = json.loads((out / "homebody.json").read_text())
    assert first["thermostat"]["reachable"] is True
    assert first["thermostat"]["setpoint_f"] == 82.0
    assert first["occupied"] is None
    rig.daemon.apply_settings({"enabled": False})
    assert json.loads((out / "homebody.json").read_text())["enabled"] is False


def test_status_names_only_enabled_people_and_marks_unreachable(tmp_path):
    rig = Rig(tmp_path)
    rig.controller.down = True
    rig.run_for(2 * MIN)
    doc = rig.daemon.status_document()
    assert [p["name"] for p in doc["people"]] == ["Person A"]
    assert doc["thermostat"]["reachable"] is False


# Transport -----------------------------------------------------------------


async def _http(port: int, request: bytes) -> tuple[int, dict]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    raw = await reader.read()
    writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(body)


def _post(body: bytes, guard: bool = True, path: str = PATH) -> bytes:
    lines = [f"POST {path} HTTP/1.1", "Host: x", f"Content-Length: {len(body)}"]
    if guard:
        lines.append("X-Snaphouse: 1")
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def test_http_server_end_to_end(tmp_path: Path):
    rig = Rig(tmp_path)

    async def scenario():
        server = await api.start(rig.daemon, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            ok = await _http(port, _post(b'{"comfort_f": 71}'))
            noguard = await _http(port, _post(b"{}", guard=False))
            bad = await _http(port, _post(b"{oops"))
            missing = await _http(port, _post(b"{}", path="/nope"))
            get = await _http(
                port, b"GET /api/homebody/settings HTTP/1.1\r\nHost: x\r\n\r\n"
            )
            junk = await _http(port, b"garbage\r\n\r\n")
            big = await _http(
                port,
                b"POST /api/homebody/settings HTTP/1.1\r\nX-Snaphouse: 1\r\n"
                b"Content-Length: 999999\r\n\r\n",
            )
        finally:
            server.close()
            await server.wait_closed()
        return ok, noguard, bad, missing, get, junk, big

    ok, noguard, bad, missing, get, junk, big = asyncio.run(scenario())
    assert ok[0] == 200 and ok[1]["settings"]["comfort_f"] == 71
    assert noguard[0] == 403
    assert bad[0] == 400
    assert missing[0] == 404
    assert get[0] == 405
    assert junk[0] == 400
    assert big[0] == 413
    assert rig.saved()["settings"] == {"comfort_f": 71}


# People endpoints ("count this phone as home")
DEVICE = "/api/homebody/people/this-device"
ADD = "/api/homebody/people/add"
REMOVE = "/api/homebody/people/remove"
PHONE = "02:00:00:00:00:0a"
LAN = {"x-real-ip": "192.0.2.5"}


class StubPeople(StubSettings):
    def __init__(self, counted=None):
        super().__init__()
        self.counted = counted
        self.added: list[tuple] = []
        self.removed: list[object] = []

    def counted_as(self, mac):
        return self.counted

    def add_person(self, name, mac):
        self.added.append((name, mac))
        if self.counted:
            raise PeopleError(f"This phone is already counted as {self.counted}.", 409)
        return {"version": 1, "people": [name]}

    def remove_person(self, person_id):
        self.removed.append(person_id)
        if person_id == "nope":
            raise PeopleError("That person cannot be removed here.")
        return {"version": 1, "people": []}


def lookup_for(table):
    return lambda ip: table.get(ip)


def people_call(method, path, headers=None, body=None, stub=None, table=None):
    stub = stub or StubPeople()
    table = {"192.0.2.5": PHONE} if table is None else table
    raw = b"" if body is None else json.dumps(body).encode()
    code, doc = api.handle(
        stub, method, path, {**GUARD, **(headers or {})}, raw, lookup_for(table)
    )
    return code, doc, stub


def test_tunnel_request_cannot_add():
    headers = {**LAN, "cf-connecting-ip": "203.0.113.9"}
    code, doc, stub = people_call("POST", ADD, headers, {"name": "Sam"})
    assert code == 403 and doc["reason"] == "remote"
    assert doc["error"] == "Count a phone from the home Wi-Fi, not from away."
    assert stub.added == []


def device(can_add, counted_as, reason):
    return 200, {"can_add": can_add, "counted_as": counted_as, "reason": reason}


def test_this_device_reports_remote_unknown_counted_and_addable():
    tunnel = {**LAN, "cf-connecting-ip": "203.0.113.9"}
    assert people_call("GET", DEVICE, tunnel)[:2] == (device(False, None, "remote"))
    assert people_call("GET", DEVICE, {}, table={})[:2] == device(
        False, None, "unknown"
    )
    counted = people_call("GET", DEVICE, LAN, stub=StubPeople(counted="Alex"))
    assert counted[:2] == device(False, "Alex", None)
    assert people_call("GET", DEVICE, LAN)[:2] == device(True, None, None)


def test_this_device_needs_no_guard_header():
    stub = StubPeople()
    table = lookup_for({"192.0.2.5": PHONE})
    code, _ = api.handle(stub, "GET", DEVICE, LAN, b"", table)
    assert code == 200


def test_add_unknown_device_is_403_unknown():
    code, doc, stub = people_call("POST", ADD, {}, {"name": "Sam"}, table={})
    assert code == 403 and doc["reason"] == "unknown"
    assert doc["error"] == "The hub could not tell which device this is."
    assert stub.added == []


def test_add_from_lan_returns_the_status_document():
    code, doc, stub = people_call("POST", ADD, LAN, {"name": "Sam"})
    assert code == 200 and doc["people"] == ["Sam"]
    assert stub.added == [("Sam", PHONE)]


def test_add_from_counted_device_is_409():
    stub = StubPeople("Alex")
    code, doc, _ = people_call("POST", ADD, LAN, {"name": "Sam"}, stub=stub)
    assert code == 409
    assert doc == {"error": "This phone is already counted as Alex."}


def test_add_needs_guard_header():
    stub = StubPeople()
    raw = json.dumps({"name": "Sam"}).encode()
    code, _ = api.handle(stub, "POST", ADD, LAN, raw, lookup_for({"192.0.2.5": PHONE}))
    assert code == 403 and stub.added == []


def test_remove_through_tunnel_is_allowed():
    headers = {**LAN, "cf-connecting-ip": "203.0.113.9"}
    code, _, stub = people_call("POST", REMOVE, headers, {"id": "added-00000001"})
    assert code == 200 and stub.removed == ["added-00000001"]
    code, doc, _ = people_call("POST", REMOVE, headers, {"id": "nope"})
    assert code == 400 and doc == {"error": "That person cannot be removed here."}


def test_remove_needs_guard_header():
    stub = StubPeople()
    code, _ = api.handle(stub, "POST", REMOVE, {}, b'{"id": "x"}', lookup_for({}))
    assert code == 403 and stub.removed == []


def test_people_paths_reject_wrong_method():
    for method in ("POST", "PUT", "DELETE"):
        assert people_call(method, DEVICE, LAN)[0] == 405
    for path in (ADD, REMOVE):
        for method in ("GET", "PUT", "DELETE"):
            assert people_call(method, path, LAN, {})[0] == 405


def test_add_rejects_unknown_keys_and_bad_bodies():
    code, doc, stub = people_call("POST", ADD, LAN, {"name": "Sam", "wifi_mac": PHONE})
    assert code == 400 and "error" in doc and stub.added == []
    assert people_call("POST", ADD, LAN, ["Sam"])[0] == 400
    code, _, stub = people_call("POST", REMOVE, LAN, {"id": "x", "extra": 1})
    assert code == 400 and stub.removed == []
    table = lookup_for({"192.0.2.5": PHONE})
    code, _ = api.handle(StubPeople(), "POST", ADD, {**GUARD, **LAN}, b"{", table)
    assert code == 400


def test_resolve_device_order():
    table = lookup_for({"192.0.2.5": PHONE})
    both = {"cf-connecting-ip": "203.0.113.9", "x-real-ip": "192.0.2.5"}
    assert api.resolve_device(both, table) == (None, "remote")
    assert api.resolve_device({}, table) == (None, "unknown")
    assert api.resolve_device({"x-real-ip": "192.0.2.6"}, table) == (None, "unknown")
    assert api.resolve_device({"x-real-ip": "192.0.2.5"}, table) == (PHONE, None)


# The garage endpoints

from test_garage_commands import make as make_garage  # noqa: E402

INTENT = "/api/homebody/garage/intent"
COMMAND = "/api/homebody/garage/command"
HOME = {"x-snaphouse": "1", "x-snaphouse-origin": "home"}
EMAIL = "cf-access-authenticated-user-email"
TUNNEL = {"x-snaphouse": "1", EMAIL: "person@example.invalid"}


class StubDaemon(StubSettings):
    def __init__(self, target=None):
        super().__init__()
        self.target = target

    def publish(self):
        return {"version": 1, "garage": self.target.snapshot() if self.target else None}

    def apply_settings(self, changes):
        super().apply_settings(changes)
        return self.publish()


class GarageStub:
    """What api.GarageTarget needs, over a real CommandGate."""

    timeout_seconds = 5.0

    def __init__(self, env):
        self.env = env
        self.gate = env.gate

    def snapshot(self):
        return {"door": self.env.model.state}

    def actor_tag(self, email):
        return self.env.audit.tag(email)


def garage_rig(tmp_path, **kwargs):
    env = make_garage(tmp_path, **kwargs)
    target = GarageStub(env)
    return env, target, StubDaemon(target)


def gcall(target, daemon, path, body, headers=HOME, method="POST"):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return asyncio.run(api.handle_garage(target, daemon, method, path, headers, raw))


def full(target, daemon, action, headers=HOME):
    code, intent = gcall(target, daemon, INTENT, {"action": action}, headers)
    assert code == 200, intent
    return gcall(
        target, daemon, COMMAND, {"action": action, "nonce": intent["nonce"]}, headers
    )


@pytest.mark.parametrize("path", [INTENT, COMMAND])
def test_garage_guard_header_is_403(tmp_path, path):
    env, target, daemon = garage_rig(tmp_path)
    for headers in ({}, {"x-snaphouse": "0"}, {"x-snaphouse-origin": "home"}):
        code, doc = gcall(target, daemon, path, {"action": "close"}, headers)
        assert code == 403 and "error" in doc
    assert env.link.sent == []


@pytest.mark.parametrize(
    "headers, expected",
    [
        ({"x-snaphouse-origin": "home"}, "home"),
        ({}, "tunnel"),
        ({"x-snaphouse-origin": "HOME"}, "tunnel"),
        ({"x-snaphouse-origin": "home "}, "tunnel"),
        ({"x-snaphouse-origin": "home, tunnel"}, "tunnel"),
        ({"x-snaphouse-origin": ""}, "tunnel"),
        ({"x-snaphouse-origin": "tunnel"}, "tunnel"),
        ({"cf-connecting-ip": "203.0.113.9"}, "tunnel"),
        ({"cf-connecting-ip": "203.0.113.9", "x-snaphouse-origin": "home"}, "home"),
    ],
)
def test_origin_home_only_when_header_is_exactly_home(headers, expected):
    assert api.origin_of(headers) == expected


def test_forged_forwarding_headers_do_not_make_a_request_home(tmp_path):
    env, target, daemon = garage_rig(tmp_path)
    forged = {"x-snaphouse": "1", "x-real-ip": "192.0.2.5", "x-forwarded-for": "x"}
    code, doc = gcall(target, daemon, INTENT, {"action": "close"}, forged)
    assert code == 403  # a tunnel request with no Access identity
    assert env.link.sent == []


@pytest.mark.parametrize("path", [INTENT, COMMAND])
def test_tunnel_request_without_access_email_is_403(tmp_path, path):
    env, target, daemon = garage_rig(tmp_path)
    for headers in ({"x-snaphouse": "1"}, {"x-snaphouse": "1", EMAIL: ""}):
        body = {"action": "close", "nonce": "x"}
        code, _ = gcall(target, daemon, path, body, headers)
        assert code == 403


def test_intent_then_command_happy_path_home_and_tunnel(tmp_path):
    env, target, daemon = garage_rig(tmp_path)
    code, intent = gcall(target, daemon, INTENT, {"action": "open"})
    assert code == 200
    assert set(intent) == {"nonce", "expires_in", "confirm_required"}
    assert intent["confirm_required"] is False and intent["expires_in"] == 30
    code, doc = gcall(
        target, daemon, COMMAND, {"action": "open", "nonce": intent["nonce"]}
    )
    assert code == 200 and doc == {"version": 1, "garage": {"door": "opening"}}
    assert env.link.sent == ["open"]
    # a no-op repeat is still a 200 with the document
    env.link.settle()
    code, doc = full(target, daemon, "open")
    assert code == 200 and env.link.sent == ["open"] and "garage" in doc

    env2, target2, daemon2 = garage_rig(tmp_path / "t")
    code, intent = gcall(target2, daemon2, INTENT, {"action": "open"}, TUNNEL)
    assert code == 200 and intent["confirm_required"] is True
    code, doc = gcall(
        target2, daemon2, COMMAND,
        {"action": "open", "nonce": intent["nonce"]}, TUNNEL,
    )
    assert code == 200 and env2.link.sent == ["open"]
    log = env2.audit.path.read_text()
    assert "person@example.invalid" not in log
    assert env2.audit.tag("person@example.invalid") in log


@pytest.mark.parametrize("bad", [["open"], 5, None, "x" * 100_000, {"a": 1}, True])
def test_command_bad_types_for_action_and_nonce_are_400(tmp_path, bad):
    env, target, daemon = garage_rig(tmp_path)
    for path, body in (
        (INTENT, {"action": bad}),
        (COMMAND, {"action": bad, "nonce": "x"}),
        (COMMAND, {"action": "open", "nonce": bad}),
    ):
        code, doc = gcall(target, daemon, path, body)
        assert code == 400 and "error" in doc
    assert env.link.sent == []


@pytest.mark.parametrize(
    "raw", [b"", b"{oops", b"[]", b'"x"', b'{"action": "open", "extra": 1}'],
)
def test_garage_bodies_must_be_json_objects_with_known_keys(tmp_path, raw):
    env, target, daemon = garage_rig(tmp_path)
    assert gcall(target, daemon, INTENT, raw)[0] == 400
    assert gcall(target, daemon, COMMAND, raw)[0] == 400


def test_command_without_nonce_is_400(tmp_path):
    env, target, daemon = garage_rig(tmp_path)
    assert gcall(target, daemon, COMMAND, {"action": "open"})[0] == 400


def test_nonce_for_other_action_and_spent_nonce_fail(tmp_path):
    env, target, daemon = garage_rig(tmp_path, door="open")
    nonce = gcall(target, daemon, INTENT, {"action": "open"})[1]["nonce"]
    code, _ = gcall(target, daemon, COMMAND, {"action": "close", "nonce": nonce})
    assert code == 400 and env.link.sent == []
    nonce = gcall(target, daemon, INTENT, {"action": "close"})[1]["nonce"]
    assert gcall(target, daemon, COMMAND, {"action": "close", "nonce": nonce})[0] == 200
    code, _ = gcall(target, daemon, COMMAND, {"action": "close", "nonce": nonce})
    assert code == 400 and env.link.sent == ["close"]


def _code(env, target, daemon, action, headers=HOME):
    return full(target, daemon, action, headers)[0]


def test_every_error_code_maps(tmp_path):
    seen: dict[int, str] = {}

    env, target, daemon = garage_rig(tmp_path / "403", remote=False)
    code, doc = full(target, daemon, "open", TUNNEL)
    seen[code] = doc["error"]

    env, target, daemon = garage_rig(tmp_path / "409", door="open")
    env.link.push(obstruction=True)
    code, doc = full(target, daemon, "close")
    seen[code] = doc["error"]

    env, target, daemon = garage_rig(tmp_path / "423")
    env.link.push(lockout=True)
    code, doc = full(target, daemon, "open")
    seen[code] = doc["error"]

    env, target, daemon = garage_rig(tmp_path / "429")
    assert full(target, daemon, "open")[0] == 200
    env.link.settle()
    code, doc = full(target, daemon, "close")
    seen[code] = doc["error"]

    env, target, daemon = garage_rig(tmp_path / "503", connect=False)
    code, doc = full(target, daemon, "open")
    seen[code] = doc["error"]

    env, target, daemon = garage_rig(tmp_path / "504")
    env.link.swallow = True
    code, doc = full(target, daemon, "open")
    seen[code] = doc["error"]

    assert sorted(seen) == [403, 409, 423, 429, 503, 504]
    assert all(isinstance(m, str) and m for m in seen.values())


def test_garage_paths_404_when_module_off():
    stub = StubDaemon()
    for path in (INTENT, COMMAND):
        code, doc = asyncio.run(
            api.handle_garage(None, stub, "POST", path, HOME, b'{"action": "open"}')
        )
        assert code == 404 and "error" in doc
        assert api.handle(stub, "POST", path, HOME, b"{}")[0] == 404


def test_garage_paths_reject_other_methods(tmp_path):
    env, target, daemon = garage_rig(tmp_path)
    for method in ("GET", "PUT", "DELETE"):
        assert gcall(target, daemon, INTENT, {}, HOME, method)[0] == 405


GARAGE_SETTINGS = [
    {"garage_left_open_minutes": 0}, {"garage_left_open_minutes": 240},
    {"garage_away_open_grace_minutes": 0}, {"garage_away_open_grace_minutes": 15},
    {"garage_alerts": False}, {"garage_remote": False},
]


@pytest.mark.parametrize("body", GARAGE_SETTINGS)
def test_settings_garage_keys_validate(tmp_path, body):
    env, target, daemon = garage_rig(tmp_path)
    code, doc = api.handle(daemon, "POST", PATH, GUARD, json.dumps(body).encode(),
                           garage=target)
    assert code == 200 and daemon.applied == [body]
    assert doc["garage"] == {"door": "closed"}


@pytest.mark.parametrize(
    "body",
    [
        {"garage_left_open_minutes": 241}, {"garage_left_open_minutes": -1},
        {"garage_left_open_minutes": "5"}, {"garage_left_open_minutes": True},
        {"garage_away_open_grace_minutes": 16}, {"garage_away_open_grace_minutes": 2.5},
        {"garage_alerts": 1}, {"garage_alerts": "true"}, {"garage_remote": None},
    ],
)
def test_settings_garage_keys_reject_bad_values(tmp_path, body):
    env, target, daemon = garage_rig(tmp_path)
    code, doc = api.handle(daemon, "POST", PATH, GUARD, json.dumps(body).encode(),
                           garage=target)
    assert code == 400 and "error" in doc and daemon.applied == []


@pytest.mark.parametrize("body", GARAGE_SETTINGS)
def test_settings_garage_keys_400_when_module_off(body):
    stub = StubDaemon()
    code, doc = api.handle(stub, "POST", PATH, GUARD, json.dumps(body).encode())
    assert code == 400 and doc == {"error": "The garage is not set up."}
    assert stub.applied == []


def test_enabling_garage_remote_from_tunnel_is_403_but_disabling_is_allowed(tmp_path):
    env, target, daemon = garage_rig(tmp_path)
    on = json.dumps({"garage_remote": True}).encode()
    off = json.dumps({"garage_remote": False}).encode()
    for headers in (GUARD, TUNNEL, {**GUARD, "x-snaphouse-origin": "Home"}):
        code, _ = api.handle(daemon, "POST", PATH, headers, on, garage=target)
        assert code == 403
    assert daemon.applied == []
    assert api.handle(daemon, "POST", PATH, TUNNEL, off, garage=target)[0] == 200
    assert api.handle(daemon, "POST", PATH, HOME, on, garage=target)[0] == 200
    assert daemon.applied == [{"garage_remote": False}, {"garage_remote": True}]


def test_people_and_climate_endpoints_unchanged_with_the_garage_on(tmp_path):
    env, target, daemon = garage_rig(tmp_path)
    code, doc = api.handle(
        daemon, "POST", PATH, GUARD, b'{"comfort_f": 70}', garage=target
    )
    assert code == 200 and daemon.applied == [{"comfort_f": 70}]
    assert api.handle(daemon, "GET", PATH, GUARD, b"", garage=target)[0] == 405
    assert api.handle(daemon, "POST", "/api/homebody/other", GUARD, b"{}",
                      garage=target)[0] == 404


async def _raw(port, path, body, headers):
    lines = [f"POST {path} HTTP/1.1", "Host: x", f"Content-Length: {len(body)}"]
    lines += [f"{k}: {v}" for k, v in headers.items()]
    return await _http(port, ("\r\n".join(lines) + "\r\n\r\n").encode() + body)


def test_slow_close_does_not_block_a_concurrent_settings_post(tmp_path):
    env, target, daemon = garage_rig(tmp_path, door="open", close_wait=0.6)
    env.link.follow_commands = False  # the confirm wait runs its full course

    async def scenario():
        server = await api.start(daemon, "127.0.0.1", 0, garage=target)
        port = server.sockets[0].getsockname()[1]
        try:
            nonce = (await _raw(port, INTENT, b'{"action":"close"}', HOME))[1]["nonce"]
            body = json.dumps({"action": "close", "nonce": nonce}).encode()
            slow = asyncio.ensure_future(_raw(port, COMMAND, body, HOME))
            await asyncio.sleep(0.1)
            started = asyncio.get_running_loop().time()
            quick = await _raw(port, PATH, b'{"comfort_f": 70}', GUARD)
            took = asyncio.get_running_loop().time() - started
            return quick, took, slow.done(), await slow
        finally:
            server.close()
            await server.wait_closed()

    quick, took, slow_done, slow = asyncio.run(scenario())
    assert quick[0] == 200 and took < 0.3
    assert slow_done is False
    assert slow[0] == 504


def test_command_wait_longer_than_the_read_timeout_is_not_cut_off(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(api, "READ_TIMEOUT_SECONDS", 0.2)
    env, target, daemon = garage_rig(tmp_path, open_wait=0.5)
    env.link.swallow = True

    async def scenario():
        server = await api.start(daemon, "127.0.0.1", 0, garage=target)
        port = server.sockets[0].getsockname()[1]
        try:
            nonce = (await _raw(port, INTENT, b'{"action":"open"}', HOME))[1]["nonce"]
            body = json.dumps({"action": "open", "nonce": nonce}).encode()
            return await _raw(port, COMMAND, body, HOME)
        finally:
            server.close()
            await server.wait_closed()

    code, doc = asyncio.run(scenario())
    assert code == 504 and "No movement" in doc["error"]


def test_the_garage_handler_has_its_own_cap(tmp_path):
    env, target, daemon = garage_rig(tmp_path, open_wait=2.0)
    target.timeout_seconds = 0.2
    env.link.swallow = True

    async def scenario():
        server = await api.start(daemon, "127.0.0.1", 0, garage=target)
        port = server.sockets[0].getsockname()[1]
        try:
            nonce = (await _raw(port, INTENT, b'{"action":"open"}', HOME))[1]["nonce"]
            body = json.dumps({"action": "open", "nonce": nonce}).encode()
            return await _raw(port, COMMAND, body, HOME)
        finally:
            server.close()
            await server.wait_closed()

    code, doc = asyncio.run(scenario())
    assert code == 504 and "error" in doc


def test_the_garage_paths_over_http_need_the_guard(tmp_path):
    env, target, daemon = garage_rig(tmp_path)

    async def scenario():
        server = await api.start(daemon, "127.0.0.1", 0, garage=target)
        port = server.sockets[0].getsockname()[1]
        try:
            return await _raw(port, INTENT, b'{"action":"open"}', {})
        finally:
            server.close()
            await server.wait_closed()

    assert asyncio.run(scenario())[0] == 403
