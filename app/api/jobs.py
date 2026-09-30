"""What starts a pass or sends something without anybody pressing a button.

Work happens three ways:

  1. **Event** — Radarr and Sonarr report through a webhook the moment they grab
     something or need manual work. Reaction within seconds.
  2. **Fast** — a short interval for the queue rules.
  3. **Deep** — less often, additionally walking the library and the indexers.

The scheduled summary runs here as well, on a clock of its own.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import UTC, datetime

from apscheduler.schedulers.background import BackgroundScheduler

from .. import digest, i18n
from . import core
from .core import log
from .findings import Decisions, open_rows

scheduler = BackgroundScheduler(timezone=os.getenv("TZ", "Etc/UTC"))

_event_lock = threading.Lock()
_last_event = 0.0
#: A pass already promised to the events that arrived since the last one.
_event_pending = False


def schedule() -> None:
    cfg = core.engine.config()
    scheduler.add_job(lambda: core.engine.run(deep=False), "interval",
                      seconds=max(20, int(cfg.get("fast_seconds", 60))),
                      id="fast", replace_existing=True, max_instances=1,
                      coalesce=True, misfire_grace_time=30)
    scheduler.add_job(lambda: core.engine.run(deep=True), "interval",
                      minutes=max(5, int(cfg.get("deep_minutes", 90))),
                      id="deep", replace_existing=True, max_instances=1,
                      coalesce=True, misfire_grace_time=300)
    log.info("Schedule: fast every %ss, deep every %s minutes",
             cfg.get("fast_seconds"), cfg.get("deep_minutes"))
    period = cfg.get("digest", "off")
    if period in digest.PERIODS:
        when = {"hour": int(cfg.get("digest_hour", 8)), "minute": 0}
        if period == "weekly":
            when["day_of_week"] = str(cfg.get("digest_day", "monday"))[:3]
        scheduler.add_job(_send_digest, "cron", id="digest", replace_existing=True,
                          max_instances=1, coalesce=True, misfire_grace_time=3600,
                          **when)
        log.info("Summary: %s at %s:00", period, when["hour"])
    elif scheduler.get_job("digest"):
        scheduler.remove_job("digest")


def trigger_event(source: str) -> None:
    """Start a fast pass soon — at most one per gap, however many events come.

    A search wave makes the service send a grab event every two seconds,
    fifteen in a row. The first one starts a pass straight away. Everything
    that arrives inside the gap after it is folded into **one** further pass
    at the end of the gap, rather than dropped: the last grab of a wave is the
    one most likely to need looking at, and dropping it left it to the next
    scheduled pass a minute later.

    Never two passes at once, and never a pile of threads waiting for one:
    there is at most one pass promised at any time, and the run itself refuses
    to start while another is going.
    """
    global _last_event, _event_pending
    cfg = core.engine.config()
    if not cfg.get("events_enabled", True):
        return
    gap = float(cfg.get("event_debounce", 8))
    with _event_lock:
        if _event_pending:
            log.debug("Event from %s folded into the pass already due", source)
            return
        _event_pending = True
        wait = max(0.0, _last_event + gap - time.monotonic())
    if wait:
        log.info("Event from %s — checking in %.0f s", source, wait)
    else:
        log.info("Event from %s — checking now", source)
    timer = threading.Timer(wait, _event_pass, args=(source,))
    timer.daemon = True
    timer.start()


def _event_pass(source: str) -> None:
    global _last_event, _event_pending
    with _event_lock:
        _event_pending = False
        _last_event = time.monotonic()
    result = core.engine.run(deep=False, trigger=source)
    # Another pass was already going and may have read the queue before the
    # event arrived. One more after the gap — still never more than one due.
    if isinstance(result, dict) and result.get("skipped"):
        trigger_event(source)


# ---------------------------------------------------------------------------
# The summary
# ---------------------------------------------------------------------------
def summary(language: str, period: str) -> digest.Summary | None:
    rows, _dismissed = open_rows(Decisions())
    return digest.compose(core.store, period=period, waiting=rows,
                          paused=core.engine.guard().paused(), language=language,
                          url=core.engine.config().get("public_url", ""))


def digest_language(cfg: dict) -> str:
    chosen = cfg.get("language")
    return chosen if chosen in i18n.AVAILABLE else i18n.DEFAULT


def _send_digest() -> None:
    """The scheduled summary. Skipped when there is nothing to say."""
    cfg = core.engine.config()
    period = cfg.get("digest", "off")
    if not digest.due(core.store, period):
        return
    found = summary(digest_language(cfg), period)
    if found is None:
        log.info("Nothing happened worth a summary")
        return
    outcomes = digest.send(core.engine.notification_targets(), found)
    if any(o["ok"] for o in outcomes):
        core.store.set(digest.SENT_KEY, datetime.now(UTC).isoformat())
