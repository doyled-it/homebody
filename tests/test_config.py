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
