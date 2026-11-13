"""Tests for the pure decision logic (src/homebody/climate.py).

The controller sees only an injected clock and thermostat snapshots, so every
rule (one command per transition, override, pacing, backoff) is checked without
a controller, a network, or real time.
"""

from __future__ import annotations

from homebody import matter
from homebody.climate import ClimateController, ClimateState

MIN = 60.0
COOL = int(matter.SystemMode.COOL)
COMFORT = 2222
AWAY = 2778


class Clock:
    def __init__(self, now: float = 10_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make(state: ClimateState | None = None, clock: Clock | None = None):
    clock = clock or Clock()
    return ClimateController(comfort_f=72, away_f=82, clock=clock, state=state), clock


def thermostat(mode: int = COOL, setpoint: int = COMFORT) -> matter.ThermostatState:
    return matter.ThermostatState(
        node_id=1,
        endpoint=1,
        system_mode=mode,
        local_temperature_c100=2300,
        occupied_cooling_setpoint_c100=setpoint,
    )


def send(ctl: ClimateController, state: matter.ThermostatState):
    """Run one full successful command cycle; return the command or None."""
    cmd = ctl.decide(state)
    if cmd is not None:
        ctl.begin_command()
        ctl.succeeded(cmd)
    return cmd


def test_unknown_presence_never_commands():
    ctl, _ = make()
    ctl.observe(None)
    assert not ctl.due()
    assert ctl.decide(thermostat()) is None


def test_first_occupied_state_commands_comfort_cool():
    ctl, _ = make()
    ctl.observe(True)
    assert ctl.due()
    cmd = ctl.decide(thermostat(setpoint=AWAY))
    assert cmd is not None
    assert (cmd.mode, cmd.setpoint_c100) == (COOL, COMFORT)


def test_first_unoccupied_state_commands_away_setpoint():
    ctl, _ = make()
    ctl.observe(False)
    cmd = ctl.decide(thermostat())
    assert cmd is not None
    assert (cmd.mode, cmd.setpoint_c100) == (COOL, AWAY)


def test_one_command_per_transition():
    ctl, clock = make()
    ctl.observe(True)
    assert send(ctl, thermostat(setpoint=AWAY)) is not None
    clock.advance(10 * MIN)
    ctl.observe(True)
    ctl.observe(True)
    assert not ctl.due()
    assert ctl.decide(thermostat()) is None


def test_hand_change_records_override_and_sends_nothing():
    ctl, clock = make()
    ctl.observe(True)
    send(ctl, thermostat(setpoint=AWAY))
    clock.advance(30 * MIN)
    ctl.observe(False)  # a transition, but the Nest was changed by hand
    cmd = ctl.decide(thermostat(setpoint=2500))
    assert cmd is None
    assert ctl.state.override is True
    assert not ctl.due()


def test_hand_changed_mode_is_an_override_too():
    ctl, clock = make()
    ctl.observe(True)
    send(ctl, thermostat(setpoint=AWAY))
    clock.advance(30 * MIN)
    ctl.observe(False)
    assert ctl.decide(thermostat(mode=int(matter.SystemMode.OFF))) is None
    assert ctl.state.override is True


def test_override_held_until_next_transition_then_automation_resumes():
    ctl, clock = make()
    ctl.observe(True)
    send(ctl, thermostat(setpoint=AWAY))
    clock.advance(30 * MIN)
    ctl.observe(False)
    ctl.decide(thermostat(setpoint=2500))  # override recorded
    clock.advance(30 * MIN)
    ctl.observe(False)  # same state: still held
    assert not ctl.due()
    clock.advance(30 * MIN)
    ctl.observe(True)  # the next transition
    assert ctl.state.override is False
    assert ctl.due()
    # The thermostat still differs from the last command (the hand change), but
    # that is already recorded; automation takes over without re-flagging it.
    cmd = ctl.decide(thermostat(setpoint=2500))
    assert cmd is not None
    assert cmd.setpoint_c100 == COMFORT
    assert ctl.state.override is False


def test_rounding_by_the_thermostat_is_not_an_override():
    ctl, clock = make()
    ctl.observe(True)
    send(ctl, thermostat(setpoint=AWAY))
    clock.advance(30 * MIN)
    ctl.observe(False)
    # The Nest stores half degrees: 22.22 C comes back as 22.0 C.
    cmd = ctl.decide(thermostat(setpoint=2200))
    assert cmd is not None
    assert cmd.setpoint_c100 == AWAY
    assert ctl.state.override is False


def test_thermostat_already_at_target_sends_nothing_but_settles():
    ctl, _ = make()
    ctl.observe(True)
    assert ctl.decide(thermostat(setpoint=COMFORT)) is None
    assert not ctl.due()
    assert ctl.state.setpoint_c100 == COMFORT


def test_latest_state_wins_over_a_backlog_of_transitions():
    ctl, clock = make()
    ctl.observe(True)
    send(ctl, thermostat(setpoint=AWAY))
    # Outage: several transitions happen with nothing sent.
    for occupied in (False, True, False):
        clock.advance(25 * MIN)
        ctl.observe(occupied)
    cmd = ctl.decide(thermostat(setpoint=COMFORT))
    assert cmd is not None
    assert cmd.setpoint_c100 == AWAY  # only the latest state, once
    ctl.begin_command()
    ctl.succeeded(cmd)
    assert not ctl.due()


def test_a_flip_and_flip_back_during_an_outage_sends_nothing():
    ctl, clock = make()
    ctl.observe(True)
    send(ctl, thermostat(setpoint=AWAY))
    clock.advance(25 * MIN)
    ctl.observe(False)
    clock.advance(25 * MIN)
    ctl.observe(True)
    assert ctl.decide(thermostat(setpoint=COMFORT)) is None
    assert not ctl.due()


def test_commands_are_paced_to_one_a_minute():
    ctl, clock = make()
    ctl.observe(True)
    send(ctl, thermostat(setpoint=AWAY))
    clock.advance(20.0)
    ctl.observe(False)  # a quick flip right after the first command
    assert not ctl.due()
    clock.advance(39.0)
    assert not ctl.due()
    clock.advance(1.0)
    assert ctl.due()


def test_failure_backs_off_and_grows_then_resets_on_success():
    ctl, clock = make()
    ctl.observe(True)
    cmd = ctl.decide(thermostat(setpoint=AWAY))
    ctl.begin_command()
    ctl.failed()
    assert not ctl.due()
    clock.advance(59.0)
    assert not ctl.due()
    clock.advance(1.0)
    assert ctl.due()
    ctl.begin_command()
    ctl.failed()
    clock.advance(119.0)
    assert not ctl.due()  # second failure waits longer
    clock.advance(1.0)
    assert ctl.due()
    ctl.begin_command()
    ctl.succeeded(cmd)
    assert not ctl.due()


def test_retry_after_a_partial_write_is_not_mistaken_for_an_override():
    ctl, clock = make()
    ctl.observe(True)
    send(ctl, thermostat(setpoint=AWAY))
    clock.advance(30 * MIN)
    ctl.observe(False)
    cmd = ctl.decide(thermostat())
    ctl.begin_command()
    ctl.failed()  # the mode write landed, the setpoint write did not
    clock.advance(2 * MIN)
    retry = ctl.decide(thermostat(setpoint=COMFORT))
    assert retry == cmd
    assert ctl.state.override is False


def test_state_round_trips_through_a_dict():
    ctl, _ = make()
    ctl.observe(True)
    send(ctl, thermostat(setpoint=AWAY))
    restored = ClimateState.from_dict(ctl.state.to_dict())
    assert restored == ctl.state


def test_state_from_garbage_is_the_default():
    assert ClimateState.from_dict({"occupied": "banana", "mode": []}) == ClimateState()
    assert ClimateState.from_dict(None) == ClimateState()


def test_restored_state_does_not_recommand_the_same_occupancy():
    ctl, clock = make()
    ctl.observe(True)
    send(ctl, thermostat(setpoint=AWAY))
    state = ClimateState.from_dict(ctl.state.to_dict())
    clock.advance(10 * MIN)
    again, _ = make(state, clock)
    again.observe(True)
    assert not again.due()
