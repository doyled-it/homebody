import dataclasses

from homebody.garage import GarageModel, GarageSettings
from homebody.garage_alerts import Alert, AlertRules
from homebody.garage_link import LinkState

MIN = 60.0


class Clock:
    def __init__(self, now: float = 100_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, minutes: float) -> None:
        self.now += minutes * MIN


def door(position: float, operation: str = "idle") -> LinkState:
    return LinkState(operation=operation, position=position)


class Rig:
    def __init__(self, settings: GarageSettings | None = None) -> None:
        self.clock = Clock()
        self.model = GarageModel(self.clock)
        self.rules = AlertRules(self.model, self.clock)
        self.settings = settings or GarageSettings()
        self.model.link_changed(True)
        self.model.state_received(door(0.0))

    def open(self) -> None:
        self.model.state_received(door(1.0))

    def close(self) -> None:
        self.model.state_received(door(0.0))

    def evaluate(self, occupied=True, ntfy=True) -> list[Alert]:
        return self.rules.evaluate(occupied, self.settings, ntfy)

    def deliver(self, occupied=True) -> list[str]:
        """Evaluate and mark everything sent. Returns the kinds."""
        alerts = self.evaluate(occupied)
        for alert in alerts:
            self.rules.mark_sent(alert)
        return [a.kind for a in alerts]


def kinds(alerts):
    return [a.kind for a in alerts]


def test_left_open_fires_once_at_n_minutes_then_one_reminder_at_60():
    rig = Rig()
    rig.open()
    assert rig.deliver() == []
    rig.clock.advance(14)
    assert rig.deliver() == []
    rig.clock.advance(1)
    assert rig.deliver() == ["left_open"]
    rig.clock.advance(30)
    assert rig.deliver() == []
    rig.clock.advance(45)
    assert rig.deliver() == ["left_open_reminder"]
    rig.clock.advance(300)
    assert rig.deliver() == []


def test_left_open_zero_is_off():
    rig = Rig(GarageSettings(left_open_minutes=0))
    rig.open()
    rig.clock.advance(500)
    assert rig.deliver() == []


def test_left_open_priority_high_when_away():
    home = Rig()
    home.open()
    home.clock.advance(15)
    (alert,) = home.evaluate(occupied=True)
    assert alert.priority == "default"
    away = Rig(GarageSettings(away_open_grace_minutes=60))
    away.open()
    away.clock.advance(15)
    (alert,) = away.evaluate(occupied=False)
    assert alert.kind == "left_open"
    assert alert.priority == "high"


def test_opened_away_waits_for_grace_and_skips_when_someone_arrives():
    rig = Rig()
    rig.open()
    assert rig.evaluate(occupied=False) == []
    rig.clock.advance(4)
    assert rig.evaluate(occupied=False) == []
    rig.clock.advance(1)
    (alert,) = rig.evaluate(occupied=False)
    assert alert.kind == "opened_away"
    assert alert.priority == "high"
    rig.rules.mark_sent(alert)
    assert rig.evaluate(occupied=False) == []

    arrives = Rig()
    arrives.open()
    arrives.evaluate(occupied=False)
    arrives.clock.advance(3)
    arrives.evaluate(occupied=True)
    arrives.clock.advance(10)
    assert "opened_away" not in kinds(arrives.evaluate(occupied=False))


def test_opened_away_grace_zero_fires_at_once():
    rig = Rig(GarageSettings(away_open_grace_minutes=0))
    rig.open()
    assert kinds(rig.evaluate(occupied=False)) == ["opened_away"]


def test_opened_away_skipped_for_remote_started_episode():
    rig = Rig(GarageSettings(away_open_grace_minutes=0))
    rig.model.note_remote_command()
    rig.open()
    assert rig.evaluate(occupied=False) == []


def test_opened_away_not_judged_when_home_at_open():
    rig = Rig(GarageSettings(away_open_grace_minutes=0))
    rig.open()
    rig.evaluate(occupied=True)
    rig.clock.advance(1)
    assert "opened_away" not in kinds(rig.evaluate(occupied=False))


def test_left_open_away_on_occupied_true_to_false_and_folds_left_open():
    rig = Rig()
    rig.open()
    rig.evaluate(occupied=True)
    rig.clock.advance(20)
    (alert,) = rig.evaluate(occupied=False)
    assert alert.kind == "left_open_away"
    assert alert.priority == "high"
    assert "20 minutes" in alert.body
    rig.rules.mark_sent(alert)
    rig.clock.advance(1)
    assert rig.evaluate(occupied=False) == []
    rig.clock.advance(60)
    assert kinds(rig.evaluate(occupied=False)) == ["left_open_reminder"]


