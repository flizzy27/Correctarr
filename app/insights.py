"""What has happened, over days rather than over one pass.

Everything here is read from what the store already keeps: the findings log,
the actions the safety guards count, the record of which findings a pass still
comes across, and the pauses of the fuse. Nothing is fetched from a service,
so the answer costs the same whether the services are there or not.

Two definitions carry most of the weight:

``findings`` on a day
    How many different findings were written down that day. Not rows: a
    problem that lasts is written down again every time it becomes news, and
    counting rows would count the same stuck download twice a day for a week.

resolved
    A finding that no pass comes across any more. Whether it was fixed here,
    by hand in the service or by the service itself does not matter — it is
    gone. The time it took is from the first time it was written down to the
    last time a pass saw it.
"""
from __future__ import annotations

import os
import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .engine import identity
from .i18n import t
from .storage import ACTIONS_KEPT_DAYS

#: As far back as anything is kept to answer from.
MOST_DAYS = ACTIONS_KEPT_DAYS

#: How many rules the ranking names.
TOP_RULES = 10

#: The most log rows read for one answer. The log is trimmed to twenty thousand
#: rows by default; this leaves room for somebody who keeps more.
SCAN = 200_000


def _moment(value) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def local_zone():
    """The time zone days are counted in: ``TZ``, as the schedule reads it.

    Not the offset of this moment: that is one fixed number, and looked at in
    November, whatever happened between midnight and one on a summer night was
    counted on the day before. ``None`` — the system's own zone, changes of the
    clocks included — when ``TZ`` is not set or not known.
    """
    name = os.getenv("TZ")
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def window(days: int, now: datetime | None = None, zone=None) -> tuple[int, datetime, list[str]]:
    """``(days, start, every date)`` for the last ``days`` calendar days.

    Whole days in the local time zone, today included, because a chart of
    "the last thirty days" whose bars start at a quarter past three in the
    afternoon reads as if the first one were half empty.
    """
    days = max(1, min(MOST_DAYS, int(days)))
    now = now or datetime.now(UTC)
    zone = zone or local_zone()
    today = now.astimezone(zone).date()
    first = today - timedelta(days=days - 1)
    start = datetime.combine(first, time.min, tzinfo=zone).astimezone(UTC)
    return days, start, [(first + timedelta(days=n)).isoformat() for n in range(days)]


def summarise(store, days: int, current: set[str], *, language: str = "en",
              now: datetime | None = None, zone=None) -> dict:
    """Counts per day, the rules behind them, and how long problems stayed.

    ``current`` is the set of findings a pass still comes across — the same
    set that decides what is waiting for somebody, so that "resolved" here and
    "no longer waiting" there never disagree.
    """
    now = now or datetime.now(UTC)
    zone = zone or local_zone()
    days, start, dates = window(days, now, zone)

    def day_of(moment: datetime) -> str:
        return moment.astimezone(zone).date().isoformat()

    daily = {d: {"date": d, "findings": set(), "automatic": set(),
                 "by_hand": set(), "failed": set(), "freed_mb": {}}
             for d in dates}

    # The log from twice as far back as asked for, so that a finding which
    # began before the window still has its real beginning.
    first_at: dict[str, datetime] = {}
    last_row: dict[str, datetime] = {}
    found_by_rule: dict[str, set[str]] = defaultdict(set)
    rows = store.findings(limit=SCAN, since=(start - timedelta(days=days)).isoformat())
    for row in rows:
        moment = _moment(row.get("at"))
        if moment is None:
            continue
        key, _dismiss = identity(row)
        if key not in first_at or moment < first_at[key]:
            first_at[key] = moment
        if key not in last_row or moment > last_row[key]:
            last_row[key] = moment
        bucket = daily.get(day_of(moment)) if moment >= start else None
        if bucket is not None:
            bucket["findings"].add(key)
            found_by_rule[str(row.get("rule") or "")].add(key)

    # Actions, one per distinct thing done: a season pack thrown out is listed
    # once per episode and is still one action — the same reckoning the fuse
    # uses.
    acted_by_rule: dict[str, set[str]] = defaultdict(set)
    failed_by_rule: dict[str, set[str]] = defaultdict(set)
    for act in store.acts_since(start):
        moment = _moment(act.get("at"))
        if moment is None:
            continue
        bucket = daily.get(day_of(moment))
        if bucket is None:
            continue
        one = act.get("identity") or f"row:{act.get('id')}"
        bucket["by_hand" if act.get("by_hand") else "automatic"].add(one)
        rule = str(act.get("rule") or "")
        acted_by_rule[rule].add(one)
        if act.get("state") == "failed":
            bucket["failed"].add(one)
            failed_by_rule[rule].add(one)
        freed = float(act.get("freed_mb") or 0)
        if freed > 0:
            bucket["freed_mb"][one] = max(freed, bucket["freed_mb"].get(one, 0.0))

    out_days = [{
        "date": bucket["date"],
        "findings": len(bucket["findings"]),
        "automatic": len(bucket["automatic"]),
        "by_hand": len(bucket["by_hand"]),
        "failed": len(bucket["failed"]),
        "freed_gb": round(sum(bucket["freed_mb"].values()) / 1024, 2),
    } for bucket in daily.values()]

    seen = store.last_seen()
    hours = []
    for key, began in first_at.items():
        if key in current:
            continue
        ended = _moment(seen.get(key)) or last_row.get(key)
        if ended is None or ended < start:
            continue
        hours.append(max(0.0, (ended - began).total_seconds() / 3600))

    ranked = Counter({rule: len(keys) for rule, keys in found_by_rule.items()})
    for rule in acted_by_rule:
        ranked.setdefault(rule, 0)
    top = sorted(ranked, key=lambda r: (-ranked[r], -len(acted_by_rule.get(r, ())), r))

    return {
        "days": days,
        "since": start.isoformat(),
        "until": now.isoformat(),
        "daily": out_days,
        "totals": {
            "findings": len(set().union(*found_by_rule.values())),
            "automatic": sum(d["automatic"] for d in out_days),
            "by_hand": sum(d["by_hand"] for d in out_days),
            "failed": sum(d["failed"] for d in out_days),
            "freed_gb": round(sum(d["freed_gb"] for d in out_days), 2),
        },
        "top_rules": [{
            "rule": rule,
            "title": t(f"rules.{rule}.title", language),
            "findings": ranked[rule],
            "actions": len(acted_by_rule.get(rule, ())),
            "failed": len(failed_by_rule.get(rule, ())),
        } for rule in top[:TOP_RULES]],
        "resolution": {
            "resolved": len(hours),
            "mean_hours": round(statistics.fmean(hours), 1) if hours else None,
            "median_hours": round(statistics.median(hours), 1) if hours else None,
        },
        "pauses": [{
            "at": pause.get("at"),
            "resumed": pause.get("resumed"),
            "reason_key": pause.get("reason"),
            "reason": t(str(pause.get("reason") or ""), language,
                        **(pause.get("params") or {})),
        } for pause in store.pauses(start)],
    }
