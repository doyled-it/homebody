"""The command gate: nonce, origin rules, state checks, pacing, confirmation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from garage_fakes import FakeGarageLink
from homebody.audit import AuditLog
from homebody.garage import GarageModel, GarageSettings
from homebody.garage_alerts import Alert
from homebody.garage_commands import CommandError, CommandGate


class Clock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make(
    tmp_path,
    *,
    door="closed",
    remote=True,
    ntfy=True,
    note_result=True,
    note_delay=0.0,
    close_wait=0.05,
    open_wait=0.05,
    note_wait=0.05,
    connect=True,
):
    clock = Clock()
    link = FakeGarageLink()
    model = GarageModel(clock)
    link.bind(model.state_received, model.link_changed)
    order: list[str] = []
    alerts: list[Alert] = []
    real_send = link.send

    async def send(action):
        order.append(f"send:{action}")
        await real_send(action)

    link.send = send  # type: ignore[method-assign]

    async def notify(alert: Alert) -> bool:
        order.append(f"note:{alert.kind}")
        if note_delay:
            await asyncio.sleep(note_delay)
        alerts.append(alert)
        return note_result

    audit = AuditLog(tmp_path, clock)
    settings = GarageSettings(remote=remote)
    gate = CommandGate(
        model=model,
        link=link,
        audit=audit,
        notify=notify if ntfy else None,
        settings=lambda: settings,
        clock=clock,
        close_wait=close_wait,
        open_wait=open_wait,
        note_wait=note_wait,
    )
    if connect:
        link.connect()
        link.push(operation="idle", position=0.0 if door == "closed" else 1.0)
    return SimpleNamespace(
        clock=clock, link=link, model=model, gate=gate, audit=audit, order=order,
        alerts=alerts,
    )


def run(coro):
    return asyncio.run(coro)


def go(env, action, origin="home", actor=None):
    nonce = env.gate.intent(action, origin, actor)["nonce"]
    return run(env.gate.command(action, nonce, origin, actor))


_FRESH = object()


def refusal(env, action, origin="home", actor=None, nonce=_FRESH):
    if nonce is _FRESH:
        nonce = env.gate.intent(action, origin, actor)["nonce"]
    with pytest.raises(CommandError) as info:
        run(env.gate.command(action, nonce, origin, actor))
    return info.value


def log_lines(env):
    import json

    return [json.loads(x) for x in env.audit.path.read_text().splitlines()]


def results(env):
    return [line["result"] for line in log_lines(env)]


def test_intent_returns_nonce_30s_and_confirm_required_only_for_tunnel(tmp_path):
    env = make(tmp_path)
    home = env.gate.intent("open", "home", None)
    tunnel = env.gate.intent("open", "tunnel", "ab12")
    assert home["expires_in"] == 30 and tunnel["expires_in"] == 30
    assert home["confirm_required"] is False
    assert tunnel["confirm_required"] is True
    assert home["nonce"] != tunnel["nonce"] and len(home["nonce"]) >= 16


def test_command_without_valid_nonce_is_400_and_moves_nothing(tmp_path):
    env = make(tmp_path)
    for bad in ("", "nope", None, 7):
        err = refusal(env, "open", nonce=bad)
        assert err.status == 400
        assert err.message == "That request expired. Start again."
    assert env.link.sent == []


def test_nonce_is_single_use_and_bound_to_its_action(tmp_path):
    env = make(tmp_path)
    wrong = env.gate.intent("open", "home", None)["nonce"]
    assert refusal(env, "close", nonce=wrong).status == 400
    assert refusal(env, "open", nonce=wrong).status == 400
    nonce = env.gate.intent("open", "home", None)["nonce"]
    assert run(env.gate.command("open", nonce, "home", None)) == "sent"
    assert refusal(env, "open", nonce=nonce).status == 400
    assert env.link.sent == ["open"]


def test_nonce_expires_after_30_seconds(tmp_path):
    env = make(tmp_path)
    nonce = env.gate.intent("open", "home", None)["nonce"]
    env.clock.now += 30
    assert refusal(env, "open", nonce=nonce).status == 400
    nonce = env.gate.intent("open", "home", None)["nonce"]
    env.clock.now += 29
    assert run(env.gate.command("open", nonce, "home", None)) == "sent"


def test_tunnel_refused_403_when_remote_off(tmp_path):
    env = make(tmp_path, remote=False)
    assert refusal(env, "open", "tunnel", "ab12").status == 403
    assert env.link.sent == [] and env.alerts == []
    assert go(env, "open", "home") == "sent"


def test_tunnel_refused_409_when_ntfy_not_configured(tmp_path):
    env = make(tmp_path, ntfy=False)
    err = refusal(env, "open", "tunnel", "ab12")
    assert err.status == 409
    assert "notification" in err.message.lower()
    assert env.link.sent == []


def test_unknown_state_503_when_link_down_never_sends(tmp_path):
    env = make(tmp_path, connect=False)
    assert refusal(env, "open").status == 503
    env.link.connect()
    err = refusal(env, "open")
    assert err.status == 409
    assert env.link.sent == []


def test_lockout_423(tmp_path):
    env = make(tmp_path)
    env.link.push(lockout=True)
    err = refusal(env, "open")
    assert err.status == 423
    assert "lock" in err.message.lower()
    assert env.link.sent == []


def test_close_with_obstruction_409_open_allowed(tmp_path):
    env = make(tmp_path, door="open")
    env.link.push(obstruction=True)
    assert refusal(env, "close").status == 409
    assert env.link.sent == []
    env2 = make(tmp_path / "b", door="closed")
    env2.link.push(obstruction=True)
    assert go(env2, "open") == "sent"


def test_open_when_already_open_is_noop_and_sends_nothing(tmp_path):
    env = make(tmp_path, door="open")
    assert go(env, "open") == "noop"
    assert env.link.sent == []
    env.clock.now += 1
    assert go(env, "open") == "noop"
    assert "noop" in results(env)


def test_noop_does_not_spend_the_rate_limit(tmp_path):
    env = make(tmp_path, door="open")
    assert go(env, "open") == "noop"
    assert go(env, "close") == "sent"


def test_double_tap_second_is_noop_or_409(tmp_path):
    env = make(tmp_path)
    assert go(env, "open") == "sent"
    env.clock.now += 1
    assert go(env, "open") == "noop"
    assert env.link.sent == ["open"]
    env.clock.now += 1
    assert refusal(env, "close").status == 409
    assert env.link.sent == ["open"]


def test_rate_limit_429_10s_and_tunnel_6_per_hour(tmp_path):
    env = make(tmp_path)
    assert go(env, "open") == "sent"
    env.link.settle()
    env.clock.now += 5
    assert refusal(env, "close").status == 429
    env.clock.now += 6
    assert go(env, "close") == "sent"
    assert "refused: rate limited" in results(env)

    env = make(tmp_path / "t", note_wait=0.01)
    action = "open"
    for _ in range(6):
        assert go(env, action, "tunnel", "ab12") == "sent"
        env.link.settle()
        env.clock.now += 11
        action = "close" if action == "open" else "open"
    err = refusal(env, action, "tunnel", "ab12")
    assert err.status == 429
    sent = list(env.link.sent)
    env.clock.now += 3600
    assert go(env, action, "tunnel", "ab12") == "sent"
    assert len(env.link.sent) == len(sent) + 1


def test_tunnel_limit_does_not_apply_to_home(tmp_path):
    env = make(tmp_path)
    action = "open"
    for _ in range(8):
        assert go(env, action, "home") == "sent"
        env.link.settle()
        env.clock.now += 11
        action = "close" if action == "open" else "open"


def test_tunnel_note_sent_before_board_command(tmp_path):
    env = make(tmp_path, note_delay=0.02)
    assert go(env, "open", "tunnel", "ab12") == "sent"
    assert env.order == ["note:remote_note", "send:open"]
    assert env.alerts[0].title == "Garage open requested from away"
    assert env.alerts[0].kind == "remote_note"


def test_home_command_sends_no_note(tmp_path):
    env = make(tmp_path)
    go(env, "open", "home")
    assert env.order == ["send:open"]


def test_slow_note_does_not_hold_the_command_past_note_wait(tmp_path):
    env = make(tmp_path, note_delay=0.5, note_wait=0.02)
    assert go(env, "open", "tunnel", "ab12") == "sent"
    assert env.order[:2] == ["note:remote_note", "send:open"]


def test_failed_note_still_sends_and_audits_note_failed(tmp_path):
    env = make(tmp_path, note_result=False)
    assert go(env, "open", "tunnel", "ab12") == "sent"
    assert env.link.sent == ["open"]
    assert "note failed" in results(env)


def test_no_confirmation_is_504_without_retry(tmp_path):
    env = make(tmp_path)
    env.link.swallow = True
    err = refusal(env, "open")
    assert err.status == 504
    assert err.message == "No movement seen yet. Check the card before trying again."
    assert env.link.sent == ["open"]
    assert "no confirmation" in results(env)


def test_tunnel_no_confirmation_sends_second_note(tmp_path):
    env = make(tmp_path)
    env.link.swallow = True
    assert refusal(env, "open", "tunnel", "ab12").status == 504
    assert [a.kind for a in env.alerts] == ["remote_note", "remote_note_followup"]
    assert env.link.sent == ["open"]


def test_late_confirm_is_audited(tmp_path):
    env = make(tmp_path)
    env.link.swallow = True
    assert refusal(env, "open", "tunnel", "ab12").status == 504
    env.clock.now += 20
    env.link.push(operation="opening")
    assert env.gate.attribute_change("opening") == ("tunnel", "ab12")
    assert "late confirm" in results(env)


def test_movement_after_60s_is_not_a_late_confirm(tmp_path):
    env = make(tmp_path)
    env.link.swallow = True
    assert refusal(env, "open", "tunnel", "ab12").status == 504
    env.clock.now += 61
    env.link.push(operation="opening")
    assert env.gate.attribute_change("opening") == ("opener", None)
    assert "late confirm" not in results(env)


def test_board_refusal_503_and_second_note_for_tunnel(tmp_path):
    env = make(tmp_path)
    env.link.fail_send = True
    err = refusal(env, "open", "tunnel", "ab12")
    assert err.status == 503
    assert [a.kind for a in env.alerts] == ["remote_note", "remote_note_followup"]
    env2 = make(tmp_path / "h")
    env2.link.fail_send = True
    assert refusal(env2, "open", "home").status == 503
    assert env2.alerts == []


def test_command_wait_does_not_block_other_coroutines(tmp_path):
    env = make(tmp_path, door="open", close_wait=0.3)
    env.link.swallow = True
    nonce = env.gate.intent("close", "home", None)["nonce"]
    done: list[str] = []

    async def other():
        await asyncio.sleep(0.02)
        done.append("other")

    async def main():
        task = asyncio.create_task(env.gate.command("close", nonce, "home", None))
        await other()
        assert not task.done()
        with pytest.raises(CommandError):
            await task
        done.append("command")

    asyncio.run(main())
    assert done == ["other", "command"]


def test_tunnel_commanded_open_marks_episode_remote_started(tmp_path):
    env = make(tmp_path)
    go(env, "open", "tunnel", "ab12")
    assert env.model.episode is not None
    assert env.model.episode.remote_started is True


def test_home_open_does_not_mark_remote_started(tmp_path):
    env = make(tmp_path)
    go(env, "open", "home")
    assert env.model.episode is not None
    assert env.model.episode.remote_started is False


def test_accepted_close_marks_the_episode_close_commanded(tmp_path):
    env = make(tmp_path, door="open")
    env.model.state_received(env.link.state)
    assert env.model.episode is not None
    go(env, "close", "home")
    assert env.model.episode is not None
    assert env.model.episode.close_commanded is True


def test_attribute_change_follows_the_command_then_falls_back_to_opener(tmp_path):
    env = make(tmp_path)
    go(env, "open", "tunnel", "ab12")
    assert env.gate.attribute_change("opening") == ("tunnel", "ab12")
    env.clock.now += 15
    assert env.gate.attribute_change("open") == ("tunnel", "ab12")
    env.clock.now += 600
    assert env.gate.attribute_change("closing") == ("opener", None)


def test_attribute_change_home_command_has_no_actor(tmp_path):
    env = make(tmp_path)
    go(env, "open", "home", None)
    assert env.gate.attribute_change("opening") == ("home", None)


def test_every_refusal_is_audited_with_its_reason(tmp_path):
    env = make(tmp_path, remote=False)
    refusal(env, "open", "tunnel", "ab12")
    refusal(env, "open", nonce="bad")
    assert results(env) == ["refused: remote off", "refused: bad nonce"]


def test_intent_rejects_an_unknown_action(tmp_path):
    env = make(tmp_path)
    with pytest.raises(CommandError) as info:
        env.gate.intent("toggle", "home", None)
    assert info.value.status == 400


# Fix round 1: a failed or unconfirmed command must never mark a later episode.


def _wall_open_while_away(env):
    """A wall-button open with nobody home, judged by the real alert rules."""
    from homebody.garage_alerts import AlertRules

    rules = AlertRules(env.model, env.clock)
    settings = GarageSettings(remote=True)
    env.link.fail_send = False
    env.link.swallow = False
    env.link.push(operation="opening")
    assert env.model.episode is not None
    assert rules.evaluate(False, settings, True) == []
    env.clock.now += 5 * 60
    return [a.kind for a in rules.evaluate(False, settings, True)]


def test_503_remote_open_then_wall_open_while_away_still_fires_opened_away(tmp_path):
    env = make(tmp_path)
    env.link.fail_send = True
    assert refusal(env, "open", "tunnel", "ab12").status == 503
    assert _wall_open_while_away(env) == ["opened_away"]


def test_504_remote_open_then_wall_open_while_away_still_fires_opened_away(tmp_path):
    env = make(tmp_path)
    env.link.swallow = True
    assert refusal(env, "open", "tunnel", "ab12").status == 504
    env.clock.now += 61
    assert _wall_open_while_away(env) == ["opened_away"]


def test_confirmed_remote_open_still_suppresses_opened_away(tmp_path):
    from homebody.garage_alerts import AlertRules

    env = make(tmp_path)
    go(env, "open", "tunnel", "ab12")
    rules = AlertRules(env.model, env.clock)
    settings = GarageSettings(remote=True)
    rules.evaluate(False, settings, True)
    env.clock.now += 5 * 60
    assert "opened_away" not in [a.kind for a in rules.evaluate(False, settings, True)]


def test_failed_or_unconfirmed_close_leaves_no_close_commanded_mark(tmp_path):
    env = make(tmp_path, door="open")
    env.model.state_received(env.link.state)
    env.link.fail_send = True
    assert refusal(env, "close", "home").status == 503
    assert env.model.episode.close_commanded is False
    env.link.fail_send = False
    env.link.swallow = True
    env.clock.now += 11
    assert refusal(env, "close", "home").status == 504
    assert env.model.episode.close_commanded is False


def test_failed_remote_open_on_a_stopped_door_leaves_the_episode_unmarked(tmp_path):
    env = make(tmp_path)
    env.link.push(operation="idle", position=0.5)
    assert env.model.episode is not None
    env.link.fail_send = True
    assert refusal(env, "open", "tunnel", "ab12").status == 503
    assert env.model.episode.remote_started is False


def test_late_confirm_also_matches_a_first_push_showing_the_end_state(tmp_path):
    env = make(tmp_path)
    env.link.swallow = True
    assert refusal(env, "open", "tunnel", "ab12").status == 504
    env.clock.now += 20
    assert env.gate.attribute_change("open") == ("tunnel", "ab12")
    assert "late confirm" in results(env)
    env2 = make(tmp_path / "c", door="open")
    env2.link.swallow = True
    assert refusal(env2, "close", "home").status == 504
    env2.clock.now += 20
    assert env2.gate.attribute_change("closed") == ("home", None)
    assert "late confirm" in results(env2)