def test_left_open_away_not_when_door_closed():
    rig = Rig()
    rig.evaluate(occupied=True)
    assert rig.evaluate(occupied=False) == []


def test_unknown_occupancy_never_fires_away_rules_but_left_open_still_fires():
    rig = Rig(GarageSettings(away_open_grace_minutes=0))
    rig.open()
    assert rig.evaluate(occupied=None) == []
    rig.clock.advance(15)
    (alert,) = rig.evaluate(occupied=None)
    assert alert.kind == "left_open"
    assert alert.priority == "default"
    unk = Rig()
    unk.evaluate(occupied=True)
    unk.open()
    unk.evaluate(occupied=None)
    unk.clock.advance(1)
    assert unk.evaluate(occupied=None) == []


def test_closed_note_once_and_not_after_commanded_close():
    rig = Rig()
    rig.open()
    rig.clock.advance(15)
    assert rig.deliver() == ["left_open"]
    rig.close()
    (note,) = rig.evaluate()
    assert note.kind == "closed_note"
    assert note.priority == "low"
    rig.rules.mark_sent(note)
    assert rig.evaluate() == []

    cmd = Rig()
    cmd.open()
    cmd.clock.advance(15)
    cmd.deliver()
    cmd.model.note_command("close")
    cmd.close()
    assert cmd.evaluate() == []

    quiet = Rig()
    quiet.open()
    quiet.clock.advance(2)
    quiet.deliver()
    quiet.close()
    assert quiet.evaluate() == []


def test_link_offline_after_10_min_once_per_outage_with_back_note_short_drop_silent():
    rig = Rig()
    rig.evaluate()
    rig.model.link_changed(False)
    assert rig.evaluate() == []
    rig.clock.advance(9)
    assert rig.evaluate() == []
    rig.model.link_changed(True)
    assert rig.evaluate() == []
    rig.model.link_changed(False)
    rig.evaluate()
    rig.clock.advance(10)
    (alert,) = rig.evaluate()
    assert alert.kind == "link_offline"
    rig.rules.mark_sent(alert)
    rig.clock.advance(60)
    assert rig.evaluate() == []
    rig.model.link_changed(True)
    (back,) = rig.evaluate()
    assert back.kind == "link_back"
    rig.rules.mark_sent(back)
    assert rig.evaluate() == []
    rig.model.link_changed(False)
    rig.evaluate()
    rig.clock.advance(10)
    assert kinds(rig.evaluate()) == ["link_offline"]


def test_link_back_not_sent_when_offline_alert_never_went_out():
    rig = Rig()
    rig.model.link_changed(False)
    rig.evaluate()
    rig.clock.advance(11)
    (alert,) = rig.evaluate()
    rig.rules.mark_dropped(alert)
    rig.model.link_changed(True)
    assert rig.evaluate() == []


def test_flap_cap_sends_one_paused_note_then_drops(caplog):
    rig = Rig(GarageSettings(left_open_minutes=1))
    sent = []
    for _ in range(6):
        rig.open()
        rig.clock.advance(1)
        sent += rig.deliver()
        rig.model.note_command("close")
        rig.close()
        rig.deliver()
    assert sent.count("left_open") == 6
    rig.open()
    rig.clock.advance(1)
    out = rig.evaluate()
    assert kinds(out) == ["paused"]
    rig.rules.mark_sent(out[0])
    rig.model.note_command("close")
    rig.close()
    rig.open()
    rig.clock.advance(1)
    assert rig.evaluate() == []
    rig.clock.advance(61)
    rig.model.note_command("close")
    rig.close()
    rig.open()
    rig.clock.advance(1)
    assert kinds(rig.evaluate()) == ["left_open"]


def test_unsent_alert_is_offered_again_until_marked_sent():
    rig = Rig()
    rig.open()
    rig.clock.advance(15)
    first = rig.evaluate()
    assert kinds(first) == ["left_open"]
    rig.clock.advance(1)
    again = rig.evaluate()
    assert kinds(again) == ["left_open"]
    rig.rules.mark_sent(again[0])
    assert rig.evaluate() == []


def test_alerts_off_returns_nothing():
    rig = Rig(GarageSettings(away_open_grace_minutes=0))
    rig.open()
    rig.clock.advance(30)
    off = dataclasses.replace(rig.settings, alerts=False)
    assert rig.rules.evaluate(False, off, True) == []
    assert rig.rules.evaluate(False, rig.settings, False) == []


