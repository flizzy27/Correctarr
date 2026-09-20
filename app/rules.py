"""The checks.

Every rule reports what it finds as a Finding. Whether a finding is also acted
on is decided by the rule's own setting (check / fix) and the global dry run.

Guiding principle: **when in doubt, do nothing.** A rule that is not sure only
reports. Better one finding left alone than one good file thrown away.

Scope of a rule
---------------
``scope="service"``  runs per Arr service — each has its own queue.
``scope="once"``     runs once per pass. Anything concerning the download
                     client, the filesystem or the indexers: there is only one
                     of those, and checking twice would mean reporting twice.

``only_kinds`` narrows it further. The library rules work on ``movieFile`` and
therefore apply to Radarr only — visible in the interface rather than silently
skipped.

Texts
-----
Nothing user facing is written here. Each rule carries translation keys
(``rules.<name>.title`` and ``rules.<name>.help``), and each finding carries a
message key plus its parameters. That is what lets the same finding appear in
English and in German without this file knowing either language.
"""
from __future__ import annotations

import difflib
import logging
import os
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from . import compat, policy, years
from .arr import Arr
from .i18n import t
from .indexers import UNKNOWN_TO_PROWLARR, rank_deviation
from .matching import match
from .scoring import score

log = logging.getLogger(__name__)

BLOCKED = -900000

VIDEO_SUFFIXES = (".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".wmv", ".mpg", ".mpeg")
ARCHIVE_SUFFIXES = (".rar", ".par2", ".sfv", ".nfo", ".srr", ".zip", ".7z", ".001", ".tmp")


@dataclass
class Finding:
    rule: str
    severity: str                 # "error" | "warning" | "info"
    title: str
    message: str                  # translation key
    params: dict = field(default_factory=dict)
    service: str = "radarr"
    entry_id: int | None = None
    action: str | None = None
    is_new: bool = True           # seen for the first time (or not for a while)
    data: dict = field(default_factory=dict)

    def describe(self, language: str = "en") -> str:
        return t(self.message, language, **self.params)

    def as_dict(self, language: str = "en") -> dict:
        return {
            "rule": self.rule, "severity": self.severity, "title": self.title,
            "description": self.describe(language), "service": self.service,
            "entry_id": self.entry_id, "action": self.action,
            "is_new": self.is_new,
            "data": {**self.data, "_msg": self.message, "_params": self.params},
        }


@dataclass(frozen=True)
class Rule:
    """One check, and what it is allowed to do about what it finds.

    ``actions`` is the menu the interface offers for this rule, and the server
    refuses anything outside it. ``default_action`` is what a fresh install
    does — chosen per rule rather than globally, because the sensible default
    is not the same everywhere: blocklisting a release matched to the wrong
    film costs nothing, while deleting a folder is worth thinking about first.
    """
    name: str
    category: str
    check: Callable
    actions: tuple[str, ...] = (policy.REPORT,)
    default_action: str = policy.REPORT
    #: Conditions this rule's findings can actually answer. Offering one the
    #: findings carry no data for would be a trap: it could never be met, and
    #: the rule would silently stop acting.
    conditions: tuple[str, ...] = ()
    scope: str = "service"                 # "service" | "once"
    only_kinds: tuple[str, ...] = ()       # empty = every kind
    deep: bool = False                     # only in the full pass

    @property
    def modifies(self) -> bool:
        """Can this rule do anything at all beyond saying so?"""
        return len(self.actions) > 1

    @property
    def deletes(self) -> bool:
        """Can any of its actions remove data irreversibly?"""
        return bool(set(self.actions) & policy.DESTRUCTIVE)

    @property
    def fix_by_default(self) -> bool:
        return self.default_action != policy.REPORT


CATEGORIES = ("queue", "import", "library", "downloader", "indexers", "system")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _messages(entry: dict) -> str:
    parts = [m for s in (entry.get("statusMessages") or [])
             for m in (s.get("messages") or [])]
    if entry.get("errorMessage"):
        parts.append(entry["errorMessage"])
    return " ".join(parts)


def _age_minutes(timestamp: str | None) -> float | None:
    if not timestamp:
        return None
    try:
        moment = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return (datetime.now(UTC) - moment).total_seconds() / 60
    except ValueError:
        return None


def _bare_title(text: str) -> str:
    """Reduce a release name to the plain title.

    Cut at the first technical marker or the year — what follows describes the
    edition, not the film. Without that cut, "1080p BluRay DTS x264" dilutes the
    comparison so much that even correct matches fall through.
    """
    value = re.sub(r"\.(mkv|mp4|avi|m4v)$", "", text or "", flags=re.IGNORECASE)
    value = re.sub(r"[._]+", " ", value)
    value = value.replace("&", " and ")
    cut = re.search(
        r"\b(19[0-9]{2}|20[0-3][0-9]"
        r"|\d{3,4}p|[0-9]{3,4}i"
        r"|bluray|blu ray|bdrip|brrip|web ?dl|web ?rip|webhd|hdtv|dvdrip|remux|uhd"
        r"|x ?26[45]|h ?26[45]|hevc|avc|xvid|divx|av1"
        r"|dts|ac3|eac3|aac|truehd|atmos|ddp?[0-9]?|flac|opus"
        r"|german|deutsch|english|multi|dl|dubbed|synchro"
        r"|complete|season|s[0-9]{2}e[0-9]{2})\b",
        value, flags=re.IGNORECASE)
    if cut:
        value = value[:cut.start()]
    return re.sub(r"[^a-z0-9 ]", " ", value.lower()).strip()


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _bare_title(a), _bare_title(b)).ratio()


def _item(entry: dict) -> dict:
    return entry.get("movie") or entry.get("series") or {}


def _top_folder(path: str) -> str:
    return (path or "").replace("\\", "/").split("/")[0]


# ===========================================================================
# Category: queue
# ===========================================================================
def size_left(entry: dict) -> float:
    """How much of a queue entry is still to come.

    Read through the alias table rather than by name: the services carry
    ``sizeleft`` today but have already announced ``sizeLeft``, and the rule
    that watches for a download standing still must not be the thing that
    stops working the day that rename lands.
    """
    return compat.number(entry, "sizeleft")


def _gb(entry: dict) -> float:
    """A queue entry's size in GB, so size conditions have something to read."""
    try:
        return round((entry.get("size") or 0) / 1024 ** 3, 2)
    except (TypeError, ValueError):
        return 0.0


