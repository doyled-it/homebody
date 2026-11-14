import json
import os
import stat

import pytest

from homebody.garage import GarageModel, GarageStore
from homebody.garage_link import LinkState


class Clock:
    def __init__(self, now: float = 10_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def door(operation="idle", position=None, has_position=False, **extra):
    return LinkState(
        operation=operation, position=position, has_position=has_position, **extra
    )


def connected_model(clock, saved=None):
    model = GarageModel(clock, saved)
    model.link_changed(True)
    return model


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        (door("opening", 0.0), "opening"),
        (door("closing", 1.0), "closing"),
        (door("idle", 0.0), "closed"),
        (door("idle", 1.0), "open"),
        (door("idle", 0.4, has_position=True), "stopped"),
        (door("idle", 0.4, has_position=False), "open"),
    ],
)
def test_state_derivation_table(link, expected):
    model = connected_model(Clock())
    model.state_received(link)
    assert model.state == expected


def test_unknown_until_first_push_and_after_drop():
    clock = Clock()
    model = GarageModel(clock)
    assert model.state == "unknown"
    model.link_changed(True)
    assert model.state == "unknown"
    model.state_received(door("idle", 0.0, obstruction=False))
    assert model.state == "closed"
    clock.now += 60
    pushed_at = clock.now
    model.state_received(door("idle", 0.0))
    clock.now += 600
    model.link_changed(False)
    assert model.state == "unknown"
    assert model.connected is False
    assert model.last_seen == pushed_at
    assert model.last_known_state == "closed"
    assert model.obstruction is None
    model.link_changed(True)
    assert model.state == "unknown"


def test_since_resets_only_on_a_change():
    clock = Clock(1000.0)
    model = connected_model(clock)
    model.state_received(door("idle", 0.0))
    first = model.since
    clock.now += 100
    model.state_received(door("idle", 0.0))
    assert model.since == first
    clock.now += 100
    model.state_received(door("opening", 0.2))
    assert model.since == 1200.0
    assert model.since_approximate is False


def test_since_is_kept_when_reconnect_state_matches_saved():
    clock = Clock(1000.0)
    first = connected_model(clock)
    first.state_received(door("idle", 1.0))
    saved = first.to_saved()
    clock.now = 5000.0
    model = connected_model(clock, saved)
    model.state_received(door("idle", 1.0))
    assert model.since == 1000.0
    assert model.since_approximate is True  # inherited


def test_since_is_kept_exactly_when_saved_since_was_exact():
    clock = Clock(1000.0)
    first = connected_model(clock)
    first.state_received(door("idle", 0.0))
    clock.now = 1100.0
    first.state_received(door("opening", 0.1))
    saved = first.to_saved()
    clock.now = 5000.0
    model = connected_model(clock, saved)
    model.state_received(door("opening", 0.1))
    assert model.since == 1100.0
    assert model.since_approximate is False


def test_since_is_approximate_when_reconnect_state_differs():
    clock = Clock(1000.0)
    first = connected_model(clock)
    first.state_received(door("idle", 0.0))
    saved = first.to_saved()
    clock.now = 5000.0
    model = connected_model(clock, saved)
    clock.now = 5005.0
    model.state_received(door("idle", 1.0))
    assert model.since == 5000.0
    assert model.since_approximate is True


def test_episode_lifecycle():
    clock = Clock(1000.0)
    model = connected_model(clock)
    model.state_received(door("idle", 0.0))
    assert model.episode is None
    clock.now = 1100.0
    model.state_received(door("opening", 0.1))
    assert model.episode is not None
    assert model.episode.started == 1100.0
    model.episode.fired.add("left_open")
    model.state_received(door("idle", 1.0))
    model.state_received(door("closing", 0.5))
    assert model.episode is not None
    assert model.episode.fired == {"left_open"}
    model.link_changed(False)
    assert model.episode is not None
    model.link_changed(True)
    assert model.state == "unknown"
    assert model.episode is not None
    clock.now = 1500.0
    model.state_received(door("idle", 1.0))
    assert model.episode is not None
    assert model.episode.started == 1100.0
    model.state_received(door("closing", 0.5))
    model.state_received(door("idle", 0.0))
    assert model.episode is None


def test_reconnect_burst_does_not_start_a_second_episode():
    clock = Clock(1000.0)
    model = connected_model(clock)
    states = []
    for link in (
        door("idle", 1.0),
        LinkState(obstruction=False),
        door("idle", 1.0),
        LinkState(light=True),
    ):
        model.state_received(link)
        states.append(model.state)
    assert "closed" not in states
    assert model.state == "open"
    assert model.light is True
    assert model.episode is not None
    assert model.pop_changes() == [(1000.0, "open")]
    # An entity push before the door is known never reads closed either.
    other = connected_model(clock)
    other.state_received(LinkState(obstruction=True))
    assert other.state == "unknown"
    assert other.obstruction is True


