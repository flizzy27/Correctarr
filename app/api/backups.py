"""Settings, rules, services and notifications as one file, and back."""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import backup, i18n
from .. import settings as S
from . import core, jobs
from .core import VERSION, fail, language_for, log, require_user
from .services import KINDS

router = APIRouter()


@router.get("/api/backup")
def read_backup(secrets: bool = False, _: dict = Depends(require_user)):
    """Settings, rules, services and notifications as one file.

    Without API keys and tokens unless asked for: a backup gets copied to
    places, and a key in it is a key in all of them.
    """
    data = backup.export(core.store, core.engine.config(), core.engine.rule_settings(),
                         secrets=secrets, version=VERSION)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    return JSONResponse(data, headers={
        "Content-Disposition": f'attachment; filename="correctarr-backup-{stamp}.json"',
        "Cache-Control": "no-store"})


class Restore(BaseModel):
    """A backup, and whether to write it or only say what it would change."""
    backup: dict
    apply: bool = False


@router.post("/api/backup/restore")
def restore_backup(body: Restore, request: Request, _: dict = Depends(require_user)):
    """Check a backup, say what it would change, and write it when told to.

    Nothing is ever deleted: whatever the backup does not mention stays as it
    is. Anything in it that fails the checks the interface is held to is
    skipped and listed, and the rest goes ahead.
    """
    language = language_for(request)
    rules_now = core.engine.rule_settings()
    try:
        planned = backup.plan(core.store, core.engine.config(), rules_now,
                              body.backup, KINDS)
    except ValueError as e:
        raise fail(request, 400, str(e)) from e
    if body.apply and planned.count:
        backup.apply(core.store, planned, rules_now)
        if planned.reschedules:
            jobs.schedule()
        log.info("A backup was restored: %d change(s)", planned.count)
    return {
        "ok": True, "applied": bool(body.apply and planned.count),
        "count": planned.count, "changes": planned.changes,
        # A service restored here is only written to the store. Its webhook
        # points at whatever address and token the installation it came from
        # had, and is put right by saving the service once — which is a change
        # in Radarr or Sonarr, and so left to a person.
        "webhooks": [entry["name"] for entry in planned.services
                     if entry["kind"] in ("radarr", "sonarr") and entry["webhook"]
                     and entry["enabled"]],
        "skipped": [{"section": section, "name": name,
                     "reason": i18n.t(key, language, detail=_reason_text(
                         str(fields.get("reason") or ""), language))}
                    for section, name, key, fields in planned.skipped],
    }


def _reason_text(raw: str, language: str) -> str:
    """A validation error carried as ``key|field|value``, in words."""
    if not raw:
        return ""
    key, *parts = raw.split("|")
    first = parts[0] if parts else ""
    second = parts[1] if len(parts) > 1 else ""
    label = (i18n.t(f"settings.{first}.label", language) if first in S.BY_KEY
             else first)
    return i18n.t(key, language, field=label, value=second, action=first,
                  range=second)
