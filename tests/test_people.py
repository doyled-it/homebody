"""Tests for the added-people helpers (src/homebody/people.py)."""

from __future__ import annotations

import re

import pytest

from homebody.people import (
    PeopleError,
    clean_name,
    new_person_id,
    parse_saved,
    to_saved,
)


def test_clean_name_trims_and_accepts():
    assert clean_name("  Sam ", ["Alex"]) == "Sam"


def test_clean_name_rejects_blank_long_control_and_duplicates():
    for bad in ["", "   ", "x" * 31, "Sa\nm", "alex", 5, None]:
        with pytest.raises(PeopleError) as e:
            clean_name(bad, ["Alex"])
        assert e.value.status == 400


def test_new_person_id_shape():
    assert re.fullmatch(r"added-[0-9a-f]{8}", new_person_id())


def test_bad_saved_people_are_skipped():
    raw = [
        {"id": "added-0000000a", "name": "Sam", "wifi_mac": "02:00:00:00:00:0a"},
        {"id": "added-0000000b", "wifi_mac": "02:00:00:00:00:0b"},
        {"id": "added-0000000c", "name": "Al", "wifi_mac": "nope"},
        "junk",
    ]
    people = parse_saved(raw)
    assert [p.name for p in people] == ["Sam"]
    assert people[0].enabled is True and people[0].bluetooth_mac is None
    assert parse_saved({"not": "a list"}) == []


def test_saved_round_trip():
    raw = [{"id": "added-0000000a", "name": "Sam", "wifi_mac": "02:00:00:00:00:0a"}]
    assert to_saved(parse_saved(raw)) == raw
