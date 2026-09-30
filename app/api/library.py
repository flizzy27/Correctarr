"""One read of a service's library, kept for a few minutes.

The profile and storage pages both ask size questions, and each question is
answered from the same lists: titles, their files, the profiles and the disks.
"""
from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request

from .. import sizing
from ..arr import Arr, ArrError
from . import core
from .core import fail

#: How long one read of a library is used again. The size questions are asked
#: while somebody moves sliders, and a library of five thousand films is an
#: answer of some 35 MB; five minutes old is fresh enough to say how much room
#: is left.
LIBRARY_TTL = 300.0
_libraries: dict[int, tuple[float, dict]] = {}
_libraries_lock = threading.Lock()


def arr_entries() -> list[dict]:
    return [e for e in core.store.services(enabled_only=True)
            if e["kind"] in ("radarr", "sonarr")]


def arr_entry(request: Request, service_id: int) -> dict:
    for entry in arr_entries():
        if entry["id"] == service_id:
            return entry
    raise fail(request, 404, "error.no_such_service")


def arr_for(entry: dict) -> Arr:
    return Arr(entry["kind"], entry["url"], entry["api_key"],
               name=entry["name"], service_id=entry["id"],
               verify=core.engine.verifies(entry))


def service_summary(entry: dict) -> dict:
    return {"id": entry["id"], "name": entry["name"], "kind": entry["kind"]}


def forget_libraries() -> None:
    with _libraries_lock:
        _libraries.clear()


def library_of(entry: dict, *extras: str) -> dict:
    """Everything the size questions need from one service.

    ``extras`` are the reads only some questions need: ``recent`` history for
    the loop verdict, ``growth`` history for the pace of the disk.
    """
    with _libraries_lock:
        cached = _libraries.get(entry["id"])
    if cached and time.monotonic() - cached[0] < LIBRARY_TTL:
        # An extra read added later does not make the rest any fresher.
        read_at, snapshot = cached[0], dict(cached[1])
    else:
        read_at, snapshot = time.monotonic(), None
    if snapshot is not None and all(extra in snapshot for extra in extras):
        return snapshot
    kind = entry["kind"]
    connector = arr_for(entry)
    try:
        if snapshot is None:
            items = connector.items()
            episode_files = None
            if kind == "sonarr":
                episode_files = {}
                for record in connector.files(sizing.series_to_read(items)):
                    episode_files.setdefault(record.get("seriesId"), []).append(record)
            definitions = sizing.definitions(connector.quality_definitions(), kind)
            snapshot = {
                "items": items,
                "titles": sizing.titles(kind, items, episode_files),
                "rates": sizing.rates(definitions,
                                      sizing.samples(kind, items, episode_files)),
                "profiles": connector.profiles(),
                "formats": connector.custom_formats(),
                "roots": sizing.root_folders(connector.root_folders(),
                                             connector.disk_space()),
            }
        if "recent" in extras and "recent" not in snapshot:
            snapshot["recent"] = connector.history(500)
        if "growth" in extras and "growth" not in snapshot:
            since = datetime.now(UTC) - timedelta(days=sizing.GROWTH_WINDOW_DAYS)
            snapshot["growth"] = connector.history_since(
                since.strftime("%Y-%m-%dT%H:%M:%SZ"))
    except ArrError as e:
        raise HTTPException(502, str(e)) from e
    finally:
        connector.close()
    with _libraries_lock:
        _libraries[entry["id"]] = (read_at, snapshot)
    return snapshot
