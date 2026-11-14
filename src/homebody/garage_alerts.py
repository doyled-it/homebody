"""Garage alert rules: pure, with an injected clock.

``AlertRules.evaluate`` returns the alerts that are due and not yet sent. An
alert is only recorded as sent by ``mark_sent`` (after delivery succeeded), so
a failed send is offered again on the next pass. The kinds already sent for an
open episode live in ``Episode.fired`` and are saved with the model. Everything
else (flap window, outage flags, who was home when the door opened) is rebuilt
empty after a restart.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from homebody.garage import Episode, GarageModel, GarageSettings

log = logging.getLogger(__name__)

Priority = Literal["min", "low", "default", "high"]

KINDS = (
    "left_open",
    "left_open_reminder",
    "opened_away",
    "left_open_away",
    "closed_note",
    "link_offline",
    "link_back",
    "paused",
    "remote_note",
    "remote_note_followup",
)

EPISODE_KINDS = ("left_open", "left_open_reminder", "opened_away", "left_open_away")

REMINDER_AFTER_MINUTES = 60
LINK_OFFLINE_SECONDS = 10 * 60
FLAP_CAP = 6
FLAP_WINDOW_SECONDS = 3600.0
# An episode first seen later than this after it began is not judged for
# opened_away, since who was home at the moment it opened is unknown.
FIRST_SIGHT_SLACK_SECONDS = 120.0


@dataclass(frozen=True)
class Alert:
    kind: str
    title: str
    body: str
    priority: Literal["min", "low", "default", "high"]


def _minutes(seconds: float) -> int:
    return max(0, int(seconds // 60))


def _duration(seconds: float) -> str:
    minutes = _minutes(seconds)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours, rest = divmod(minutes, 60)
    text = f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{text} {rest} min" if rest else text


class _Tracked:
    """What the rules know about the episode in progress."""

    def __init__(self, episode: Episode, opened_away: bool) -> None:
        self.episode = episode
        self.opened_away = opened_away
        self.arrived = False


class AlertRules:
    def __init__(self, model: GarageModel, clock: Callable[[], float]) -> None:
        self._model = model
        self._clock = clock
        self._tracked: _Tracked | None = None
        self._closed: list[Episode] = []
        self._last_occupied: bool | None = None
        self._away_pending: Episode | None = None
        self._down_since: float | None = None
        self._offline_done = False
        self._offline_sent = False
        self._back_pending = False
        self._sent_times: list[float] = []
        self._paused_at: float | None = None
        self._paused_pending = False
        # what each offered alert refers to, so a late mark_sent finds it
        self._offered: dict[str, object] = {}

    # tracking

    def _age(self, episode: Episode, now: float) -> float:
        return max(0.0, now - episode.started)

    def _track(self, occupied: bool | None, now: float) -> None:
        episode = self._model.episode
        tracked = self._tracked
        if tracked is not None and tracked.episode is not episode:
            old = tracked.episode
            if old.fired and not old.close_commanded and "closed_note" not in old.fired:
                self._closed.append(old)
            if self._away_pending is old:
                self._away_pending = None
            self._tracked = tracked = None
        if episode is not None and tracked is None:
            opened_away = (
                occupied is False
                and not episode.since_approximate
                and not episode.remote_started
                and self._age(episode, now) <= FIRST_SIGHT_SLACK_SECONDS
            )
            self._tracked = tracked = _Tracked(episode, opened_away)
        if tracked is not None and occupied is True:
            tracked.arrived = True
        if occupied is not None:
            if (
                self._last_occupied is True
                and occupied is False
                and episode is not None
                and "left_open_away" not in episode.fired
            ):
                self._away_pending = episode
            if occupied is True:
                self._away_pending = None
            self._last_occupied = occupied

    # candidates

    def _candidates(
        self, occupied: bool | None, settings: GarageSettings, now: float
    ) -> list[tuple[Alert, object]]:
        out: list[tuple[Alert, object]] = []
        episode = self._model.episode
        tracked = self._tracked
        if episode is not None and tracked is not None:
            age = self._age(episode, now)
            away_priority: Priority = "high" if occupied is False else "default"
            open_for = _duration(age)
            if (
                self._away_pending is episode
                and "left_open_away" not in episode.fired
            ):
                out.append(
                    (
                        Alert(
                            "left_open_away",
                            "Garage door left open",
                            f"Everyone has left and the garage door has been open "
                            f"for {open_for}.",
                            "high",
                        ),
                        episode,
                    )
                )
            if (
                tracked.opened_away
                and not tracked.arrived
                and occupied is False
                and "opened_away" not in episode.fired
                and age >= settings.away_open_grace_minutes * 60
            ):
                out.append(
                    (
                        Alert(
                            "opened_away",
                            "Garage door opened while away",
                            f"The garage door opened while nobody was home and has "
                            f"been open for {open_for}.",
                            "high",
                        ),
                        episode,
                    )
                )
            n = settings.left_open_minutes
            if n > 0:
                folded = any(a.kind == "left_open_away" for a, _ in out)
                if (
                    not folded
                    and "left_open" not in episode.fired
                    and age >= n * 60
                ):
                    out.append(
                        (
                            Alert(
                                "left_open",
                                "Garage door left open",
                                f"The garage door has been open for {open_for}.",
                                away_priority,
                            ),
                            episode,
                        )
                    )
                if (
                    ("left_open" in episode.fired or "left_open_away" in episode.fired)
                    and "left_open_reminder" not in episode.fired
                    and age >= (n + REMINDER_AFTER_MINUTES) * 60
                ):
                    out.append(
                        (
                            Alert(
                                "left_open_reminder",
                                "Garage door still open",
                                f"The garage door is still open after {open_for}.",
                                away_priority,
                            ),
                            episode,
                        )
                    )
        if self._closed:
            out.append(
                (
                    Alert(
                        "closed_note",
                        "Garage door closed",
                        "The garage door is closed again.",
                        "low",
                    ),
                    self._closed[0],
                )
            )
        out.extend(self._link_candidates(now))
        return out

    def _link_candidates(self, now: float) -> list[tuple[Alert, object]]:
        out: list[tuple[Alert, object]] = []
        if self._model.connected:
            self._down_since = None
            if self._offline_sent:
                self._back_pending = True
            self._offline_sent = False
            self._offline_done = False
        else:
            if self._down_since is None or self._down_since > now:
                self._down_since = now
            down = now - self._down_since
            if not self._offline_done and down >= LINK_OFFLINE_SECONDS:
                out.append(
                    (
                        Alert(
                            "link_offline",
                            "Garage board offline",
                            f"The garage board has not answered for "
                            f"{_duration(down)}. The door state is unknown.",
                            "default",
                        ),
                        "link_offline",
                    )
                )
        if self._back_pending and self._model.connected:
            out.append(
                (
                    Alert(
                        "link_back",
                        "Garage board back online",
                        "The garage board is reachable again.",
                        "low",
                    ),
                    "link_back",
                )
            )
        return out

    # public API

    def evaluate(
        self, occupied: bool | None, settings: GarageSettings, ntfy_configured: bool
    ) -> list[Alert]:
        now = self._clock()
        self._track(occupied, now)
        if not settings.alerts or not ntfy_configured:
            self._closed.clear()
            self._away_pending = None
            self._back_pending = False
            self._offered.clear()
            return []
        candidates = self._candidates(occupied, settings, now)
        self._sent_times = [
            t for t in self._sent_times if 0 <= now - t < FLAP_WINDOW_SECONDS
        ]
        paused_age = None if self._paused_at is None else now - self._paused_at
        if paused_age is not None and not 0 <= paused_age < FLAP_WINDOW_SECONDS:
            self._paused_at = None
        slots = max(0, FLAP_CAP - len(self._sent_times))
        offered = candidates[:slots]
        overflow = candidates[slots:]
        result: list[Alert] = []
        if overflow and self._paused_at is None:
            self._paused_pending = True
        for alert, target in overflow:
            self._offered[alert.kind] = target
            self.mark_dropped(alert)
        if self._paused_pending and self._paused_at is None:
            paused = Alert(
                "paused",
                "Garage alerts paused",
                "Garage alerts are paused for the hour after too many in a row.",
                "low",
            )
            self._offered["paused"] = "paused"
            result.append(paused)
        for alert, target in offered:
            self._offered[alert.kind] = target
            result.append(alert)
        return result

    def mark_sent(self, alert: Alert) -> None:
        now = self._clock()
        target = self._offered.pop(alert.kind, None)
        if alert.kind == "paused":
            self._paused_at = now
            self._paused_pending = False
            return
        self._sent_times.append(now)
        self._apply(alert, target, sent=True)

    def mark_dropped(self, alert: Alert) -> None:
        target = self._offered.pop(alert.kind, None)
        log.warning("garage alert %s dropped", alert.kind)
        if alert.kind == "paused":
            self._paused_pending = False
            self._paused_at = self._clock()
            return
        self._apply(alert, target, sent=False)

    def _apply(self, alert: Alert, target: object, *, sent: bool) -> None:
        kind = alert.kind
        if kind in EPISODE_KINDS and isinstance(target, Episode):
            target.fired.add(kind)
            if kind == "left_open_away":
                target.fired.add("left_open")
            if self._away_pending is target:
                self._away_pending = None
        elif kind == "closed_note":
            if self._closed:
                done = self._closed.pop(0)
                done.fired.add("closed_note")
        elif kind == "link_offline":
            self._offline_done = True
            self._offline_sent = sent
        elif kind == "link_back":
            self._back_pending = False
