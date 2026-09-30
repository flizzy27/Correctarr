"""The guided setup."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from . import core
from .core import log, require_user

router = APIRouter()


@router.get("/api/setup/state")
def setup_state(_: dict = Depends(require_user)):
    """Whether the guided setup has been run through.

    Kept separate from the settings because it is not a preference: it is a
    one-off fact about this installation, and it should not appear on a page
    full of things somebody might want to change.
    """
    services = core.store.services()
    return {
        "completed": bool(core.store.get("setup_done", False)),
        "services": len(services),
        "arr_services": sum(1 for s in services if s["kind"] in ("radarr", "sonarr")),
        "notifications": len(core.store.notifications()),
    }


@router.post("/api/setup/complete")
def setup_complete(_: dict = Depends(require_user)):
    core.store.set("setup_done", True)
    log.info("The setup wizard was completed")
    return {"ok": True}


@router.post("/api/setup/restart")
def setup_restart(_: dict = Depends(require_user)):
    core.store.set("setup_done", False)
    return {"ok": True}
