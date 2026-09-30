"""Where the room on the disks is going, and how fast."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import i18n, sizing
from .core import language_for, require_user
from .library import arr_entries, library_of, service_summary

router = APIRouter()


@router.get("/api/storage")
def storage_overview(request: Request, _: dict = Depends(require_user)):
    """Where the room is going: per service, per disk, and how fast.

    Two services on one pool see it under two paths, so the disks are joined
    by what they report about themselves, and the pace of every service on a
    disk is added up before anything is said about when it is full.
    """
    language = language_for(request)
    services, contributions = [], []
    for entry in arr_entries():
        summary = service_summary(entry)
        try:
            snapshot = library_of(entry, "growth")
        except HTTPException as e:
            services.append({**summary, "ok": False, "error": str(e.detail)})
            continue
        paths = [root["path"] for root in snapshot["roots"]]
        paces = sizing.growth(snapshot["growth"], paths, items=snapshot["items"])
        for root in snapshot["roots"]:
            contributions.append((entry, root, paces.get(root["path"])))
        services.append({
            **summary, "ok": True, "error": None,
            "root_folders": [{k: root[k] for k in (
                "path", "accessible", "free_gb", "total_gb")}
                for root in snapshot["roots"]],
            "library": sizing.breakdown(snapshot["titles"]),
            "growth": _with_reason(sizing.combined(list(paces.values())),
                                   language),
        })

    disks = []
    groups = sizing.disks([root for _entry, root, _pace in contributions])
    for number, group in enumerate(groups, start=1):
        members = [(e, r, p) for e, r, p in contributions if r in group]
        known = [p for _e, _r, p in members if p and p["known"]]
        pace = (round(sum(p["gb_per_month"] for p in known), 1)
                if known else None)
        head = group[0]
        disks.append({
            "id": number,
            "free_gb": head["free_gb"], "total_gb": head["total_gb"],
            "used_percent": (round(100 * (1 - head["free"] / head["total"]), 1)
                             if head.get("total") and head.get("free") is not None
                             else None),
            "folders": [{"service_id": e["id"], "service": e["name"],
                         "path": r["path"]} for e, r, _p in members],
            "gb_per_month": pace,
            # A service on this disk with too little history adds nothing
            # to the pace, so the date is later than it will really be.
            "complete": len(known) == len(members),
            **sizing.outlook(head["free_gb"], pace),
        })
    for service in services:
        service["disks"] = sorted({
            disk["id"] for disk in disks for folder in disk["folders"]
            if folder["service_id"] == service["id"]})
    return {"services": services, "disks": disks}


def _with_reason(pace: dict, language: str) -> dict:
    pace = dict(pace)
    pace["text"] = (i18n.t(pace["reason"], language, days=pace["days"],
                           imports=pace["imports"],
                           need_days=sizing.MIN_GROWTH_DAYS,
                           need_imports=sizing.MIN_GROWTH_IMPORTS)
                    if pace["reason"] else None)
    return pace
