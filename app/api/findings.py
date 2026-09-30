"""The findings: the log, what is waiting for somebody, and the buttons on it."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import i18n, policy
from ..arr import ArrError
from ..engine import identity
from ..rules import ALL, BY_NAME
from . import core
from .core import fail, fail_from_value_error, language_for, require_user

router = APIRouter()


class Decisions:
    """What deciding whether a finding is still open needs, read once.

    Three questions per finding, none of which the row can answer by itself:
    is it still there (the log keeps it long after the problem went away), has
    somebody dismissed it, and what is its rule set to do today.
    """

    def __init__(self) -> None:
        self.cfg = core.engine.config()
        self.current = core.store.seen_since(_still_current_since(self.cfg))
        self.dismissed = core.store.dismissed()

    def configured(self, rule) -> str:
        return core.engine.rule_policy(self.cfg, rule.name).action


def _still_current_since(cfg: dict) -> str:
    """How recently a pass must have come across a finding for it to count.

    Two full passes and a margin. A rule that only runs on the full pass
    touches its findings every ninety minutes by default, and one pass missed
    to a service that was briefly away must not make everything look solved.
    """
    try:
        deep = int(cfg.get("deep_minutes", 90) or 90)
    except (TypeError, ValueError):
        deep = 90
    minutes = max(180, 2 * deep + 30)
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()


def _data_of(row: dict) -> dict:
    data = row.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, TypeError):
            data = {}
    return data if isinstance(data, dict) else {}


def _why_not(data: dict, state: str, configured: str) -> tuple[str, dict]:
    """Why nothing was done about a finding by itself, as a key and its fields.

    Empty when something was done. The reason recorded by the pass comes
    first because it is the most specific; after that the result says it —
    a dry run, a failure — and what is left is the rule's own setting.
    """
    if state == "done":
        return "", {}
    held = data.get("_held")
    if held:
        params = data.get("_held_params")
        return str(held), params if isinstance(params, dict) else {}
    if state == "dry":
        return "policy.dry_run_on", {}
    if state == "failed":
        return "policy.last_try_failed", {}
    if configured == policy.REPORT:
        return "policy.report_only", {}
    return "policy.not_yet", {}


def _localise(rows: list[dict], language: str,
              context: Decisions | None = None) -> list[dict]:
    """Re-render everything a stored finding says, in the requested language.

    The store keeps the English text plus the keys and their parameters, so an
    entry written months ago still shows up in whatever language is active now.

    Three things are rendered, not one. The description was always translated.
    The **result of the action** was not — it was built where the action ran
    and stored as a sentence, so the German interface read "blocklisted, new
    search started" in English. And the reason a finding was *not* acted on was
    recorded and then never shown to anybody at all.

    On top of that each row says whether it is still waiting for somebody:
    ``open`` is a finding that is still there, was not dealt with, has
    something that can be done about it and has not been dismissed. Only the
    newest row of a finding can be open — the older ones are history.
    """
    context = context or Decisions()
    out = []
    newest: set[str] = set()
    for row in rows:
        data = _data_of(row)
        row = {**row, "data": data}

        key = data.get("_msg")
        if key and language != i18n.DEFAULT:
            row["description"] = i18n.t(key, language, **(data.get("_params") or {}))

        rendered = policy.render(data.get("_action"), language)
        if rendered:
            row["action"] = rendered
        state = _action_state(data, row.get("action"))
        row["action_state"] = state

        seen_key, dismiss_key = identity(row)
        row["dismissed"] = dismiss_key in context.dismissed
        row["current"] = seen_key in context.current
        first = seen_key not in newest
        newest.add(seen_key)

        # What this one finding can be told to do, and what would be done if
        # nobody says. Sent with the row because the interface draws a button
        # from it, and a button has to know before it is pressed.
        rule = BY_NAME.get(row.get("rule"))
        offers: list[str] = []
        configured = policy.REPORT
        if rule is not None:
            configured = context.configured(rule)
            offers = _offers(rule, data)
            row["can_do"] = offers
            suggested = core.engine.suggested_action(
                rule, _shallow_finding(row, data), configured) if offers else policy.REPORT
            row["suggested"] = suggested
            row["destructive"] = sorted(set(offers) & policy.DESTRUCTIVE)
            # Where the suggestion came from. A rule that looked at this case
            # and recommends something other than its own setting says why,
            # and that reason is only shown when its suggestion is the one
            # being offered.
            own = (suggested in (data.get("suggested_action"), data.get("suggested"))
                   and suggested != policy.REPORT)
            row["suggested_by"] = "finding" if own else "rule"
            reason = data.get("suggested_reason")
            if own and isinstance(reason, str) and reason:
                params = data.get("suggested_reason_params")
                row["suggested_reason"] = i18n.t(
                    reason, language, **(params if isinstance(params, dict) else {}))

        held = data.get("_held")
        if held:
            row["held_back"] = i18n.t(
                "findings_page.held_back", language,
                reason=i18n.t(held, language, **(data.get("_held_params") or {})))

        why, fields = _why_not(data, state, configured)
        if why:
            row["why"] = {"key": why, "text": i18n.t(
                "findings_page.held_back", language,
                reason=i18n.t(why, language, **fields))}

        row["open"] = bool(offers and first and state != "done"
                           and row["current"] and not row["dismissed"])
        out.append(row)
    return out


def _offers(rule, data: dict) -> list[str]:
    """What a button on this one finding may do.

    "Do what it suggests" is a setting, not a button — the button names the
    suggestion itself. And a finding whose own suggestion is to leave it alone
    — a download client that is not answering, somebody else's download —
    gets no button at all: every action on offer would be the wrong one.
    """
    if (data or {}).get("suggested") == policy.REPORT:
        return []
    return [a for a in rule.actions if a not in (policy.REPORT, policy.AS_SUGGESTED)]


#: The most rows looked through for open findings. A finding that is still
#: there is usually recent in the log, and a store trimmed to twenty thousand
#: rows does not need to be read end to end every thirty seconds.
OPEN_SCAN = 5000


def open_rows(context: Decisions) -> tuple[list[dict], int]:
    """The newest row of every finding that is still waiting for somebody.

    Also how many were left out because somebody dismissed them, so the
    interface can say that they exist.
    """
    acting = [r.name for r in ALL if r.modifies]
    names = set(acting)
    wanted = {k for k in context.current
              if len(parts := k.split("|")) > 1 and parts[1] in names}
    if not wanted:
        return [], 0
    out, taken, dismissed = [], set(), 0
    for row in core.store.findings(limit=OPEN_SCAN, rules=acting):
        if len(taken) >= len(wanted):
            break
        seen_key, dismiss_key = identity(row)
        if seen_key not in wanted or seen_key in taken:
            continue
        taken.add(seen_key)
        if _action_state(_data_of(row), row.get("action")) == "done":
            continue
        if dismiss_key in context.dismissed:
            dismissed += 1
            continue
        out.append(row)
    return out, dismissed


class _ShallowFinding:
    """Just enough of a finding for the suggestion, without rebuilding one."""

    __slots__ = ("rule", "data")

    def __init__(self, rule: str, data: dict):
        self.rule = rule
        self.data = data


def _shallow_finding(row: dict, data: dict) -> Any:
    return _ShallowFinding(str(row.get("rule") or ""), data)


def _action_state(data: dict, action: str | None) -> str:
    """"done", "dry", "failed" or "" — for something written today or in 2024.

    Rows from before the result carried a key are still in the store, and the
    only thing they have is the English sentence. Reading the prefix off that
    is exactly what the interface used to do, which is why it stays here as the
    fallback rather than in the interface.
    """
    stored = data.get("_action")
    if isinstance(stored, dict) and stored.get("state"):
        return str(stored["state"])
    text = str(action or "")
    if not text:
        return ""
    if text.startswith(policy.DRY_PREFIX):
        return "dry"
    if text.startswith(policy.FAILED_PREFIX):
        return "failed"
    return "done"


@router.get("/api/findings")
def findings(request: Request, limit: int = 200, rule: str | None = None,
             fixed_only: bool = False, open_only: bool = False,
             _: dict = Depends(require_user)):
    """The log, newest first — or, with ``open_only``, what is waiting for
    somebody: one row per finding that is still there and was not dealt with."""
    context = Decisions()
    if open_only:
        rows, _dismissed = open_rows(context)
        if rule:
            rows = [r for r in rows if r.get("rule") == rule]
        return _localise(rows[:max(1, min(limit, 1000))], language_for(request),
                         context)
    rows = core.store.findings(limit=max(1, min(limit, 1000)), rule=rule,
                               fixed_only=fixed_only)
    return _localise(rows, language_for(request), context)


_SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


@router.get("/api/decisions")
def decisions(request: Request, _: dict = Depends(require_user)):
    """What is waiting for somebody, rule by rule, for the overview.

    Each rule carries the ids of its open findings and what would be done to
    each if the recommendation were followed, so one button can do exactly
    that — and leave out whatever deletes, which keeps its own button.
    """
    context = Decisions()
    rows, dismissed = open_rows(context)
    grouped: dict[str, dict] = {}
    for row in _localise(rows, language_for(request), context):
        entry = grouped.setdefault(row["rule"], {
            "rule": row["rule"], "count": 0, "severity": row.get("severity") or "info",
            "actions": {}, "ids": [], "safe_ids": []})
        entry["count"] += 1
        if (_SEVERITY_ORDER.get(row.get("severity"), 3)
                < _SEVERITY_ORDER.get(entry["severity"], 3)):
            entry["severity"] = row["severity"]
        suggested = row.get("suggested") or policy.REPORT
        entry["actions"][suggested] = entry["actions"].get(suggested, 0) + 1
        entry["ids"].append(row["id"])
        if suggested not in policy.DESTRUCTIVE:
            entry["safe_ids"].append(row["id"])
    ordered = sorted(grouped.values(),
                     key=lambda e: (_SEVERITY_ORDER.get(e["severity"], 3), -e["count"]))
    return {"total": len(rows), "dismissed": dismissed, "rules": ordered,
            "destructive": sorted(policy.DESTRUCTIVE)}


@router.post("/api/findings/{finding_id}/dismiss")
def dismiss_finding(finding_id: int, request: Request,
                    _: dict = Depends(require_user)):
    """Leave this one alone until it changes.

    Hidden from what is waiting, not deleted: it stays in the log, and the
    moment the finding says something different it is back.
    """
    row = core.store.finding(finding_id)
    if row is None:
        raise fail(request, 404, "error.no_such_finding")
    seen_key, dismiss_key = identity(row)
    core.store.dismiss(dismiss_key, seen_key, str(row.get("rule") or ""))
    return {"ok": True, "id": finding_id, "dismissed": True}


@router.delete("/api/findings/{finding_id}/dismiss")
def undismiss_finding(finding_id: int, request: Request,
                      _: dict = Depends(require_user)):
    row = core.store.finding(finding_id)
    if row is None:
        raise fail(request, 404, "error.no_such_finding")
    core.store.undismiss(identity(row)[1])
    return {"ok": True, "id": finding_id, "dismissed": False}


@router.get("/api/fixed")
def fixed(request: Request, limit: int = 100, _: dict = Depends(require_user)):
    """Only what was actually changed — the record of work done."""
    rows = core.store.findings(limit=max(1, min(limit, 1000)), fixed_only=True)
    rows = [r for r in rows if policy.really_happened(r.get("action"))]
    return _localise(rows, language_for(request), Decisions())


class ActNow(BaseModel):
    """Which action to carry out. Left out, the rule decides."""
    action: str | None = None


@router.post("/api/findings/{finding_id}/act")
def act_on_finding(finding_id: int, body: ActNow, request: Request,
                   _: dict = Depends(require_user)):
    """Do something about one finding, now, because somebody pressed a button.

    Deliberately not subject to the dry run. That switch exists to stop changes
    nobody asked for; this is the opposite, and a button that quietly does
    nothing because of a setting on another page would be worse than no button
    at all. What it is subject to is the rule's own list of permitted actions.

    For a search it waits for the answer rather than reporting that a search
    was started. The whole question is whether anything is out there, and the
    service knows within seconds.
    """
    row = core.store.finding(finding_id)
    if row is None:
        raise fail(request, 404, "error.no_such_finding")
    try:
        return core.engine.act_now(row, body.action, language_for(request))
    except ValueError as e:
        if str(e) == "error.action_not_allowed":
            # Named, so the refusal says which action it was rather than
            # showing the placeholder.
            label = i18n.t(f"policy.action.{body.action}", language_for(request))
            if label.startswith("policy.action."):
                label = str(body.action)
            raise fail(request, 400, "error.action_not_allowed", action=label) from e
        raise fail_from_value_error(request, e) from e
    except ArrError as e:
        raise HTTPException(502, str(e)) from e


class ActOnMany(BaseModel):
    """Which findings, and optionally what to do with all of them."""
    ids: list[int] = Field(default_factory=list, max_length=500)
    action: str | None = None
    #: Anything that removes files is left out unless this says otherwise.
    #: A single button that deletes forty folders because it was pressed once
    #: is not a convenience.
    allow_destructive: bool = False


@router.post("/api/findings/act")
def act_on_many(body: ActOnMany, request: Request,
                _: dict = Depends(require_user)):
    """Do the same thing to a list of findings.

    Searches are not waited on here. One of them is worth twenty seconds of
    somebody's attention; forty of them is not, and what came of them shows up
    in the list on the next pass either way.
    """
    if not body.ids:
        raise fail(request, 400, "error.nothing_selected")

    rows, skipped = [], 0
    context = Decisions()
    for finding_id in dict.fromkeys(body.ids):
        row = core.store.finding(finding_id)
        if row is None:
            continue
        rule = BY_NAME.get(row.get("rule"))
        if rule is None:
            continue
        data = _data_of(row)
        if not _offers(rule, data):
            continue
        # What would actually be done to this one: the action asked for, or
        # its own recommendation. That is what decides whether it deletes —
        # not whether its rule could, somewhere further down its list.
        chosen = body.action or core.engine.suggested_action(
            rule, _shallow_finding(row, data), context.configured(rule))
        if not body.allow_destructive and chosen in policy.DESTRUCTIVE:
            skipped += 1
            continue
        rows.append(row)

    if not rows:
        raise fail(request, 400, "error.nothing_to_do")
    answer = core.engine.act_many(rows, body.action, language_for(request))
    return {**answer, "skipped": skipped}
