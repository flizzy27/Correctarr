"""The last layer: nothing automatic may happen without limit.

Every rule has its own conditions, and the engine already waits after a
failure. Neither of those is enough on its own, and this is the case that
showed it. An episode was flagged by the year check, removed, blocklisted and
searched for. The service grabbed another copy within seconds, the year check
flagged that one too, and round it went — eight times in four minutes, until
somebody noticed. The failure backoff did not catch it, because every new grab
is a new release, and so as far as the backoff was concerned a new finding.

So three guards sit in front of every automatic action, whichever rule asks
for it and whatever it is:

**Per title.** The same title — a film, an episode, a profile, a folder — is
acted on at most ``safety_title_limit`` times in ``safety_title_hours``,
counted across every rule and every kind of action. Past that the finding is
held and reported: something is going round in a circle and it needs a person.
A button pressed by a person is never blocked, but it is counted.

**Re-grab.** When a rule has thrown something out — removed, blocklisted,
deleted — and the same rule then flags the same title again with a different
release, the service has grabbed a replacement that is no better. Acting a
second time is how the loop above started. The title is held for the whole
window instead, for every rule.

**The fuse.** More than ``safety_hourly_limit`` automatic actions in an hour,
or more than ``safety_discard_limit`` that throw something away, and every
automatic action stops until a person says otherwise. The state is kept in
the store, so a restart does not reset it, and a notification goes out the
moment it trips.

Every limit is checked *before* acting, so none of them is ever exceeded.

What counts is an action that was attempted, not one that succeeded. The
live case above was recorded as a failure every single time — the service
answered that the queue entry did not exist — while its own history showed
the release being marked as failed at the same second. A failure is no proof
that nothing happened.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from . import policy

log = logging.getLogger(__name__)

#: Where the fuse keeps its state in the settings table.
PAUSE_KEY = "safety_pause"
RESUMED_KEY = "safety_resumed"

#: What the store writes when a title is held because it was grabbed again.
#: Not an action, so it is never counted as one — but while it is inside the
#: window, the title is left alone.
LOOP = "loop"

#: The window the fuse counts in.
FUSE_HOURS = 1.0

#: What the notification about a tripped fuse is filed under. Not a rule: it
#: goes out through the same channels, grouped under a heading of its own.
NOTICE_RULE = "safety"


@dataclass(frozen=True)
class Limits:
    """The thresholds, read tolerantly from the configuration.

    A value that cannot be read falls back to the default rather than to
    "unlimited" — the failure mode of this, like of every policy here, is
    "does less than expected", never "does more".
    """
    per_title: int = 3
    window_hours: float = 24.0
    per_hour: int = 60
    discards_per_hour: int = 30

    @classmethod
    def from_config(cls, cfg: dict) -> Limits:
        base = cls()

        def read(key: str, fallback: float) -> float:
            try:
                value = float(cfg.get(key, fallback))
            except (TypeError, ValueError):
                return fallback
            return value if value >= 1 else fallback

        return cls(per_title=int(read("safety_title_limit", base.per_title)),
                   window_hours=read("safety_title_hours", base.window_hours),
                   per_hour=int(read("safety_hourly_limit", base.per_hour)),
                   discards_per_hour=int(read("safety_discard_limit",
                                              base.discards_per_hour)))


@dataclass(frozen=True)
class Hold:
    """Why an automatic action is not going ahead."""
    reason: str
    params: dict = field(default_factory=dict)
    #: Set when this very check tripped the fuse, so the engine announces it
    #: once rather than on every finding that is held afterwards.
    tripped: bool = False


# ---------------------------------------------------------------------------
# What a finding is about
# ---------------------------------------------------------------------------
def subject(finding, entry: dict | None = None) -> str:
    """The thing a finding is about, as one string.

    A film or a series by its id on the service it came from — and on Sonarr
    the episode as well where it is known, because a season of twenty-two
    episodes each needing an import is not one title acted on twenty-two
    times. Without an item: the profile, the path, the release, the download
    client entry. Without any of those, the finding itself.

    ``entry`` is the queue entry the finding was made from, when there is one.
    Most queue rules do not copy the item id into their findings; the queue
    entry carries it anyway, and reading it here keeps every rule covered
    without each of them having to remember.
    """
    data = finding.data or {}
    entry = entry or {}
    service = str(finding.service or "")
    where = data.get("_instance")
    where = where if where is not None else service

    item = data.get("item_id") or entry.get("movieId") or entry.get("seriesId")
    if not item:
        item = ((entry.get("movie") or entry.get("series") or {}).get("id"))
    if item:
        key = f"{service}:{where}:item:{item}"
        episode = entry.get("episodeId")
        if not episode:
            episodes = data.get("episode_ids") or []
            episode = episodes[0] if len(episodes) == 1 else None
        if episode:
            key += f":episode:{episode}"
        elif data.get("season") is not None:
            key += f":season:{data['season']}"
        return key
    if data.get("profile_id"):
        return f"{service}:{where}:profile:{data['profile_id']}"
    for name in ("path", "release", "nzo_id", "file"):
        if data.get(name):
            return f"{service}:{name}:{data[name]}"
    return f"{service}:{finding.rule}:{finding.title}"


def identity(finding) -> str:
    """What tells one grab of a title from the next: release and download.

    The download rather than the queue entry where it is known, because a
    season pack is one download listed as one queue entry per episode.
    Throwing it out is one action, not twenty-two, and the fuse counts it once.
    """
    data = finding.data or {}
    release = str(data.get("release") or "")
    where = data.get("_download") or finding.entry_id
    if not release and where is None:
        return ""
    return f"{release}|{where if where is not None else ''}"


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------
class Guard:
    """Decides whether one more automatic action is allowed, and keeps count."""

    def __init__(self, store, limits: Limits | None = None):
        self.store = store
        self.limits = limits or Limits()

    # -- the fuse --------------------------------------------------------------
    def paused(self) -> dict | None:
        """The fuse's state when it has tripped, otherwise ``None``."""
        state = self.store.get(PAUSE_KEY)
        return state if isinstance(state, dict) and state.get("reason") else None

    def pause(self, reason: str, params: dict | None = None) -> dict:
        state = {"since": datetime.now(UTC).isoformat(), "reason": reason,
                 "params": dict(params or {})}
        self.store.set(PAUSE_KEY, state)
        log.warning("Automatic actions paused: %s %s", reason, state["params"])
        return state

    def resume(self) -> None:
        """Let automatic actions run again.

        The count starts afresh from here. Otherwise the actions that tripped
        the fuse would still be inside the hour, and the very next one would
        trip it again before the person who pressed the button had let go.
        """
        self.store.set(PAUSE_KEY, None)
        self.store.set(RESUMED_KEY, datetime.now(UTC).isoformat())
        log.info("Automatic actions resumed by hand")

    def _fuse_since(self, now: datetime) -> datetime:
        since = now - timedelta(hours=FUSE_HOURS)
        resumed = _moment(self.store.get(RESUMED_KEY))
        return max(since, resumed) if resumed else since

    # -- deciding --------------------------------------------------------------
    def check(self, finding, action: str) -> Hold | None:
        """``None`` when the action may go ahead, otherwise why not."""
        if self.paused():
            return Hold("policy.fuse_paused")

        now = datetime.now(UTC)
        hours = self.limits.window_hours
        subj = (finding.data or {}).get("_subject") or subject(finding)
        rows = self.store.acts_on(subj, now - timedelta(hours=hours))

        if any(row["action"] == LOOP for row in rows):
            return Hold("policy.regrabbed", {"hours": _tidy(hours)})

        # Thrown out by this rule before, and flagged by it again with
        # something else in its place: the replacement is no better, and a
        # second round is how a loop starts.
        mine = identity(finding)
        again = [row for row in rows
                 if row["rule"] == finding.rule
                 and row["action"] in policy.DISCARDS
                 and row["identity"] != mine]
        if again:
            self.store.note_act(subj, finding.rule, LOOP, mine)
            log.warning("%s flagged %s again after throwing it out — holding "
                        "it for %s h", finding.rule, subj, _tidy(hours))
            return Hold("policy.regrabbed", {"hours": _tidy(hours)})

        acted = [row for row in rows if row["action"] != LOOP]
        if len(acted) >= self.limits.per_title:
            return Hold("policy.acted_too_often",
                        {"count": len(acted), "hours": _tidy(hours)})

        since = self._fuse_since(now)
        total = self.store.count_acts(since)
        if total >= self.limits.per_hour:
            self.pause("safety.too_many",
                       {"count": total, "limit": self.limits.per_hour})
            return Hold("policy.fuse_paused", tripped=True)
        if action in policy.DISCARDS:
            thrown = self.store.count_acts(since, policy.DISCARDS)
            if thrown >= self.limits.discards_per_hour:
                self.pause("safety.too_many_discards",
                           {"count": thrown,
                            "limit": self.limits.discards_per_hour})
                return Hold("policy.fuse_paused", tripped=True)
        return None

    def note(self, finding, action: str, *, by_hand: bool = False) -> None:
        """Count one action that was attempted. Never lets a run fail."""
        try:
            subj = (finding.data or {}).get("_subject") or subject(finding)
            self.store.note_act(subj, finding.rule, action, identity(finding),
                                by_hand=by_hand)
        except Exception:                                       # noqa: BLE001
            log.exception("Could not count the action on %s", finding.rule)


def _moment(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _tidy(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"
