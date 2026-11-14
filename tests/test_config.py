"""Tests for config loading (src/homebody/config.py)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from homebody import config
from homebody.garage_link import DEFAULT_ENTITY_NAMES

EXAMPLE = """
[bluetooth]
adapter = "00:00:00:00:00:01"

[[person]]
name = "Person A"
enabled = true
wifi_mac = "AA:BB:CC:00:00:01"
bluetooth_mac = "aa:bb:cc:00:00:02"

[[person]]
name = "Person B"
enabled = false
wifi_mac = "aa:bb:cc:00:00:03"

[climate]
comfort_f = 70
away_f = 80
away_minutes = 15
"""


def test_loads_people_and_normalises_addresses():
    cfg = config.parse(EXAMPLE)
    a, b = cfg.people
    assert (a.name, a.enabled) == ("Person A", True)
    assert a.wifi_mac == "aa:bb:cc:00:00:01"
    assert a.bluetooth_mac == "aa:bb:cc:00:00:02"
    assert b.enabled is False
    assert b.bluetooth_mac is None
    assert cfg.bluetooth_adapter == "00:00:00:00:00:01"


def test_climate_values_and_defaults():
    cfg = config.parse(EXAMPLE)
    assert (cfg.comfort_f, cfg.away_f, cfg.away_minutes) == (70, 80, 15)
    bare = config.parse("")
    assert (bare.comfort_f, bare.away_f, bare.away_minutes) == (72, 82, 20)
    assert bare.people == ()
    assert bare.bluetooth_adapter is None


def test_enabled_defaults_to_true():
    cfg = config.parse('[[person]]\nname = "X"\nwifi_mac = "aa:bb:cc:00:00:01"\n')
    assert cfg.people[0].enabled is True


def test_invalid_mac_is_rejected_without_echoing_it():
    text = '[[person]]\nname = "X"\nwifi_mac = "not-a-mac"\n'
    with pytest.raises(config.ConfigError) as err:
        config.parse(text)
    assert "not-a-mac" not in str(err.value)


def test_person_needs_a_name_and_a_signal():
    with pytest.raises(config.ConfigError):
        config.parse('[[person]]\nwifi_mac = "aa:bb:cc:00:00:01"\n')
    with pytest.raises(config.ConfigError):
        config.parse('[[person]]\nname = "X"\n')


def test_load_reads_a_file(tmp_path):
    path = tmp_path / "local.toml"
    path.write_text(EXAMPLE)
    assert len(config.load(path).people) == 2


def test_load_missing_file_is_a_config_error(tmp_path):
    with pytest.raises(config.ConfigError):
        config.load(tmp_path / "absent.toml")


def test_enabled_people_excludes_disabled():
    cfg = config.parse(EXAMPLE)
    assert [p.name for p in cfg.enabled_people] == ["Person A"]


def test_person_ids_are_opaque_slugs_by_default_or_from_config():
    cfg = config.parse(EXAMPLE)
    assert [p.id for p in cfg.people] == ["person-1", "person-2"]
    named = config.parse(
        '[[person]]\nid = "p-one"\nname = "X"\nwifi_mac = "aa:bb:cc:00:00:01"\n'
    )
    assert named.people[0].id == "p-one"
    with pytest.raises(config.ConfigError):
        config.parse(
            '[[person]]\nid = "Bad Id"\nname = "X"\nwifi_mac = "aa:bb:cc:00:00:01"\n'
        )


def test_daemon_settings_default_and_parse():
    default = config.parse("")
    assert default.matter_url == "ws://127.0.0.1:5580/ws"
    assert default.matter_node_id == 0
    assert str(default.state_dir) == "/var/lib/homebody/state"

    custom = config.parse(
        '[matter]\nurl = "ws://127.0.0.1:9/ws"\nnode_id = 7\n'
        '[daemon]\nstate_dir = "/tmp/hb-state"\n'
    )
    assert custom.matter_url == "ws://127.0.0.1:9/ws"
    assert custom.matter_node_id == 7
    assert str(custom.state_dir) == "/tmp/hb-state"


def test_bad_node_id_is_rejected():
    with pytest.raises(config.ConfigError):
        config.parse('[matter]\nnode_id = "seven"\n')


def test_status_and_api_defaults_and_overrides():
    cfg = config.parse("")
    assert str(cfg.status_dir) == "/var/lib/snaphouse/public/homebody"
    assert cfg.api_port == 8783
    cfg = config.parse('[status]\ndir = "/tmp/x"\n[api]\nport = 9000\n')
    assert (str(cfg.status_dir), cfg.api_port) == ("/tmp/x", 9000)


@pytest.mark.parametrize("text", ["[api]\nport = 0\n", "[api]\nport = 70000\n",
                                  '[api]\nport = "80"\n', "[status]\ndir = 3\n"])
def test_bad_status_and_api_settings_are_rejected(text):
    with pytest.raises(config.ConfigError):
        config.parse(text)


# The garage module

GARAGE = """
[garage]
host = "board.example.invalid"
noise_psk = "example-key"
"""

FULL_GARAGE = """
[garage]
host = "board.example.invalid"
port = 6054
noise_psk = "example-key"
page_url = "https://house.example.invalid/"
entity_names = { cover = "Door" }
close_wait_seconds = 40
open_wait_seconds = 12
note_wait_seconds = 3
left_open_minutes = 20
away_open_grace_minutes = 2

[garage.ntfy]
url = "https://ntfy.example.invalid"
topic = "example-topic"
token = "example-token"