def test_fired_kinds_survive_restart_via_saved():
    rig = Rig()
    rig.open()
    rig.clock.advance(15)
    assert rig.deliver() == ["left_open"]
    saved = rig.model.to_saved()
    model = GarageModel(rig.clock, saved)
    model.link_changed(True)
    model.state_received(door(1.0))
    rules = AlertRules(model, rig.clock)
    assert rules.evaluate(True, rig.settings, True) == []
    rig.clock.advance(1)
    assert rules.evaluate(True, rig.settings, True) == []


def test_clock_stepping_back_fires_nothing_early():
    rig = Rig()
    rig.open()
    rig.clock.advance(-30)
    assert rig.evaluate() == []


# Added after the mutation check: each pins a behaviour a mutant survived on.


def test_reminder_is_60_minutes_after_the_left_open_threshold():
    rig = Rig()
    rig.open()
    rig.clock.advance(15)
    rig.deliver()
    rig.clock.advance(59)
    assert rig.deliver() == []
    rig.clock.advance(1)
    assert rig.deliver() == ["left_open_reminder"]


def test_reminder_scales_with_n():
    rig = Rig(GarageSettings(left_open_minutes=30))
    rig.open()
    rig.clock.advance(30)
    rig.deliver()
    rig.clock.advance(59)
    assert rig.deliver() == []
    rig.clock.advance(1)
    assert rig.deliver() == ["left_open_reminder"]


def test_reminder_never_arrives_with_or_before_the_first_alert():
    rig = Rig()
    rig.open()
    rig.clock.advance(200)
    assert kinds(rig.evaluate()) == ["left_open"]


def test_opened_away_needs_occupied_false_when_it_comes_due():
    rig = Rig()
    rig.open()
    rig.evaluate(occupied=False)
    rig.clock.advance(10)
    assert "opened_away" not in kinds(rig.evaluate(occupied=None))


def test_opened_away_not_judged_when_presence_unknown_at_open():
    rig = Rig(GarageSettings(away_open_grace_minutes=0))
    rig.open()
    rig.evaluate(occupied=None)
    rig.clock.advance(10)
    assert "opened_away" not in kinds(rig.evaluate(occupied=False))


def test_left_open_away_still_fires_after_left_open_was_sent():
    rig = Rig()
    rig.open()
    rig.evaluate(occupied=True)
    rig.clock.advance(15)
    assert rig.deliver(occupied=True) == ["left_open"]
    rig.clock.advance(5)
    (alert,) = rig.evaluate(occupied=False)
    assert alert.kind == "left_open_away"
    assert "20 minutes" in alert.body


def test_unknown_presence_in_between_does_not_forget_someone_was_home():
    rig = Rig()
    rig.open()
    rig.evaluate(occupied=True)
    rig.clock.advance(1)
    assert rig.evaluate(occupied=None) == []
    rig.clock.advance(1)
    assert kinds(rig.evaluate(occupied=False)) == ["left_open_away"]


def _fill_cap(rig):
    for _ in range(6):
        rig.open()
        rig.clock.advance(1)
        rig.deliver()
        rig.model.note_command("close")
        rig.close()
        rig.deliver()
        rig.clock.advance(1)


def test_dropped_overflow_alert_is_not_offered_again_when_the_hour_passes():
    rig = Rig(GarageSettings(left_open_minutes=1))
    _fill_cap(rig)
    rig.open()
    rig.clock.advance(1)
    for alert in rig.evaluate():
        rig.rules.mark_sent(alert)
    rig.clock.advance(120)
    assert "left_open" not in kinds(rig.evaluate())


def test_paused_note_does_not_use_up_a_cap_slot():
    rig = Rig(GarageSettings(left_open_minutes=1))
    _fill_cap(rig)
    rig.open()
    rig.clock.advance(1)
    for alert in rig.evaluate():
        rig.rules.mark_sent(alert)
    rig.model.note_command("close")
    rig.close()
    rig.clock.advance(47.5)
    rig.open()
    rig.clock.advance(1)
    # the first of the six sent alerts is now over an hour old: one slot is free
    assert kinds(rig.evaluate()) == ["left_open"]


def test_mark_dropped_stops_an_alert_being_offered_again():
    rig = Rig()
    rig.open()
    rig.clock.advance(15)
    (alert,) = rig.evaluate()
    rig.rules.mark_dropped(alert)
    rig.clock.advance(1)
    assert rig.evaluate() == []


def test_remote_command_notes_do_not_count_against_the_cap():
    rig = Rig()
    for kind in ("remote_note", "remote_note_followup") * 5:
        rig.rules.mark_sent(Alert(kind, "t", "b", "default"))
    rig.open()
    rig.clock.advance(15)
    assert kinds(rig.evaluate()) == ["left_open"]