def check_wrong_year(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """The year in the release name contradicts the title it was matched to.

    Checked in EVERY state on purpose, not only at import: a wrong film stays
    wrong, and the sooner it surfaces the less bandwidth is wasted.

    The comparison itself lives in :mod:`app.years`, because it is far less
    obvious than it looks — a title that is itself a number, a spelled out
    resolution, and the several defensible answers to "what year is this film
    from" all have to be handled before the two numbers can be compared. The
    finding carries a confidence, so a near miss and a twenty year gap can be
    treated differently.
    """
    tolerance = int(cfg.get("year_tolerance", 1))
    findings = []
    for entry in ctx["queue"]:
        item = _item(entry)
        verdict = years.judge(entry.get("title", ""), item, tolerance)
        if not verdict.wrong:
            continue
        findings.append(Finding(
            rule="wrong_year", severity="error", service=arr.kind, entry_id=entry["id"],
            title=f"{item.get('title', '?')} ({verdict.expected})",
            message="finding.wrong_year",
            params={"found": ", ".join(map(str, verdict.found)),
                    "expected": verdict.expected},
            data={"release": entry.get("title"), "years": list(verdict.found),
                  "expected": verdict.expected, "distance": verdict.distance,
                  # How sure the mismatch is, so a condition can be set to act
                  # only on the obvious ones and report the rest.
                  "confidence": verdict.confidence, "gb": _gb(entry)},
        ))
    return findings


def check_wrong_title(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """The release name resembles none of the known titles for this item.

    Important: localised releases carry the local distribution title —
    "Stichtag" for *Due Date*, "Wir" for *Us*. Comparing only against title and
    original title flags all of those wrongly. The distribution titles live in
    ``alternateTitles``, but ONLY in the full item list; the queue entry has the
    field empty. That is why this rule needs the full pass and stays quiet
    otherwise.

    Measured: with alternate titles 0 false positives, without them 23 of 23.
    """
    items = {i["id"]: i for i in ctx.get("items", []) if i.get("id")}
    if not items:
        return []                       # without alternate titles, no judgement
    threshold = float(cfg.get("title_similarity", 0.45))
    findings = []
    for entry in ctx["queue"]:
        item = _item(entry)
        full = items.get(item.get("id")) or item
        release = entry.get("title") or ""
        if not release or not full.get("title"):
            continue
        names = [full.get("title"), full.get("originalTitle")]
        names += [a.get("title") for a in (full.get("alternateTitles") or [])]
        names = [n for n in names if n]
        best = max((_similarity(release, n) for n in names), default=0.0)
        if best >= threshold:
            continue
        findings.append(Finding(
            rule="wrong_title", severity="error", service=arr.kind, entry_id=entry["id"],
            title=full.get("title", "?"),
            message="finding.wrong_title",
            params={"count": len(names), "percent": f"{best:.0%}"},
            data={"release": release, "similarity": round(best, 3),
                  # Confidence in the FINDING, so the inverse of the match:
                  # the less the name resembles any known title, the surer
                  # this release belongs to something else.
                  "confidence": round(1 - best, 3), "checked": len(names),
                  "gb": _gb(entry)},
        ))
    return findings


def check_profile_violation(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """The waiting download breaks today's profile rules.

    A release is scored only when it is grabbed. Change a profile afterwards and
    the download runs to nothing. Here it is re-scored against the rules that
    apply now — and against the RAW release name, because Radarr itself never
    sees all of it (see ``scoring.py``).
    """
    profiles = {p["id"]: p for p in ctx["profiles"]}
    formats = {f["name"]: f for f in ctx["formats"]}
    findings = []
    for entry in ctx["queue"]:
        item = _item(entry)
        profile = profiles.get(item.get("qualityProfileId"))
        if not profile:
            continue
        total, hits = score(entry, profile, formats)
        if total > BLOCKED:
            continue
        blocking = [h for h in hits if "-999999" in h]
        findings.append(Finding(
            rule="profile_violation", severity="error", service=arr.kind,
            entry_id=entry["id"], title=item.get("title", "?"),
            message="finding.profile_violation",
            params={"profile": profile.get("name"),
                    "reason": ", ".join(blocking) or f"score {total}"},
            data={"release": entry.get("title"), "score": total, "hits": hits,
                  "gb": _gb(entry)},
        ))
    return findings


def check_not_an_upgrade(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """The import would not be an upgrade, so it sits there forever."""
    findings = []
    for entry in ctx["queue"]:
        if entry.get("trackedDownloadState") != "importPending":
            continue
        if "upgrade" not in _messages(entry).lower():
            continue
        findings.append(Finding(
            rule="not_an_upgrade", severity="warning", service=arr.kind,
            entry_id=entry["id"], title=_item(entry).get("title", "?"),
            message="finding.not_an_upgrade",
            data={"release": entry.get("title"), "gb": _gb(entry)},
        ))
    return findings


def check_stalled(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """A download that started and then stopped moving.

    The download client works through its queue in order, so almost every entry
    has zero bytes — that is normal and not a fault. Only something that started
    and then stopped is reported. The store remembers the remaining byte count
    between runs to tell the two apart.
    """
    threshold = int(cfg.get("stalled_minutes", 120))
    store = ctx["store"]
    # Keyed on the connection rather than on the kind. Two Radarr instances
    # share a kind and nothing else, and the pruning below has to be able to
    # tell "this entry left my queue" from "this entry belongs to somebody
    # else's queue and I have never seen it".
    scope = f"{getattr(arr, 'service_id', None) or arr.kind}:"
    findings, active = [], set()
    for entry in ctx["queue"]:
        key = f"{scope}{entry.get('downloadId') or entry['id']}"
        active.add(key)
        left, total = size_left(entry), entry.get("size") or 0
        if not total:
            continue
        minutes = store.check_progress(key, left)
        if left >= total or left <= 0 or minutes < threshold:
            continue
        percent = 100 * (1 - left / total)
        findings.append(Finding(
            rule="stalled", severity="warning", service=arr.kind, entry_id=entry["id"],
            title=_item(entry).get("title", "?"),
            message="finding.stalled",
            params={"minutes": int(minutes), "percent": f"{percent:.0f}"},
            data={"release": entry.get("title"), "minutes": int(minutes),
                  "percent": round(percent, 1), "gb": _gb(entry)},
        ))
    store.prune_progress(active, scope)
    return findings


def check_grab_loop(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """The same title is grabbed over and over and discarded again.

    Typical when no available release satisfies the profile: grab, fail, search,
    grab again. Only reported — intervening would be dangerous, because the
    cause sits in the profile and that is where it has to be fixed.
    """
    threshold = int(cfg.get("loop_grabs", 4))
    hours = int(cfg.get("loop_hours", 12))
    counter: Counter = Counter()
    last_seen: dict[int, str] = {}
    for entry in ctx.get("history", []):
        if not compat.event_is(entry, compat.GRABBED):
            continue
        age = _age_minutes(entry.get("date"))
        if age is None or age > hours * 60:
            continue
        item_id = entry.get("movieId") or entry.get("seriesId")
        if item_id:
            counter[item_id] += 1
            last_seen.setdefault(item_id, entry.get("sourceTitle", ""))
    items = {i["id"]: i for i in ctx.get("items", []) if i.get("id")}
    findings = []
    for item_id, count in counter.items():
        if count < threshold:
            continue
        item = items.get(item_id, {})
        findings.append(Finding(
            rule="grab_loop", severity="warning", service=arr.kind,
            title=item.get("title", f"ID {item_id}"),
            message="finding.grab_loop",
            params={"count": count, "hours": hours},
            data={"grabs": count, "last": last_seen.get(item_id, ""),
                  "item_id": item_id},
        ))
    return findings


# ===========================================================================
# Category: import
# ===========================================================================
def check_manual_import(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """Manual work is demanded although title and year line up.

    Imported only when BOTH the year and the title similarity check out.
    Everything else belongs to ``wrong_year`` or ``wrong_title`` — this rule
    must never cement a wrong match.
    """
    tolerance = int(cfg.get("year_tolerance", 1))
    threshold = float(cfg.get("title_similarity", 0.45))
    findings = []
    for entry in ctx["queue"]:
        if entry.get("trackedDownloadState") != "importBlocked":
            continue
        if "manual import" not in _messages(entry).lower():
            continue
        item = _item(entry)
        release = entry.get("title", "")
        # Importing without being asked needs the year to be stated AND to fit.
        # A name that mentions no year at all is not evidence of anything, and
        # this is the gate in front of an action, not a reason to report one.
        verdict = years.judge(release, item, tolerance)
        year_ok = bool(verdict.found) and not verdict.wrong
        title_ok = max(_similarity(release, item.get("title", "")),
                       _similarity(release, item.get("originalTitle") or "")) >= threshold
        if not (year_ok and title_ok):
            continue
        findings.append(Finding(
            rule="manual_import", severity="warning", service=arr.kind,
            entry_id=entry["id"], title=item.get("title", "?"),
            message="finding.manual_import",
            data={"release": release, "downloadId": entry.get("downloadId"),
                  "item_id": item.get("id")},
        ))
    return findings


def _root_path(candidate: dict, folder: str, cfg: dict) -> str:
    """The path of the top folder as this container sees it."""
    for base in (cfg.get("cleanup_paths") or []):
        guess = os.path.join(base, folder)
        if os.path.isdir(guess):
            return guess
    path = (candidate.get("path") or "").replace("\\", "/")
    return path.split("/" + folder)[0] + "/" + folder if folder and folder in path else path


def _unpack_state(candidate: dict, ctx: dict, cfg: dict) -> tuple[str, float, int]:
    """Judge an _UNPACK_ folder: (state, age_hours, videos).

    state is "running", "done_not_renamed" or "failed".

    Important for the age: **do not use the file time.** Unpacked files carry
    the timestamp stored inside the archive, which can be years old — measured
    at 3.8 years for a folder that was in fact 2.6 hours old. What counts is the
    time of the FOLDER.
    """
    path = candidate.get("_root_path") or ""
    minimum = float(cfg.get("trash_video_mb", 300)) * 1024 ** 2
    age, videos = 0.0, 0
    try:
        if path and os.path.isdir(path):
            age = (time.time() - os.path.getmtime(path)) / 3600
            for folder, _, files in os.walk(path):
                for name in files:
                    if not name.lower().endswith(VIDEO_SUFFIXES):
                        continue
                    try:
                        if os.path.getsize(os.path.join(folder, name)) >= minimum:
                            videos += 1
                    except OSError:
                        pass
    except OSError:
        pass

    # Does the download client still know about it? Then it is working on it.
    name = _top_folder(candidate.get("relativePath") or "")
    name = name.replace("_UNPACK_", "").replace("_FAILED_", "")
    known = any(name[:35] in n or n[:35] in name
                for n in (ctx.get("downloader_names") or set()) if n)
    if known:
        return "running", age, videos
    if videos:
        return "done_not_renamed", age, videos
    return "failed", age, videos


def check_unpack_failed(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """An _UNPACK_ folder where unpacking really did fail.

    Context matters: ``_UNPACK_`` is entirely normal at first — that is what
    SABnzbd calls a folder WHILE it is unpacking. An early draft reported every
    such folder immediately and mostly produced noise: 18 of 25 findings in one
    hour were exactly this, a different folder each time, because a download
    wave creates new ones constantly.

    So only a genuine failure is reported: the download client no longer knows
    the job, the folder is old enough, and there is NO usable video file in it.
    Folders that do contain a finished video are picked up by
    ``unmatched_files`` and imported.
    """
    threshold = float(cfg.get("unpack_timeout_hours", 2))
    findings, seen = [], set()
    for candidate in ctx.get("import_candidates", []):
        path = candidate.get("relativePath") or ""
        if "_UNPACK_" not in path and "_FAILED_" not in path:
            continue
        folder = _top_folder(path)
        if not folder or folder in seen:
            continue
        seen.add(folder)
        enriched = {**candidate, "_root_path": _root_path(candidate, folder, cfg)}
        state, age, videos = _unpack_state(enriched, ctx, cfg)
        if state != "failed" or age < threshold:
            continue
        findings.append(Finding(
            rule="unpack_failed", severity="warning", service=arr.kind,
            title=folder[:70],
            message="finding.unpack_failed",
            params={"hours": f"{age:.1f}"},
            data={"path": enriched["_root_path"], "age_hours": round(age, 1),
                  "videos": videos},
        ))
    return findings


def check_detached_folder(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """The services and this container see different download folders.

    Happens when the directory on the host is deleted and recreated while a
    container still holds it mounted: the container then keeps working in the
    old, detached directory. From outside that is invisible, it still occupies
    space, and on the next restart everything in it would be lost.

    Detected by the services reporting files in the download folder while the
    same folder looks empty here. **The same** is meant literally: the listing
    and the check both use ``path_downloads``. An earlier build fetched against
    a hard coded ``/downloads`` and checked against the configured path — the
    moment anyone changed it, that was a false alarm.
    """
    path = cfg.get("path_downloads") or "/downloads"
    reported = len(ctx.get("import_candidates", []))
    if not reported:
        return []
    try:
        visible = len(os.listdir(path))
    except OSError:
        return []                     # not mounted -> no statement
    if visible > 0:
        return []
    return [Finding(
        rule="detached_folder", severity="error", service=arr.kind,
        title=path,
        message="finding.detached_folder",
        params={"service": arr.name, "count": reported, "path": path},
        data={"reported": reported, "visible": visible, "path": path},
    )]


def _from_history(name: str, ctx: dict) -> tuple[dict | None, str]:
    """Look up in the grab history what this release was fetched for.

    By far the most reliable source: the service KNOWS which title it grabbed
    for and records it. On import that link is lost, because only the file name
    is parsed from then on — and that is exactly what fails for "Crank.I.2006"
    or "The.Transporter.The.Mission". The history still has the answer.
    """
    short = _top_folder(name)
    if len(short) < 8:
        return None, ""
    items = {i["id"]: i for i in ctx.get("items", []) if i.get("id")}
    for entry in ctx.get("history", []):
        # Only events whose source title is a release name. A history row for a
        # deleted or renamed file carries a path there instead, and a path that
        # happens to share its first characters with the folder would claim it
        # for whatever title that file belonged to.
        if not (compat.event_is(entry, compat.GRABBED)
                or compat.event_is(entry, compat.IMPORTED)):
            continue
        source = entry.get("sourceTitle") or ""
        if not source:
            continue
        if source[:45] == short[:45] or short[:45] in source or source[:45] in short:
            item_id = entry.get("movieId") or entry.get("seriesId")
            if item_id and item_id in items:
                return items[item_id], "history"
    return None, ""


def check_unmatched_files(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """A file in the download folder that cannot be matched to a title.

    The parser trips over small things — "Crank.I.2006" is not found even though
    the film is in the library as "Crank" with the alternate title "Crank 1".
    Files like that then sit there finished for hours or days; 13 and 14 hours
    for 20 GB together in the observed case.

    Matching happens in two stages:

      1. **Grab history.** It is recorded which title was grabbed for. That is
         not a guess, it is a fact.
      2. **Name matching.** Only when stage 1 gives nothing — for files that
         were dropped in by hand, say.

    If neither works and the file has been lying around long enough, the release
    is blocklisted and searched for again rather than left there forever.
    """
    findings, seen = [], set()
    candidates = ctx.get("match_candidates")
    profiles = {p["id"]: p for p in ctx.get("profiles", [])}
    formats = {f["name"]: f for f in ctx.get("formats", [])}
    give_up = float(cfg.get("give_up_hours", 6))

    for candidate in ctx.get("import_candidates", []):
        path = candidate.get("relativePath") or ""
        rejections = [r.get("reason", "") for r in candidate.get("rejections", [])]
        folder = _top_folder(path)
        if not folder:
            continue
        if "_UNPACK_" in path or "_FAILED_" in path:
            # Only taken over when unpacking is finished and a video is ready —
            # then it is not a stuck unpack any more, just an unimported file.
            enriched = {**candidate, "_root_path": _root_path(candidate, folder, cfg)}
            state, _, videos = _unpack_state(enriched, ctx, cfg)
            if state != "done_not_renamed" or not videos:
                continue
        elif not any("unknown" in r.lower() for r in rejections):
            continue
        if folder in seen:
            continue
        seen.add(folder)

        # Stage 1: the history knows
        item, source = _from_history(folder, ctx)
        confidence = 1.0 if item else 0.0
        reason = t("finding.matched_by_history", "en",
                   title=(item or {}).get("title", "")) if item else ""
        # Stage 2: name matching
        if not item and candidates:
            item, confidence, reason = match(
                folder, candidates, year_tolerance=int(cfg.get("year_tolerance", 1)))

        better, comparison = False, ""
        if item:
            profile = profiles.get(item.get("qualityProfileId"))
            existing = item.get("movieFile") or {}
            if profile:
                incoming = {"title": folder, "size": candidate.get("size"),
                            "quality": candidate.get("quality"), "movie": item}
                new_score, _ = score(incoming, profile, formats)
                if existing:
                    current = {"title": (existing.get("sceneName")
                                         or existing.get("relativePath") or ""),
                               "size": existing.get("size"),
                               "quality": existing.get("quality"), "movie": item}
                    old_score, _ = score(current, profile, formats)
                else:
                    old_score = None
                better = old_score is None or new_score > old_score
                comparison = (f"{old_score} → {new_score}" if old_score is not None
                              else f"no file yet, {new_score}")

        age = _age_minutes(candidate.get("dateAdded")) or 0
        data = {"path": candidate.get("path"), "rejections": rejections,
                "confidence": round(confidence, 3),
                "age_hours": round(age / 60, 1),
                "gb": round((candidate.get("size") or 0) / 1024 ** 3, 1),
                "quality": candidate.get("quality"),
                "languages": candidate.get("languages") or [],
                "release": folder,
                # So the fix hits the right service: a series title belongs to
                # Sonarr even when the folder was listed through Radarr.
                "kind": (item or {}).get("_kind", arr.kind)}

        if item and better:
            findings.append(Finding(
                rule="unmatched_files", severity="warning", service=data["kind"],
                title=folder[:70], message="finding.unmatched_importable",
                params={"reason": reason, "comparison": comparison},
                data={**data, "item_id": item.get("id"), "todo": "import"}))
        elif item:
            findings.append(Finding(
                rule="unmatched_files", severity="info", service=data["kind"],
                title=folder[:70], message="finding.unmatched_no_gain",
                params={"reason": reason, "comparison": comparison},
                data={**data, "item_id": None, "todo": None}))
        else:
            giving_up = give_up > 0 and age >= give_up * 60
            history_id = None
            for entry in ctx.get("history", []):
                source_title = entry.get("sourceTitle") or ""
                if source_title and (source_title[:45] in folder
                                     or folder[:45] in source_title):
                    history_id = entry.get("movieId") or entry.get("seriesId")
                    break
            findings.append(Finding(
                rule="unmatched_files",
                severity="warning" if giving_up else "info",
                service=arr.kind, title=folder[:70],
                message=("finding.unmatched_giving_up" if giving_up
                         else "finding.unmatched_waiting"),
                params={"reason": reason or "no match", "hours": f"{age / 60:.1f}"},
                data={**data, "item_id": history_id,
                      "todo": "give_up" if (giving_up and history_id) else None}))
    return findings


def check_leftover_files(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """Leftover download debris: half RAR sets, remnants, dead folders.

    Deleting is irreversible, so ALL conditions have to hold at once before
    anything counts as debris:

      1. The folder sits below one of the configured directories. Nothing
         outside is ever touched — checked twice, here and immediately before
         deleting.
      2. Nothing inside has changed for ``trash_age_hours``.
      3. The download client no longer knows the name — neither in its queue nor
         in its recent history.
      4. No Arr queue mentions it.
      5. It holds no usable video file any more, or is explicitly marked
         ``_FAILED_``.

    **Condition 3 is the decisive one** and the reason the download client has
    to be configured: SABnzbd creates the folder when a download is queued, not
    when it starts. With a long queue a folder can sit untouched for hours and
    be perfectly fine — age alone would be a dangerous criterion.

    Verified in production: of 129 folders exactly two were removed.
    """
    paths = [p for p in (cfg.get("cleanup_paths") or []) if p]
    if not paths:
        return [Finding(
            rule="leftover_files", severity="info", service=arr.kind,
            title=t("finding.cleanup_suspended_title", "en"),
            message="finding.cleanup_no_paths")]

    age_threshold = float(cfg.get("trash_age_hours", 24))
    min_mb = float(cfg.get("trash_video_mb", 300))
    downloader_names: set[str] = ctx.get("downloader_names") or set()
    downloader_up = ctx.get("downloader_reachable")

    # Without word from the download client NOTHING is deleted. Neither when it
    # is not configured at all (None) nor when it does not answer (False).
    if downloader_up is not True:
        return [Finding(
            rule="leftover_files", severity="info", service=arr.kind,
            title=t("finding.cleanup_suspended_title", "en"),
            message=("finding.cleanup_no_downloader" if downloader_up is None
                     else "finding.cleanup_downloader_down"))]

    queue_names: set[str] = set()
    for entry in ctx.get("all_queues", []):
        for key in ("title", "outputPath", "downloadId"):
            if entry.get(key):
                queue_names.add(str(entry[key]).replace("\\", "/").rsplit("/", 1)[-1])

    findings = []
    for base in paths:
        if not os.path.isdir(base):
            continue
        real_base = os.path.realpath(base)
        try:
            entries = os.listdir(base)
        except OSError as e:
            log.warning("%s is not readable: %s", base, e)
            continue

        for name in entries:
            path = os.path.join(base, name)
            # (1) Never outside the configured directories, and never follow a
            # symlink out of them.
            if os.path.islink(path):
                continue
            if not os.path.realpath(path).startswith(real_base + os.sep):
                continue
            plain = name.replace("_UNPACK_", "").replace("_FAILED_", "")

            # (3)+(4) does anyone still know about it?
            if any(plain[:40] in n or n[:40] in plain
                   for n in (downloader_names | queue_names) if n):
                continue

            # (2) age: the most recent change anywhere inside
            newest, total_bytes, files = 0.0, 0, []
            try:
                if os.path.isfile(path):
                    newest, total_bytes = os.path.getmtime(path), os.path.getsize(path)
                    files = [name]
                else:
                    for folder, _, names in os.walk(path):
                        for file_name in names:
                            full = os.path.join(folder, file_name)
                            try:
                                newest = max(newest, os.path.getmtime(full))
                                total_bytes += os.path.getsize(full)
                                files.append(file_name)
                            except OSError:
                                pass
                    if not files:
                        newest = os.path.getmtime(path)
            except OSError:
                continue
            age_hours = (time.time() - newest) / 3600 if newest else 0
            # An empty folder is unambiguously debris and does not need the full
            # waiting time. Observed on a remnant SABnzbd left behind after a
            # blocklist: 0 MB, but 12 hours old.
            wait = float(cfg.get("trash_empty_hours", 2)) if not files else age_threshold
            if age_hours < wait:
                continue

            # (5) is there still a usable video in it?
            videos = [f for f in files if f.lower().endswith(VIDEO_SUFFIXES)]
            big_enough = total_bytes / 1024 ** 2 >= min_mb
            archives_only = bool(files) and all(
                f.lower().endswith(ARCHIVE_SUFFIXES) or f.startswith("__") for f in files)

            if "_FAILED_" in name:
                reason_key = "reason.marked_failed"
                reason_params: dict[str, Any] = {}
            elif not files:
                reason_key = "reason.empty_folder"
                reason_params = {}
            elif archives_only:
                reason_key = "reason.archives_only"
                reason_params = {"count": len(files)}
            elif videos and big_enough:
                continue               # usable video: hands off
            elif videos:
                reason_key = "reason.partial_video"
                reason_params = {"mb": f"{total_bytes / 1024 ** 2:.0f}"}
            else:
                reason_key = "reason.no_video"
                reason_params = {"count": len(files)}

            findings.append(Finding(
                rule="leftover_files", severity="warning", service=arr.kind,
                title=name[:70],
                message="finding.leftover_files",
                params={"reason": t(reason_key, "en", **reason_params),
                        "days": f"{age_hours / 24:.1f}",
                        "mb": f"{total_bytes / 1024 ** 2:.0f}"},
                data={"path": path, "mb": round(total_bytes / 1024 ** 2, 1),
                      "age_hours": round(age_hours, 1), "files": len(files),
                      "reason_key": reason_key, "reason_params": reason_params},
            ))
    return findings


# ===========================================================================
# Category: library
# ===========================================================================
def check_missing_audio_language(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """An existing file has none of the wanted audio languages.

    The most reliable language evidence there is: after the import the real
    audio tracks are read from the file. What shows up here genuinely lacks the
    language — unlike before the import, where only the file name is guessed at.

    Does nothing until ``audio_languages`` is set, because there is no sensible
    default: which languages matter is entirely up to whoever runs this.
    """
    wanted = [w.strip().lower() for w in (cfg.get("audio_languages") or "").split(",")
              if w.strip()]
    if not wanted:
        return []
    findings = []
    for item in ctx.get("items", []):
        file_info = item.get("movieFile")
        if not file_info:
            continue
        media_info = file_info.get("mediaInfo") or {}
        languages = (media_info.get("audioLanguages") or "").lower()
        if not languages:
            continue                                   # no data, no judgement
        if any(w in languages for w in wanted):
            continue
        findings.append(Finding(
            rule="missing_audio_language", severity="warning", service=arr.kind,
            title=item.get("title", "?"),
            message="finding.missing_audio_language",
            params={"found": media_info.get("audioLanguages"),
                    "wanted": ", ".join(wanted)},
            data={"item_id": item.get("id"),
                  "languages": media_info.get("audioLanguages"),
                  "file": file_info.get("relativePath")},
        ))
    return findings


def check_unreadable_file(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """The file could not be read — often a sign of damage."""
    findings = []
    for item in ctx.get("items", []):
        file_info = item.get("movieFile")
        if not file_info or file_info.get("mediaInfo"):
            continue
        findings.append(Finding(
            rule="unreadable_file", severity="error", service=arr.kind,
            title=item.get("title", "?"),
            message="finding.unreadable_file",
            data={"item_id": item.get("id"), "file": file_info.get("relativePath"),
                  "gb": round((file_info.get("size") or 0) / 1024 ** 3, 2)},
        ))
    return findings


def check_below_profile(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """The existing file would not be accepted under today's rules.

    Re-scores the release name of the file on disk against the profile. Below
    the blocking threshold, the file no longer matches what is being asked for —
    after a profile change, for instance.
    """
    profiles = {p["id"]: p for p in ctx["profiles"]}
    formats = {f["name"]: f for f in ctx["formats"]}
    findings = []
    for item in ctx.get("items", []):
        file_info = item.get("movieFile")
        profile = profiles.get(item.get("qualityProfileId"))
        if not file_info or not profile:
            continue
        name = file_info.get("sceneName") or file_info.get("relativePath") or ""
        if not name:
            continue
        synthetic = {"title": name, "size": file_info.get("size"),
                     "quality": file_info.get("quality"), "movie": item}
        total, hits = score(synthetic, profile, formats)
        if total > BLOCKED:
            continue
        blocking = [h for h in hits if "-999999" in h]
        # Leave pure size violations out: the lower bounds in a profile say what
        # should still be GRABBED — they are not a verdict on what is already
        # there. Measured against the owner's own library that would otherwise
        # be 170 of 189 findings, and the search for better copies is running
        # anyway.
        without_size = [h for h in blocking
                        if not re.search(r"(under|over)\s[\d.,]+\s?GB", h, re.IGNORECASE)]
        if not without_size:
            continue
        findings.append(Finding(
            rule="below_profile", severity="warning", service=arr.kind,
            title=item.get("title", "?"),
            message="finding.below_profile",
            params={"profile": profile.get("name"), "reason": ", ".join(without_size)},
            data={"item_id": item.get("id"), "file": name, "score": total, "hits": hits},
        ))
    return findings


def check_missing_items(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """Monitored, released, but no file — and never searched for.

    The two services answer this question with different things. Radarr hands
    back movies, where ``id`` is the movie. Sonarr hands back **episodes**,
    where ``id`` is the episode and the series is a field on it. Reading ``id``
    for both meant an episode id was passed to a series search: with luck a
    different series was searched for, without it the service refused the
    command outright. The episode ids travel with the finding instead, so the
    search can ask for exactly the episodes that are missing.
    """
    findings = []
    for item in ctx.get("missing", []):
        if arr.kind == "sonarr":
            series = item.get("series") or {}
            series_id = item.get("seriesId") or series.get("id")
            if not series_id:
                continue
            season = item.get("seasonNumber")
            number = item.get("episodeNumber")
            label = (f"S{int(season):02d}E{int(number):02d}"
                     if season is not None and number is not None else "")
            title = " ".join(x for x in (series.get("title") or "?", label) if x)
            data = {"item_id": series_id, "year": series.get("year"),
                    "episode_ids": [item["id"]] if item.get("id") else [],
                    "season": season}
        else:
            title = item.get("title") or "?"
            data = {"item_id": item.get("id"), "year": item.get("year")}
        findings.append(Finding(
            rule="missing_items", severity="info", service=arr.kind, title=title,
            message="finding.missing_items", data=data,
        ))
    return findings


# ===========================================================================
# Category: downloader
# ===========================================================================
def check_downloader_warning(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """The download client is reporting a warning.

    From production: "Importing 202 files from ... failed" — a broken NZB. The
    service searches again on its own, but the warning stays up until somebody
    clears it. That is exactly what this rule does after recording it: note it
    down first, then tidy up.
    """
    findings = []
    for client in ctx.get("downloaders", []):
        for warning in client.get("warnings", []):
            severity = ("error" if str(warning.get("kind", "")).upper()
                        in ("ERROR", "CRITICAL") else "warning")
            findings.append(Finding(
                rule="downloader_warning", severity=severity, service="sabnzbd",
                title=f"{client['name']}: {str(warning.get('text', ''))[:52]}",
                message="finding.passthrough",
                params={"text": str(warning.get("text", ""))[:400]},
                data={"client": client["name"], "source": warning.get("source"),
                      "kind": warning.get("kind")},
            ))
    return findings


def check_downloader_stale_entry(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """An entry whose file has long since landed in the library.

    After its own import the service cleans up by itself. After a manual import
    — by this tool, for instance — the download client never finds out: the
    entry and its source folder stay. Observed on Crank and Transporter, both of
    which were still in the history after the import.

    Detected by file size: a library file matching to within one percent means
    the job is done.

    Size alone is NOT enough though: several films happen to be the same size.
    An early draft therefore reported that Crank was "already present as
    Hangover 3". The title has to match as well.
    """
    threshold = float(cfg.get("downloader_done_hours", 1))
    candidates = ctx.get("match_candidates")
    findings = []

    library = []
    for item in ctx.get("items", []):
        file_info = item.get("movieFile")
        if file_info and file_info.get("size"):
            library.append((int(file_info["size"]), item))
    if not library:
        return []

    for client in ctx.get("downloaders", []):
        for entry in client.get("history", []):
            if str(entry.get("status", "")).lower() != "completed":
                continue
            completed = entry.get("completed") or 0
            if not completed or (time.time() - completed) / 3600 < threshold:
                continue
            size = int(entry.get("bytes") or 0)
            if not size:
                continue
            name = str(entry.get("name", ""))

            same_size = [item for s, item in library if abs(s - size) <= size * 0.01]
            if not same_size:
                continue
            hit = None
            if candidates:
                narrowed = [(item, spellings) for item, spellings in candidates
                            if any(item.get("id") == c.get("id") for c in same_size)]
                found, _, _ = match(name, narrowed or candidates,
                                    year_tolerance=int(cfg.get("year_tolerance", 1)))
                if found and any(found.get("id") == c.get("id") for c in same_size):
                    hit = found.get("title")
            if not hit:
                # Fallback: the library file name mentions the download
                for item in same_size:
                    file_info = item.get("movieFile") or {}
                    origin = file_info.get("sceneName") or file_info.get("originalFilePath") or ""
                    if origin and origin[:30] and origin[:30] in name:
                        hit = item.get("title")
                        break
            if not hit:
                continue
            findings.append(Finding(
                rule="downloader_stale_entry", severity="info", service="sabnzbd",
                title=name[:70],
                message="finding.downloader_stale_entry",
                params={"title": hit, "gb": round(size / 1024 ** 3, 1),
                        "client": client["name"]},
                data={"client": client["name"], "nzo_id": entry.get("nzo_id"),
                      "gb": round(size / 1024 ** 3, 1), "item": hit},
            ))
    return findings


def check_downloader_paused(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """The download client is stopped — then nothing moves at all.

    Easy to miss for a long time: grabbing continues, the queue grows, but
    nothing arrives.
    """
    findings = []
    for client in ctx.get("downloaders", []):
        status = client.get("status", {})
        if not (status.get("paused") or status.get("paused_all")):
            continue
        findings.append(Finding(
            rule="downloader_paused", severity="error", service="sabnzbd",
            title=client["name"],
            message="finding.downloader_paused",
            params={"waiting": client.get("waiting", 0)},
            data={"client": client["name"], "waiting": client.get("waiting", 0)},
        ))
    return findings


def check_downloader_disk_space(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """The download client is reporting little free space."""
    threshold = float(cfg.get("disk_threshold_gb", 250))
    findings = []
    for client in ctx.get("downloaders", []):
        status = client.get("status", {})
        for field_name, label_key in (("diskspace1", "label.working_folder"),
                                      ("diskspace2", "label.finished_folder")):
            try:
                free = float(status.get(field_name))
            except (TypeError, ValueError):
                continue
            if free >= threshold:
                continue
            findings.append(Finding(
                rule="downloader_disk_space",
                severity="error" if free < threshold / 2 else "warning",
                service="sabnzbd",
                title=f"{client['name']}: {t(label_key, 'en')}",
                message="finding.disk_low",
                params={"free": f"{free:.0f}"},
                data={"client": client["name"], "folder": label_key,
                      "free_gb": round(free)},
            ))
    return findings


def check_downloader_update(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """A new version of the download client is available."""
    findings = []
    for client in ctx.get("downloaders", []):
        status = client.get("status", {})
        available = status.get("new_release")
        if not available:
            continue
        findings.append(Finding(
            rule="downloader_update", severity="info", service="sabnzbd",
            title=f"{client['name']} {available}",
            message="finding.downloader_update",
            params={"current": status.get("version", "?"), "available": available},
            data={"client": client["name"], "available": available,
                  "url": status.get("new_rel_url")},
        ))
    return findings


# ===========================================================================
# Category: indexers
# ===========================================================================
def check_indexer_disabled(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """Prowlarr has temporarily switched an indexer off because of errors.

    Easy to miss, but it takes effect immediately: every search reaches one
    source fewer.
    """
    findings = []
    for group in ctx.get("indexer_state", []):
        for status in group.get("statuses", []):
            if not status.get("disabledTill"):
                continue
            name = next((i.get("name") for i in group.get("indexers", [])
                         if i.get("id") == status.get("indexerId")),
                        f"ID {status.get('indexerId')}")
            findings.append(Finding(
                rule="indexer_disabled", severity="error", service="prowlarr",
                title=name,
                message="finding.indexer_disabled",
                params={"until": str(status.get("disabledTill"))[:19],
                        "last_error": str(status.get("mostRecentFailure"))[:19]},
                data={"indexer": name, "until": status.get("disabledTill")},
            ))
    return findings


def check_indexer_ineffective(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """An indexer is queried diligently and delivers practically nothing.

    Costs time on every search and counts against the provider's daily limit
    without ever contributing. Only reported — it may cover a niche that is
    rare but important.
    """
    minimum = int(cfg.get("indexer_min_queries", 200))
    quota = float(cfg.get("indexer_min_yield", 0.2))
    findings = []
    for group in ctx.get("indexer_state", []):
        for view in group.get("views", []):
            if view.queries < minimum:
                continue
            percent = 100 * view.grabs / view.queries
            if percent >= quota:
                continue
            findings.append(Finding(
                rule="indexer_ineffective", severity="warning", service="prowlarr",
                title=view.name,
                message="finding.indexer_ineffective",
                params={"queries": view.queries, "grabs": view.grabs,
                        "percent": f"{percent:.1f}"},
                data={"indexer": view.name, "queries": view.queries,
                      "grabs": view.grabs, "yield": round(percent, 2),
                      "rating": view.rating},
            ))
    return findings


def check_indexer_ranking(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """The configured order contradicts the measured usefulness.

    What is compared is the RANK, not the absolute number: if the best indexer
    sits at priority 1 there is nothing to say — whether that number reads 1, 5
    or 10. Only reported from several places of deviation; with a tolerance of 1
    five of six indexers would have come up in testing, which is noise.

    Deliberately only a suggestion: a wrongly set priority is easy to make and
    hard to notice later.
    """
    tolerance = int(cfg.get("indexer_rank_tolerance", 2))
    findings = []
    for group in ctx.get("indexer_state", []):
        for deviation in rank_deviation(group.get("views", []), tolerance):
            view = deviation["view"]
            findings.append(Finding(
                rule="indexer_ranking", severity="info", service="prowlarr",
                title=view.name,
                message="finding.indexer_ranking",
                params={"places": deviation["places"],
                        "direction": t(f"direction.{deviation['direction']}", "en"),
                        "actual": deviation["actual_rank"],
                        "target": deviation["target_rank"],
                        "rating": view.rating,
                        "actual_priority": deviation["actual_priority"],
                        "suggested": deviation["suggested_priority"]},
                data={"indexer": view.name,
                      "actual_priority": deviation["actual_priority"],
                      "suggested_priority": deviation["suggested_priority"],
                      "rating": view.rating, "parts": view.parts},
            ))
    return findings


def check_indexer_unknown(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """An indexer shows up in the history that Prowlarr does not know.

    Either configured directly in the Arr service instead of through Prowlarr,
    or removed since. In the first case it escapes central management.
    """
    findings = []
    for group in ctx.get("indexer_state", []):
        for view in group.get("views", []):
            if UNKNOWN_TO_PROWLARR not in view.notes:
                continue
            findings.append(Finding(
                rule="indexer_unknown", severity="info", service="prowlarr",
                title=view.name,
                message="finding.indexer_unknown",
                params={"score": f"{view.mean_score:.0f}"},
                data={"indexer": view.name, "score": round(view.mean_score)},
            ))
    return findings


# ===========================================================================
# Category: system
# ===========================================================================
def check_api_changes(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """The service has marked part of the API this uses as replaced.

    This is the only advance warning these services give. When a response comes
    from something that is on its way out, it says so in a header — and then
    keeps working, sometimes for a release or two, until one day it does not.

    Nobody reads headers, so it is surfaced here instead: as an ordinary
    finding, months before anything actually breaks, naming exactly which call
    needs attention. It is reporting only and always will be; there is nothing
    to fix at this end except the code.
    """
    findings = []
    for path, count in sorted(arr.observed.deprecated.items()):
        findings.append(Finding(
            rule="api_changes", severity="info", service=arr.kind,
            title=f"{arr.name}: {path}",
            message="finding.api_changes",
            params={"path": path, "version": arr.observed.version or "?"},
            data={"path": path, "count": count,
                  "version": arr.observed.version},
        ))
    return findings


def check_service_health(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """The service is reporting a problem about itself."""
    return [Finding(
        rule="service_health",
        severity="error" if entry.get("type") == "error" else "info",
        service=arr.kind, title=f"{arr.name}: {entry.get('source', '?')}",
        message="finding.passthrough",
        params={"text": entry.get("message", "")},
        data={"wikiUrl": entry.get("wikiUrl"), "source": entry.get("source")},
    ) for entry in ctx.get("health", [])]


def _holds_a_root(disk_path: str, roots: set[str]) -> bool:
    """Does this storage location hold any of the library folders?

    Not an equality test, because the two are not the same kind of thing. The
    services answer the disk question per **mount**, and a mount is usually a
    shorter path than the library folder sitting on it — often just ``/``. An
    earlier build compared the two strings directly, and on any install whose
    mounts were not spelled exactly like its root folders that silently
    discarded every row, which switched the whole rule off.
    """
    disk = (disk_path or "").rstrip("/")
    if not disk:
        return False
    for raw in roots:
        root = (raw or "").rstrip("/")
        if not root:
            continue
        if root == disk or root.startswith(disk + "/") or disk.startswith(root + "/"):
            return True
    return False


def check_disk_space(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """Free space on a storage location is running low."""
    threshold = float(cfg.get("disk_threshold_gb", 250))
    roots = {r.get("path") for r in ctx.get("root_folders", []) if r.get("path")}
    rows = ctx.get("disk_space", [])
    relevant = [e for e in rows if _holds_a_root(e.get("path", ""), roots)]
    # Nothing matched at all: report on everything rather than on nothing. A
    # warning about a drive that turns out not to matter is a small annoyance;
    # silence about the one that does is how a library stops being written.
    findings = []
    for entry in (relevant or rows):
        free = (entry.get("freeSpace") or 0) / 1024 ** 3
        if free >= threshold:
            continue
        total = (entry.get("totalSpace") or 1) / 1024 ** 3
        findings.append(Finding(
            rule="disk_space", severity="error" if free < threshold / 2 else "warning",
            service=arr.kind, title=entry.get("path", "?"),
            message="finding.disk_space",
            params={"free": f"{free:.0f}", "total": f"{total:.0f}"},
            data={"free_gb": round(free), "total_gb": round(total),
                  "path": entry.get("path")},
        ))
    return findings


# ===========================================================================
ALL: tuple[Rule, ...] = (
    # -- queue --------------------------------------------------------------
    Rule("wrong_year", "queue", check_wrong_year,
         actions=(policy.REPORT, "remove", "blocklist", "blocklist_and_search"),
         default_action="blocklist_and_search",
         conditions=("max_gb", "min_confidence")),
    Rule("wrong_title", "queue", check_wrong_title,
         actions=(policy.REPORT, "remove", "blocklist", "blocklist_and_search"),
         conditions=("max_gb", "min_confidence"), deep=True),
    Rule("profile_violation", "queue", check_profile_violation,
         actions=(policy.REPORT, "remove", "blocklist", "blocklist_and_search"),
         default_action="blocklist_and_search", conditions=("max_gb",)),
    Rule("not_an_upgrade", "queue", check_not_an_upgrade,
         # No search afterwards: the file already on disk is the better one.
         actions=(policy.REPORT, "remove", "blocklist"),
         default_action="blocklist", conditions=("max_gb",)),
    Rule("stalled", "queue", check_stalled,
         actions=(policy.REPORT, "remove", "blocklist", "blocklist_and_search"),
         conditions=("min_age_hours", "max_gb")),
    # Nothing to do here on purpose: the cause is in the profile, and no
    # action taken on the queue can fix that.
    Rule("grab_loop", "queue", check_grab_loop, deep=True),

    # -- import -------------------------------------------------------------
    Rule("manual_import", "import", check_manual_import,
         actions=(policy.REPORT, "import"), default_action="import"),
    Rule("unpack_failed", "import", check_unpack_failed,
         actions=(policy.REPORT, "delete"),
         conditions=("min_age_hours", "max_gb"), scope="once"),
    Rule("detached_folder", "import", check_detached_folder, scope="once"),
    Rule("leftover_files", "import", check_leftover_files,
         actions=(policy.REPORT, "delete"), default_action="delete",
         conditions=("min_age_hours", "max_gb"), scope="once"),
    Rule("unmatched_files", "import", check_unmatched_files,
         actions=(policy.REPORT, "import", "import_and_clean",
                  "blocklist_and_search"),
         default_action="import_and_clean", scope="once",
         conditions=("min_age_hours", "max_gb", "min_confidence")),

    # -- library ------------------------------------------------------------
    Rule("missing_audio_language", "library", check_missing_audio_language,
         actions=(policy.REPORT, "search"), only_kinds=("radarr",), deep=True),
    Rule("unreadable_file", "library", check_unreadable_file,
         actions=(policy.REPORT, "refresh", "search"), conditions=("max_gb",),
         only_kinds=("radarr",), deep=True),
    Rule("below_profile", "library", check_below_profile,
         actions=(policy.REPORT, "search"), only_kinds=("radarr",), deep=True),
    Rule("missing_items", "library", check_missing_items,
         actions=(policy.REPORT, "search"), deep=True),

    # -- downloader ---------------------------------------------------------
    Rule("downloader_warning", "downloader", check_downloader_warning,
         actions=(policy.REPORT, "clear_warning"),
         default_action="clear_warning", scope="once"),
    Rule("downloader_stale_entry", "downloader", check_downloader_stale_entry,
         actions=(policy.REPORT, "remove_entry"),
         default_action="remove_entry", conditions=("max_gb",), scope="once"),
    # Deliberately reporting by default: a pause is usually somebody's
    # decision, and overruling it unasked would be presumptuous.
    Rule("downloader_paused", "downloader", check_downloader_paused,
         actions=(policy.REPORT, "resume"), scope="once"),
    Rule("downloader_disk_space", "downloader", check_downloader_disk_space,
         scope="once"),
    Rule("downloader_update", "downloader", check_downloader_update, scope="once"),

    # -- indexers -----------------------------------------------------------
    Rule("indexer_disabled", "indexers", check_indexer_disabled,
         scope="once", deep=True),
    Rule("indexer_ineffective", "indexers", check_indexer_ineffective,
         scope="once", deep=True),
    Rule("indexer_ranking", "indexers", check_indexer_ranking,
         scope="once", deep=True),
    Rule("indexer_unknown", "indexers", check_indexer_unknown,
         scope="once", deep=True),

    # -- system -------------------------------------------------------------
    Rule("service_health", "system", check_service_health),
    Rule("disk_space", "system", check_disk_space),
    # Reporting only on purpose, and permanently: the thing that needs
    # changing when this fires is this program, not anything on the server.
    Rule("api_changes", "system", check_api_changes, deep=True),
)

BY_NAME: dict[str, Rule] = {r.name: r for r in ALL}