[garage.actors]
ab12 = "Person A"
"""


def test_garage_absent_means_off_without_error():
    cfg = config.parse(EXAMPLE)
    assert cfg.garage is None and cfg.garage_error is None


def test_garage_full_section_parses_with_defaults_for_ports_and_entity_names():
    cfg = config.parse(GARAGE)
    g = cfg.garage
    assert cfg.garage_error is None
    assert (g.host, g.port) == ("board.example.invalid", 6053)
    assert g.noise_psk == "example-key"
    assert g.allow_plaintext is False and g.ntfy is None and g.page_url is None
    assert dict(g.entity_names) == DEFAULT_ENTITY_NAMES
    assert (g.close_wait, g.open_wait, g.note_wait) == (30.0, 10.0, 5.0)
    assert (g.left_open_minutes, g.away_open_grace_minutes) == (15, 5)
    assert g.actors == {}


def test_garage_full_section_overrides_every_default():
    g = config.parse(FULL_GARAGE).garage
    assert g.port == 6054
    assert g.entity_names["cover"] == "Door"
    assert g.entity_names["lockout"] == DEFAULT_ENTITY_NAMES["lockout"]
    assert (g.close_wait, g.open_wait, g.note_wait) == (40.0, 12.0, 3.0)
    assert (g.left_open_minutes, g.away_open_grace_minutes) == (20, 2)
    assert g.actors == {"ab12": "Person A"}
    assert g.ntfy.topic == "example-topic" and g.ntfy.token == "example-token"
    assert g.timeout_seconds == 40.0 + 3.0 + 10.0


def test_ntfy_click_url_is_the_page_url_with_the_garage_tab():
    g = config.parse(FULL_GARAGE).garage
    assert g.ntfy.click_url == "https://house.example.invalid/#garage"
    no_page = FULL_GARAGE.replace('page_url = "https://house.example.invalid/"\n', "")
    assert config.parse(no_page).garage.ntfy.click_url is None


def test_partial_entity_names_merge_over_the_defaults():
    g = config.parse(FULL_GARAGE).garage
    assert set(g.entity_names) == set(DEFAULT_ENTITY_NAMES)


@pytest.mark.parametrize(
    "text, field",
    [
        ('[garage]\nnoise_psk = "example-key"\n', "host"),
        ('[garage]\nhost = "board.example.invalid"\n', "noise_psk"),
        ('[garage]\nhost = ""\nnoise_psk = "example-key"\n', "host"),
        (GARAGE + 'port = "x"\n', "port"),
        (GARAGE + "close_wait_seconds = 61\n", "close_wait_seconds"),
        (GARAGE + "left_open_minutes = 241\n", "left_open_minutes"),
        (GARAGE + "[garage.ntfy]\nurl = \"https://ntfy.example.invalid\"\n", "topic"),
        (GARAGE + "[garage.entity_names]\nbogus = \"x\"\n", "entity_names"),
    ],
)
def test_garage_missing_host_or_key_is_off_with_an_error_naming_the_field_not_the_value(
    text, field
):
    cfg = config.parse(text)
    assert cfg.garage is None
    assert field in cfg.garage_error
    for secret in ("board.example.invalid", "example-key", "example-topic"):
        assert secret not in cfg.garage_error


def test_plaintext_requires_the_explicit_flag():
    bare = '[garage]\nhost = "board.example.invalid"\n'
    assert config.parse(bare).garage is None
    ok = config.parse(bare + "allow_plaintext = true\n")
    assert ok.garage.allow_plaintext is True and ok.garage.noise_psk is None
    bad = config.parse(bare + 'allow_plaintext = "yes"\n')
    assert bad.garage is None and "allow_plaintext" in bad.garage_error


def test_garage_secrets_are_not_in_the_repr_of_the_error_or_logs(caplog):
    cfg = config.parse(GARAGE + 'port = "example-key"\n')
    assert cfg.garage is None
    assert "example-key" not in caplog.text


def test_bad_garage_never_breaks_the_rest_of_the_config():
    cfg = config.parse(EXAMPLE + '\n[garage]\nhost = 5\n')
    assert cfg.garage is None and cfg.garage_error
    assert len(cfg.people) == 2 and cfg.comfort_f == 70
    cfg = config.parse("garage = 3\n" + EXAMPLE)
    assert cfg.garage is None and "garage" in cfg.garage_error


def test_example_file_parses_and_holds_only_placeholders():
    text = (Path(__file__).resolve().parent.parent / "local.example.toml").read_text()
    assert config.parse(text).garage is None
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("# [garage]"))
    block = []
    for ln in lines[start:]:
        if ln.startswith("# [") and not ln.startswith("# [garage"):
            break
        if ln.startswith("# "):
            block.append(ln[2:])
    cfg = config.parse("\n".join(block))
    assert cfg.garage_error is None and cfg.garage is not None
    assert cfg.garage.host.startswith("<") and cfg.garage.noise_psk.startswith("<")
    assert cfg.garage.ntfy.topic.startswith("<")
    assert cfg.garage.entity_names["lockout"] == "Lock"
    assert cfg.garage.allow_plaintext is False
    assert (cfg.garage.close_wait, cfg.garage.open_wait) == (30.0, 10.0)
    assert not re.search(r"\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b", text, re.I)
    assert set(re.findall(r"\d+\.\d+\.\d+\.\d+", text)) <= {"127.0.0.1"}
    assert "@" not in text
