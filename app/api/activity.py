"""The queue as it is now, and how the indexers are doing — both read only."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..arr import ArrError
from ..indexers import WEIGHTS, rank_deviation, ranked
from ..rules import size_left
from ..scoring import score
from . import core
from .core import log, require_user

router = APIRouter()


@router.get("/api/queue")
def queue(_: dict = Depends(require_user)):
    out = []
    for service in core.engine.arr_services():
        try:
            if not service.reachable()[0]:
                continue
            profiles = {p["id"]: p for p in service.profiles()}
            formats = {f["name"]: f for f in service.custom_formats()}
            for entry in service.queue():
                item = entry.get("movie") or entry.get("series") or {}
                profile = profiles.get(item.get("qualityProfileId"))
                total, hits = score(entry, profile, formats) if profile else (None, [])
                size = entry.get("size") or 0
                out.append({
                    "service": service.name, "kind": service.kind, "id": entry["id"],
                    "release": entry.get("title"), "item": item.get("title"),
                    "year": item.get("year"),
                    "profile": profile.get("name") if profile else None,
                    "state": entry.get("trackedDownloadState"),
                    "status": entry.get("status"),
                    "gb": round(size / 1024 ** 3, 2),
                    # Clamped: the remaining byte count is reported by the
                    # download client and briefly exceeds the total while a
                    # repair is running, which showed as a negative percentage.
                    "percent": (round(min(100.0, max(0.0,
                                100 * (1 - size_left(entry) / size))), 1)
                                if size else None),
                    "score_then": entry.get("customFormatScore"),
                    "score_now": total, "hits": hits,
                    "messages": [m for s in (entry.get("statusMessages") or [])
                                 for m in (s.get("messages") or [])],
                })
        except ArrError as e:
            log.warning("Could not read the queue of %s: %s", service.name, e)
        finally:
            service.close()
    return out


@router.get("/api/indexers")
def indexers(_: dict = Depends(require_user)):
    """The indexer ratings for inspection — read only, changes nothing."""
    cfg = core.engine.config()
    out = []
    for group in core.engine._indexer_state(cfg):
        deviations = {d["view"].name: d for d in rank_deviation(
            group["views"], int(cfg.get("indexer_rank_tolerance", 2)))}
        out.append({
            "name": group["name"], "weights": WEIGHTS,
            "indexers": [{
                "name": v.name, "priority": v.priority, "enabled": v.enabled,
                "queries": v.queries, "grabs": v.grabs,
                "yield": round(100 * v.grabs / v.queries, 2) if v.queries else None,
                "errors": v.failed_queries + v.failed_grabs,
                "response_ms": round(v.response_ms),
                "mean_score": round(v.mean_score), "mean_gb": round(v.mean_gb, 1),
                "samples": v.samples, "rating": v.rating, "parts": v.parts,
                "solid": v.solid, "notes": v.notes,
                "deviation": ({k: val for k, val in deviations[v.name].items()
                               if k != "view"} if v.name in deviations else None),
            } for v in ranked(group["views"])],
        })
    return out