def test_clock_stepping_backward_clamps():
    clock = Clock(10_000.0)
    model = connected_model(clock)
    model.state_received(door("idle", 0.0))
    model.state_received(door("opening", 0.1))
    clock.now = 10_000.0 - 1800.0
    model.state_received(door("idle", 1.0))
    now = clock.now
    assert model.since is not None
    assert model.since <= now
    assert model.last_seen is not None
    assert model.last_seen <= now
    assert model.episode is not None
    assert model.episode.started <= now
    assert model.state == "open"


def test_restart_restore_keeps_episode_and_fired_kinds_when_still_open():
    clock = Clock(1000.0)
    first = connected_model(clock)
    first.state_received(door("idle", 0.0))
    clock.now = 1100.0
    first.state_received(door("idle", 1.0))
    assert first.episode is not None
    first.episode.fired.add("left_open")
    saved = json.loads(json.dumps(first.to_saved()))
    clock.now = 9000.0
    model = connected_model(clock, saved)
    model.state_received(door("idle", 1.0))
    assert model.episode is not None
    assert model.episode.started == 1100.0
    assert model.episode.fired == {"left_open"}


def test_restart_drops_episode_when_board_reports_closed():
    clock = Clock(1000.0)
    first = connected_model(clock)
    first.state_received(door("idle", 0.0))
    first.state_received(door("idle", 1.0))
    saved = first.to_saved()
    model = connected_model(Clock(9000.0), saved)
    model.state_received(door("idle", 0.0))
    assert model.episode is None


def test_restart_without_saved_episode_starts_approximate_one_if_open():
    model = connected_model(Clock(9000.0), {})
    model.state_received(door("idle", 1.0))
    assert model.episode is not None
    assert model.episode.started == 9000.0
    assert model.episode.since_approximate is True


def test_remote_and_close_command_marks():
    clock = Clock(1000.0)
    model = connected_model(clock)
    model.state_received(door("idle", 0.0))
    model.note_remote_command()
    model.state_received(door("opening", 0.1))
    assert model.episode is not None
    assert model.episode.remote_started is True
    assert model.episode.close_commanded is False
    model.note_command("close")
    assert model.episode.close_commanded is True
    saved = model.to_saved()
    restored = connected_model(clock, saved)
    restored.state_received(door("idle", 1.0))
    assert restored.episode is not None
    assert restored.episode.close_commanded is True
    assert restored.episode.remote_started is True


def test_pop_changes_drains():
    model = connected_model(Clock(1000.0))
    model.state_received(door("idle", 0.0))
    assert model.pop_changes() == [(1000.0, "closed")]
    assert model.pop_changes() == []


def test_to_saved_roundtrip_ignores_damaged_fields():
    clock = Clock(1000.0)
    model = connected_model(clock)
    model.state_received(door("idle", 1.0))
    clean = GarageModel(clock, json.loads(json.dumps(model.to_saved())))
    assert clean.last_known_state == "open"
    assert clean.episode is not None
    damaged = {
        "last_known_state": 7,
        "since": "yesterday",
        "since_approximate": "yes",
        "last_seen": True,
        "episode": {"started": "x", "fired": "left_open"},
    }
    fresh = GarageModel(clock, damaged)
    assert fresh.last_known_state is None
    assert fresh.since is None
    assert fresh.last_seen is None
    assert fresh.episode is None
    partial = GarageModel(
        clock, {"episode": {"started": 5, "fired": ["a", 3], "remote_started": "x"}}
    )
    assert partial.episode is not None
    assert partial.episode.fired == {"a"}
    assert partial.episode.remote_started is False
    assert GarageModel(clock, "junk").state == "unknown"  # type: ignore[arg-type]


def test_store_roundtrip_and_missing_and_damaged_file_start_fresh(tmp_path):
    store = GarageStore(tmp_path)
    assert store.load() == {}
    store.save({"a": 1})
    assert store.path.name == "garage.json"
    assert store.load() == {"a": 1}
    store.path.write_text("{not json")
    assert store.load() == {}
    store.path.write_text("[1, 2]")
    assert store.load() == {}


def test_store_write_failure_raises_oserror_without_touching_other_files(tmp_path):
    other = tmp_path / "state.json"
    other.write_text('{"climate": true}')
    store = GarageStore(tmp_path)
    store.save({"a": 1})
    tmp_path.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        if os.access(tmp_path, os.W_OK):
            pytest.skip("directory permissions not enforced (running as root)")
        with pytest.raises(OSError):
            store.save({"a": 2})
    finally:
        tmp_path.chmod(stat.S_IRWXU)
    assert other.read_text() == '{"climate": true}'
    assert store.load() == {"a": 1}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["garage.json", "state.json"]
