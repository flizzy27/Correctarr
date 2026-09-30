"""A summary now and then, on top of the report at the end of a pass.

The report of a pass says what that pass found. Nobody reads a week of them in
a row, and the questions a week raises are different ones: how much was dealt
with, what is still waiting for a person, and whether automatic actions were
paused at some point without anybody noticing.

Sent through the same connections as everything else and rendered by the same
channels. A summary is not a run, so its sections are headed by what they are
rather than by a rule, and its headline carries its numbers — both done by
extending the report's own shape here rather than by teaching every channel
about summaries.

Off by default. It is one more message, and nobody has asked for it yet.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from . import notifications, policy
from .engine import _from_row as finding_from_row
from .engine import identity
from .i18n import t
from .notifications import Group, Report
from .rules import Finding
from .safety import NOTICE_RULE

log = logging.getLogger(__name__)

#: How far back each kind of summary looks.
PERIODS = {"daily": 1, "weekly": 7}

#: When the last summary went out, in the settings table.
SENT_KEY = "digest_sent"

#: How many rows each section reads. The channels show a handful and count
#: the rest; this only has to be enough to count.
ROWS = 1000


@dataclass
class Section(Group):
    """A part of the summary, headed by what it holds rather than by a rule."""
    key: str = ""
    params: dict = field(default_factory=dict)

    def title(self, language: str) -> str:
        return t(self.key, language, **self.params)


@dataclass
class Summary(Report):
    """A report whose headline carries the numbers of the period."""
    numbers: dict = field(default_factory=dict)

    def headline(self) -> str:
        return t(self.headline_key, self.language, **self.numbers)


def _when(value) -> str:
    """A stored moment in the server's own time, the way a person writes it."""
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value or "")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def compose(store, *, period: str, waiting: list[dict], paused: dict | None,
            language: str = "en", url: str = "",
            now: datetime | None = None) -> Summary | None:
    """The summary of the last day or week, or ``None`` when there is nothing.

    ``waiting`` are the rows still waiting for somebody, as the findings page
    works them out — the summary must not count them differently. ``paused``
    is the fuse's state now, when it is tripped.
    """
    now = now or datetime.now(UTC)
    since = now - timedelta(days=PERIODS.get(period, 7))

    # Every fix is its own line. A failure is one line per finding, however
    # often it was retried, and none at all once a later attempt worked:
    # measured on a live store, one week held a hundred failed rows for
    # sixty-one findings.
    #
    # A row keeps the moment it was found, also once somebody has pressed its
    # button; a finding from last month dealt with this week carries when that
    # was, and belongs to this week. Read by the moment of finding alone, a
    # week spent clearing old findings by hand was a week with nothing to say.
    done, fixed_keys, failures = [], set(), {}
    for row in store.findings(limit=ROWS, fixed_only=True):
        finding = finding_from_row(row)
        if str(finding.data.get("_acted_at") or row.get("at") or "") < since.isoformat():
            continue
        rendered = policy.render(finding.data.get("_action"), language)
        if rendered:
            finding.action = rendered
        key = identity(row)[0]
        if policy.really_happened(row.get("action")):
            done.append(finding)
            fixed_keys.add(key)
        elif str(row.get("action") or "").startswith(policy.FAILED_PREFIX):
            failures.setdefault(key, finding)
    failed = [finding for key, finding in failures.items() if key not in fixed_keys]

    freed = {}
    for act in store.acts_since(since):
        if float(act.get("freed_mb") or 0) > 0:
            one = act.get("identity") or f"row:{act.get('id')}"
            freed[one] = max(float(act["freed_mb"]), freed.get(one, 0.0))
    freed_gb = round(sum(freed.values()) / 1024, 1)

    open_now = []
    for row in waiting:
        finding = finding_from_row(row)
        finding.title = f"{t(f'rules.{finding.rule}.title', language)}: {finding.title}"
        finding.action = None
        open_now.append(finding)

    recorded = store.pauses(since)
    # A pause from before the fuse's history was kept has no row of its own.
    # Still tripped, it is the most important thing the summary has to say.
    if paused and not any(p.get("resumed") is None for p in recorded):
        recorded.append({"reason": paused.get("reason"),
                         "params": paused.get("params"), "resumed": None})
    pauses = [Finding(
        rule=NOTICE_RULE, severity="error", service="correctarr",
        title=t(str(pause.get("reason") or ""), language,
                **(pause.get("params") or {})),
        message=str(pause.get("reason") or ""),
        params=dict(pause.get("params") or {}),
        action=(t("digest.resumed_at", language, when=_when(pause["resumed"]))
                if pause.get("resumed") else t("digest.still_paused", language)))
        for pause in recorded]

    if not (done or failed or open_now or pauses):
        return None

    sections = []
    if pauses:
        sections.append(Section(rule="digest_paused", severity="error",
                                findings=pauses, key="digest.paused"))
    if open_now:
        sections.append(Section(rule="digest_waiting", severity="warning",
                                findings=open_now, key="digest.waiting"))
    if failed:
        sections.append(Section(rule="digest_failed", severity="warning",
                                findings=failed, key="digest.failed"))
    if done:
        sections.append(Section(
            rule="digest_fixed", severity="info", findings=done,
            key="digest.fixed_freed" if freed_gb > 0 else "digest.fixed",
            params={"gb": freed_gb}))

    return Summary(groups=sections, total=len(done) + len(failed) + len(open_now),
                   fixed=len(done), language=language, url=url,
                   headline_key=f"digest.title_{period}",
                   numbers={"fixed": len(done), "waiting": len(open_now)})


def send(connections: list[dict], summary: Summary) -> list[dict]:
    """Hand the summary to every enabled connection, whatever its filters.

    A connection that only wants errors from the queue rules has still asked
    for nothing about summaries either way, and the setting that switches
    summaries on is the one place that decides whether they go out.
    """
    outcomes = []
    for connection in connections:
        if not connection.get("enabled", True):
            continue
        name = connection.get("name") or connection.get("kind")
        try:
            ok, detail = notifications.build(connection).send(summary)
        except Exception as e:                              # noqa: BLE001
            ok, detail = False, str(e)
        if not ok:
            log.warning("The summary via %s failed: %s", name, detail)
        outcomes.append({"id": int(connection.get("id") or 0), "name": name,
                         "ok": ok, "detail": detail})
    return outcomes


def due(store, period: str, now: datetime | None = None) -> bool:
    """Has long enough passed since the last one?

    The schedule fires once per period, but a restart at the wrong minute can
    make it fire twice. Half a period is a margin that absorbs that and still
    lets a summary through that was moved to an earlier hour.
    """
    if period not in PERIODS:
        return False
    now = now or datetime.now(UTC)
    last = store.get(SENT_KEY)
    if not last:
        return True
    try:
        moment = datetime.fromisoformat(str(last))
    except ValueError:
        return True
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return now - moment >= timedelta(days=PERIODS[period]) / 2


def preview(summary: Summary | None) -> dict | None:
    """The summary as data, for the page that shows what would be sent."""
    if summary is None:
        return None
    return {
        "headline": summary.headline(),
        "severity": summary.severity,
        "sections": [{
            "key": section.rule,
            "title": section.title(summary.language),
            "severity": section.severity,
            "count": len(section.findings),
            "lines": [{"title": f.title, "action": f.action or ""}
                      for f in section.findings],
        } for section in summary.groups],
    }
