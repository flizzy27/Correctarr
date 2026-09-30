"""How things stand, the passes that were run, and starting one by hand."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from .. import auth, i18n
from . import core, jobs
from .core import BASE, BUILD, BUILT_AT, COMMIT, VERSION, fail, language_for, require_user
from .findings import Decisions, open_rows
from .services import probe_all

router = APIRouter()


@router.get("/api/status")
def status(request: Request, _: dict = Depends(require_user)):
    entries = core.store.services(enabled_only=True)
    probed = probe_all(entries)
    services = [{"name": entry["name"], "kind": entry["kind"], "url": entry["url"],
                 "ok": probed[entry["id"]][0], "info": probed[entry["id"]][1]}
                for entry in entries]
    recent = core.store.runs(1)
    scheduled = {j.id: (j.next_run_time.isoformat() if j.next_run_time else None)
                 for j in jobs.scheduler.get_jobs()}
    cfg = core.engine.config()
    return {
        "version": VERSION, "build": BUILD,
        "built_at": BUILT_AT, "commit": COMMIT,
        # Whether the guided setup has been run through. The button that opens
        # it sits in the top bar of every page, and once it has been done it is
        # a permanent offer to redo something nobody wants to redo.
        "setup_done": bool(core.store.get("setup_done", False)),
        "base": BASE, "auth": auth.mode(),
        "services": services, "running": core.engine.running,
        "last_trigger": core.engine.last_trigger,
        "last_run": recent[0] if recent else None,
        "next_fast": scheduled.get("fast"), "next_deep": scheduled.get("deep"),
        "summary": core.store.summary(), "store": core.store.size(),
        # How many findings are waiting for somebody to decide. Shown beside
        # the findings in the navigation on every page, not only the overview.
        "decisions": len(open_rows(Decisions())[0]),
        "dry_run": bool(cfg.get("dry_run")),
        "theme": cfg.get("theme", "midnight"),
        "density": cfg.get("density", "normal"),
        "safety": _safety_state(language_for(request)),
    }


def _safety_state(language: str) -> dict:
    """Whether automatic actions are paused, and why, in the reader's words."""
    paused = core.engine.guard().paused()
    if not paused:
        return {"paused": False}
    return {"paused": True, "since": paused.get("since"),
            "reason": i18n.t(str(paused.get("reason")), language,
                             **(paused.get("params") or {}))}


@router.post("/api/safety/resume")
def resume_actions(request: Request, _: dict = Depends(require_user)):
    """Let automatic actions run again after the fuse has tripped.

    Only by a person, and only through here. Nothing resumes by itself: the
    fuse trips because something was going round in a circle, and whatever
    that was is still there until somebody has looked at it.
    """
    core.engine.guard().resume()
    return {"ok": True, "safety": _safety_state(language_for(request))}


@router.get("/api/runs")
def runs(limit: int = 50, _: dict = Depends(require_user)):
    return core.store.runs(max(1, min(limit, 200)))


@router.post("/api/check")
def check(request: Request, deep: bool = False, _: dict = Depends(require_user)):
    if core.engine.running:
        raise fail(request, 409, "error.already_running")
    if not core.store.services(enabled_only=True):
        raise fail(request, 400, "error.no_services")
    return core.engine.run(deep=deep, trigger="manual")


@router.post("/api/maintenance/compact")
def compact(request: Request, _: dict = Depends(require_user)):
    cfg = core.engine.config()
    store = core.store
    removed = store.trim_findings(keep=int(cfg.get("log_keep", 20000)),
                                  days=int(cfg.get("log_days", 90)))
    store.prune_seen(30)
    store.prune_sessions()
    before = store.size()["mb"]
    store.compact()
    after = store.size()["mb"]
    return {"ok": True, "removed": removed, "before_mb": before, "after_mb": after,
            "message": i18n.t("message.compacted", language_for(request),
                              removed=removed,
                              freed=max(0, round(before - after, 2)))}
