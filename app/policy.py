"""What happens when a rule finds something.

Until now a rule had one switch: report, or fix. That is too coarse for most of
them. Finding a release matched to the wrong film can reasonably mean four
different things depending on how much you trust the automation:

    report                  say so and leave it alone
    remove                  take it out of the queue, let it be grabbed again
    blocklist               take it out and refuse this release from now on
    blocklist_and_search    the above, and look for a replacement immediately

All four are defensible. Which one is right depends on how good the indexers
are, how much disk there is, and how much the person running it wants to be
asked first. So the choice belongs to them, not here.

Conditions
----------
On top of the action, a rule can carry conditions that have to hold before it
acts. These exist because the dangerous rules are dangerous in proportion to
what they touch:

    min_age_hours   wait this long before acting — a download that arrived two
                    minutes ago is not stuck, it is new
    max_gb          never act on anything bigger than this — a cheap way to
                    say "delete the small debris, ask me about the 60 GB remux"
    min_confidence  only act when the match is at least this certain

A finding that fails a condition is still reported. It is not hidden; it is
simply not acted on, and the reason is recorded so it is visible why.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

#: Doing nothing is always available and is always the safe default.
REPORT = "report"

#: Every action any rule can take. Keeping them in one place means the
#: interface, the validation and the translations all agree on the set.
ACTIONS = (
    REPORT,
    "remove",                # out of the queue, may be grabbed again
    "blocklist",             # out of the queue, never this release again
    "blocklist_and_search",  # the above, plus look for a replacement now
    "import",                # bring the file into the library
    "import_and_clean",      # the above, and clear the download client entry
    "search",                # look for a better copy
    "refresh",               # have the service read the file again
    "delete",                # remove files from disk, irreversibly
    "clear_warning",         # acknowledge a download client warning
    "remove_entry",          # drop a finished entry and its source folder
    "resume",                # start a paused download client
    "unblocklist",           # let a refused release be tried again
)

#: What each action needs from a finding in order to do anything at all. A rule
#: that offers an action its own findings cannot satisfy is a trap: the action
#: appears in the menu, gets chosen, and then quietly does nothing every time.
#:
#: ``entry_id`` means the finding's own field; everything else is a key in
#: ``finding.data``. Alternatives are separated, one of them is enough.
REQUIRES: dict[str, tuple[str, ...]] = {
    REPORT: (),
    "remove": ("entry_id",),
    "blocklist": ("entry_id", "release"),
    "blocklist_and_search": ("entry_id", "release"),
    "import": ("downloadId", "path"),
    "import_and_clean": ("downloadId", "path"),
    "search": ("item_id",),
    "refresh": ("item_id",),
    "delete": ("path",),
    "clear_warning": ("client",),
    "remove_entry": ("nzo_id",),
    "resume": ("client",),
    "unblocklist": ("blocklist_id",),
}

#: Actions that remove data. The interface marks them, and the conditions
#: default to something cautious for any rule that offers one.
DESTRUCTIVE = frozenset({"delete", "remove_entry"})

#: Actions that reach out and change something in another service.
CHANGES_SOMETHING = frozenset(ACTIONS) - {REPORT}

CONDITIONS = ("min_age_hours", "max_gb", "min_confidence")


@dataclass(frozen=True)
class Policy:
    """The decision for one rule."""
    action: str = REPORT
    min_age_hours: float = 0.0
    max_gb: float = 0.0          # 0 means no limit
    min_confidence: float = 0.0  # 0 means no requirement

    def acts(self) -> bool:
        return self.action != REPORT

    def as_dict(self) -> dict[str, Any]:
        return {"action": self.action, "min_age_hours": self.min_age_hours,
                "max_gb": self.max_gb, "min_confidence": self.min_confidence}


@dataclass(frozen=True)
class Verdict:
    """Whether to act on one finding, and why not when the answer is no."""
    act: bool
    action: str = REPORT
    #: A translation key plus its parameters, so the reason reads in the user's
    #: own language rather than in English only.
    reason: str = ""
    params: dict[str, Any] | None = None

    def explain(self, translate, language: str) -> str:
        if not self.reason:
            return ""
        return translate(self.reason, language, **(self.params or {}))


#: The range each condition is accepted in, used by both the API and the
#: interface so the two cannot drift apart.
LIMITS = {"min_age_hours": (0.0, 8760.0),   # a year
          "max_gb": (0.0, 10000.0),
          "min_confidence": (0.0, 1.0)}


def parse(raw: Any, allowed: tuple[str, ...], default_action: str,
          conditions: tuple[str, ...] = CONDITIONS) -> Policy:
    """Turn what is in the store into a policy, tolerantly.

    Anything unrecognised falls back to reporting. A configuration written by a
    newer version, or corrupted by hand, must never cause an action nobody
    asked for — the failure mode of this function is "does less than expected",
    never "does more".

    ``conditions`` names the ones this rule can actually answer. A condition
    outside that list is dropped rather than applied, because applying it would
    be worse than useless: the finding could never satisfy it, so the rule
    would quietly stop acting for a reason nobody chose.
    """
    if not isinstance(raw, dict):
        return Policy(action=default_action if default_action in allowed else REPORT)

    # No entry at all means "never decided", which is the rule's own default.
    # An entry naming something this rule cannot do means a configuration from
    # elsewhere — a newer build, a hand edit — and that falls back to report.
    # The two are not the same and must not be collapsed.
    stated = raw.get("action")
    if stated is None:
        action = default_action if default_action in allowed else REPORT
    else:
        action = str(stated)
        if action not in allowed:
            log.debug("Action %r is not available for this rule, reporting instead",
                      action)
            action = REPORT

    def number(key: str) -> float:
        if key not in conditions:
            return 0.0
        try:
            value = float(raw.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0.0
        low, high = LIMITS[key]
        return min(high, max(low, value))

    return Policy(action=action, min_age_hours=number("min_age_hours"),
                  max_gb=number("max_gb"),
                  min_confidence=number("min_confidence"))


def validate(raw: dict, allowed: tuple[str, ...],
             conditions: tuple[str, ...] = CONDITIONS) -> dict:
    """Check a policy submitted through the API. Raises ValueError with a key."""
    action = str(raw.get("action") or REPORT)
    if action not in allowed:
        raise ValueError(f"error.action_not_allowed|{action}")

    clean: dict[str, Any] = {"action": action}
    for key, (low, high) in LIMITS.items():
        if key not in raw:
            continue
        if key not in conditions:
            raise ValueError(f"error.condition_not_allowed|{key}")
        try:
            value = float(raw[key])
        except (TypeError, ValueError):
            raise ValueError(f"error.not_a_number|{key}") from None
        if not low <= value <= high:
            raise ValueError(f"error.out_of_range|{key}|{_tidy(low)}–{_tidy(high)}")
        clean[key] = value
    return clean


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------
def decide(policy: Policy, finding) -> Verdict:
    """Should this finding be acted on?

    The conditions are checked against whatever the finding happens to carry.
    A condition that cannot be evaluated — asking for a size on a finding that
    has none — is treated as not met, so the cautious answer wins by default.
    """
    if not policy.acts():
        return Verdict(act=False, reason="policy.report_only")

    data = finding.data or {}

    if policy.min_age_hours > 0:
        age = _age_hours(data)
        if age is None:
            return Verdict(act=False, action=policy.action,
                           reason="policy.age_unknown")
        if age < policy.min_age_hours:
            return Verdict(act=False, action=policy.action,
                           reason="policy.too_young",
                           params={"age": f"{age:.1f}",
                                   "needed": _tidy(policy.min_age_hours)})

    if policy.max_gb > 0:
        size = _size_gb(data)
        if size is None:
            return Verdict(act=False, action=policy.action,
                           reason="policy.size_unknown")
        if size > policy.max_gb:
            return Verdict(act=False, action=policy.action,
                           reason="policy.too_large",
                           params={"size": f"{size:.1f}",
                                   "limit": _tidy(policy.max_gb)})

    if policy.min_confidence > 0:
        confidence = data.get("confidence")
        if confidence is None:
            return Verdict(act=False, action=policy.action,
                           reason="policy.confidence_unknown")
        if float(confidence) < policy.min_confidence:
            return Verdict(act=False, action=policy.action,
                           reason="policy.not_confident_enough",
                           params={"confidence": f"{float(confidence):.0%}",
                                   "needed": f"{policy.min_confidence:.0%}"})

    return Verdict(act=True, action=policy.action)


def _age_hours(data: dict) -> float | None:
    for key in ("age_hours", "hours"):
        if data.get(key) is not None:
            try:
                return float(data[key])
            except (TypeError, ValueError):
                return None
    if data.get("minutes") is not None:
        try:
            return float(data["minutes"]) / 60
        except (TypeError, ValueError):
            return None
    return None


def _size_gb(data: dict) -> float | None:
    if data.get("gb") is not None:
        try:
            return float(data["gb"])
        except (TypeError, ValueError):
            return None
    if data.get("mb") is not None:
        try:
            return float(data["mb"]) / 1024
        except (TypeError, ValueError):
            return None
    return None


def _tidy(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


# ---------------------------------------------------------------------------
# Upgrading from the old switch
# ---------------------------------------------------------------------------
def from_legacy_switch(fix: bool, default_action: str) -> dict:
    """What ``fix: true`` used to mean, expressed as a policy.

    Anyone who had a rule set to fix keeps it fixing exactly as before; anyone
    who had it off keeps it reporting. An upgrade must not quietly start doing
    something, and must not quietly stop.
    """
    return {"action": default_action if fix else REPORT}
