"""The settings, and whether the paths they name are really there."""
from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Body, Depends, Request

from .. import i18n, policy
from .. import settings as S
from ..rules import ALL
from . import core, jobs
from .core import fail, fail_from_value_error, language_for, require_user

router = APIRouter()


@router.get("/api/paths")
def paths(request: Request, _: dict = Depends(require_user)):
    """Whether the configured paths are actually there, with plain words.

    The single most common setup mistake: the paths inside the container do not
    match the ones the Arr services see. Otherwise that only surfaces when a
    rule stays quiet although it should have found something.
    """
    cfg = core.engine.config()
    language = language_for(request)
    # Which rules are actually set to remove something. Read access is always
    # needed; write access only matters if something is meant to be deleted,
    # and saying "read only" about a path nothing writes to is just noise.
    deleting = _rules_that_delete()
    out = []
    for key, required in (("path_downloads", True), ("path_incomplete", False),
                          ("path_movies", False), ("path_series", False)):
        path = cfg.get(key) or ""
        # Only the download folders are ever written to. The libraries are
        # mounted read only on purpose and should stay that way.
        needs_write = bool(deleting) and key in ("path_downloads", "path_incomplete")
        entry: dict[str, Any] = {"key": key, "path": path, "required": required,
                                 "needs_write": needs_write}
        if not path:
            entry |= {"state": "unset",
                      "note": i18n.t("path.unset_required" if required
                                     else "path.unset_optional", language)}
        elif not os.path.isdir(path):
            entry |= {"state": "missing",
                      "note": i18n.t("path.missing", language, path=path)}
        else:
            try:
                count = len(os.listdir(path))
                writable = os.access(path, os.W_OK)
                note = i18n.t("path.ok", language, count=count)
                if not writable and needs_write:
                    # The one combination that is a genuine problem: something
                    # is set to delete here and cannot. Name the rule, so the
                    # answer is either "mount it rw" or "stop it deleting".
                    entry["state"] = "no_write"
                    note += " — " + i18n.t(
                        "path.write_needed", language,
                        rules=", ".join(i18n.t(f"rules.{n}.title", language)
                                        for n in deleting))
                elif not writable:
                    note += " — " + i18n.t("path.read_only", language)
                entry |= {"entries": count, "writable": writable, "note": note}
                entry.setdefault("state", "ok")
            except OSError as e:
                entry |= {"state": "unreadable", "note": str(e)}
        out.append(entry)
    return {"paths": out, "cleanup": cfg.get("cleanup_paths", []),
            "deleting_rules": deleting}


def _rules_that_delete() -> list[str]:
    """Rules currently enabled AND set to an action that removes files."""
    settings = core.engine.rule_settings()
    return [r.name for r in ALL
            if settings[r.name]["enabled"]
            and settings[r.name]["action"] in policy.DESTRUCTIVE]


@router.get("/api/settings")
def read_settings(_: dict = Depends(require_user)):
    cfg = core.engine.config()
    values = {f.key: cfg.get(f.key, f.default) for f in S.FIELDS}
    return {"schema": S.describe(), "values": S.mask(values)}


@router.post("/api/settings")
def write_settings(request: Request, body: dict = Body(...),
                   _: dict = Depends(require_user)):
    """Accepts either ``{"key": …, "value": …}`` or several at once.

    Every value is validated against the schema before a single one is written —
    either all of them or none.
    """
    if "key" in body:
        incoming = {str(body["key"]): body.get("value")}
    elif isinstance(body.get("values"), dict):
        incoming = body["values"]
    else:
        raise fail(request, 400, "error.bad_settings_body")

    current = core.engine.config()
    clean: dict[str, Any] = {}
    for key, value in incoming.items():
        if key not in S.BY_KEY:
            raise fail(request, 400, "error.unknown_setting", key=key)
        try:
            clean[key] = S.validate(key, S.unmask(key, value, current.get(key)))
        except ValueError as e:
            raise fail_from_value_error(request, e) from e

    core.store.set_many(clean)
    if any(S.BY_KEY[k].reschedules for k in clean):
        jobs.schedule()

    language = language_for(request)
    notes = []
    for key, value in clean.items():
        if S.BY_KEY[key].kind == "path" and value and not os.path.isdir(value):
            notes.append(i18n.t("path.missing", language, path=value))
    updated = core.engine.config()
    return {"ok": True, "saved": sorted(clean),
            "values": S.mask({f.key: updated.get(f.key) for f in S.FIELDS}),
            "notes": notes}
