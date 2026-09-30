"""What happened over the last days, read from the store alone."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..insights import summarise
from . import core
from .core import language_for, require_user
from .findings import Decisions, open_rows

router = APIRouter()


@router.get("/api/insights")
def insights_view(request: Request, days: int = 30, _: dict = Depends(require_user)):
    """What happened over the last days: counts per day, the rules behind them,
    how long problems stayed, and what is waiting now. Read from the store
    only — nothing here contacts a service."""
    context = Decisions()
    rows, dismissed = open_rows(context)
    out = summarise(core.store, days, context.current,
                    language=language_for(request))
    by_rule: dict[str, int] = {}
    for row in rows:
        name = str(row.get("rule") or "")
        by_rule[name] = by_rule.get(name, 0) + 1
    out["waiting"] = {"total": len(rows), "dismissed": dismissed,
                      "by_rule": dict(sorted(by_rule.items(), key=lambda kv: -kv[1]))}
    return out
