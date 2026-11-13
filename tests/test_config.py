"""Tests for config loading (src/homebody/config.py)."""

from __future__ import annotations

import pytest

from homebody import config

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
    assert str(default.state_dir) == "/var/lib/homebody"

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
