"""The rules: what each may do, and what it is set to do."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import policy
from ..rules import ALL, BY_NAME, CATEGORIES
from . import core
from .core import fail, require_user

router = APIRouter()


@router.get("/api/rules")
def list_rules(_: dict = Depends(require_user)):
    """Every rule, what it may do, and what it is currently set to do."""
    settings = core.engine.rule_settings()
    counts = core.store.summary()["per_rule"]
    return {
        "categories": list(CATEGORIES),
        "destructive": sorted(policy.DESTRUCTIVE),
        "limits": {k: list(v) for k, v in policy.LIMITS.items()},
        "rules": [{
            "name": r.name, "category": r.category, "found": counts.get(r.name, 0),
            "scope": r.scope, "only_kinds": list(r.only_kinds), "deep": r.deep,
            "modifies": r.modifies, "deletes": r.deletes,
            "actions": list(r.actions), "default_action": r.default_action,
            "conditions": list(r.conditions),
            **settings[r.name],
        } for r in ALL],
    }


class RuleUpdate(BaseModel):
    """What may be changed about a rule.

    ``fix`` is what the previous interface sent. It is still accepted so an
    older client, or a bookmarked request, keeps working — it simply means
    "this rule's own default action" or "report".
    """
    enabled: bool | None = None
    action: str | None = None
    min_age_hours: float | None = None
    max_gb: float | None = None
    min_confidence: float | None = None
    fix: bool | None = None


def _policy_failure(request: Request, error: ValueError) -> HTTPException:
    """Turn a rejected policy into a translated 400."""
    key, *parts = str(error).split("|")
    if key == "error.action_not_allowed":
        return fail(request, 400, key, action=parts[0] if parts else "")
    if key in ("error.not_a_number", "error.condition_not_allowed"):
        return fail(request, 400, key, field=parts[0] if parts else "")
    if key == "error.out_of_range":
        return fail(request, 400, key, field=parts[0] if parts else "",
                    range=parts[1] if len(parts) > 1 else "")
    return fail(request, 400, "error.bad_rules_body")


def _apply_to_rule(request: Request, rule, entry: dict, submitted: dict) -> dict:
    """Merge one submitted change into a rule's settings, validating it."""
    entry = dict(entry)
    if "enabled" in submitted:
        entry["enabled"] = bool(submitted["enabled"])

    wanted = {key: submitted[key] for key in ("action", *policy.CONDITIONS)
              if key in submitted}
    if "fix" in submitted and "action" not in wanted:
        if submitted["fix"] and not rule.modifies:
            raise fail(request, 400, "error.rule_cannot_fix", name=rule.name)
        wanted["action"] = rule.default_action if submitted["fix"] else policy.REPORT
    if not wanted:
        return entry

    # Conditions are meaningless without knowing which action they guard, so
    # the current action always travels with them.
    wanted.setdefault("action", entry.get("action", policy.REPORT))
    try:
        entry.update(policy.validate(wanted, rule.actions, rule.conditions))
    except ValueError as e:
        raise _policy_failure(request, e) from None
    return entry


@router.post("/api/rules/{name}")
def configure_rule(name: str, body: RuleUpdate, request: Request,
                   _: dict = Depends(require_user)):
    rule = BY_NAME.get(name)
    if rule is None:
        raise fail(request, 404, "error.no_such_rule", name=name)
    everything = core.engine.rule_settings()
    everything[name] = _apply_to_rule(
        request, rule, everything[name],
        body.model_dump(exclude_none=True))
    core.store.set("rules", everything)
    return {"ok": True, "rule": name, **everything[name]}


@router.post("/api/rules")
def configure_rules(request: Request, body: dict = Body(...),
                    _: dict = Depends(require_user)):
    """Several rules at once — "report only", for instance."""
    incoming = body.get("rules")
    if not isinstance(incoming, dict):
        raise fail(request, 400, "error.bad_rules_body")
    everything = core.engine.rule_settings()
    for name, value in incoming.items():
        rule = BY_NAME.get(name)
        if rule is None or not isinstance(value, dict):
            raise fail(request, 400, "error.no_such_rule", name=name)
        everything[name] = _apply_to_rule(request, rule, everything[name], value)
    core.store.set("rules", everything)
    return {"ok": True, "rules": everything}
