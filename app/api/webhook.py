"""Where Radarr and Sonarr call in."""
from __future__ import annotations

import secrets

from fastapi import APIRouter, HTTPException, Request

from . import jobs
from .core import log, origin_of, webhook_token

router = APIRouter()

#: Webhook events worth waking up for. Everything else is recorded and
#: otherwise ignored, so an unknown event from a future version is harmless.
EVENTS = frozenset({"grab", "download", "manualinteractionrequired",
                    "health", "healthissue", "healthrestored"})


@router.post("/api/event")
async def event(request: Request, token: str = ""):
    """Receive webhook calls from Radarr and Sonarr."""
    # Compared as bytes: compare_digest refuses a str with anything outside
    # ASCII, and a made-up token with an umlaut in it was a server error.
    if not secrets.compare_digest(token.encode("utf-8"),
                                  webhook_token().encode("utf-8")):
        log.warning("Webhook with a wrong token from %s", origin_of(request))
        raise HTTPException(403, "Bad token")
    try:
        body = await request.json()
    except Exception:                                   # noqa: BLE001
        body = {}
    kind = str(body.get("eventType") or "").lower()
    source = str(body.get("instanceName") or "arr")[:40]
    log.info("Webhook: %s from %s", kind or "?", source)
    if kind == "test":
        return {"ok": True, "message": "Connection works"}
    # The toggle in Radarr is called onHealthIssue, but the value on the wire
    # is "Health" — the two do not match, and matching the toggle name meant
    # every health report was quietly dropped. "healthissue" stays in the list
    # so nothing breaks if a future version ever sends it.
    if kind in EVENTS:
        jobs.trigger_event(f"{source}/{kind}")
    return {"ok": True}
