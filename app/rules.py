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
import zlib
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from . import compat, languages, packs, policy, years
from .arr import Arr
from .i18n import t
from .indexers import UNKNOWN_TO_PROWLARR, rank_deviation
from .matching import match
from .scoring import matched, refused, score

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
    #: What those conditions are set to on a fresh install. Most rules want
    #: nothing, which is why this is usually empty — but a rule that throws a
    #: download away needs to be sure before it does, and "sure" is a number.
    #: Leaving it to the person means it is zero until somebody thinks to
    #: change it, which is the wrong way round for a default that deletes.
    default_conditions: dict[str, float] = field(default_factory=dict)
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


EPISODE_MARKER = years.EPISODE_MARKER


def _library_files(arr: Arr, ctx: dict):
    """Every file in the library, as ``(item, file, label)``.

    The two services do not hold this the same way and the difference is not
    cosmetic. A movie carries its one file on itself, so Radarr answers the
    whole question in the call that fetches the library. A series has many
    files and carries none of them, so they arrive from a separate call.

    Three library rules were written against ``movieFile`` and were therefore
    marked as applying to Radarr only — not because the question does not
    apply to a series, but because nothing had fetched the answer. This is
    where that stops.

    What decides is the shape of the data, not ``arr.kind``. The shared state
    holds the libraries of every service at once, so a list that is all one
    kind is the exception there rather than the rule, and a helper that picks
    a branch from the connection it was handed would answer for one of them
    and stay silent about the rest.
    """
    by_series: dict[int, list[dict]] = {}
    for info in ctx.get("files", []):
        series_id = info.get("seriesId")
        if series_id:
            by_series.setdefault(series_id, []).append(info)

    for item in ctx.get("items", []):
        movie_file = item.get("movieFile")
        if movie_file:
            yield item, movie_file, item.get("title", "?")
            continue
        for info in by_series.get(item.get("id"), []):
            yield item, info, f"{item.get('title', '?')} — {_file_label(info)}"


def _file_label(info: dict) -> str:
    """Something short that says which file this is."""
    name = (info.get("relativePath") or info.get("path") or "").replace("\\", "/")
    if name:
        return name.rsplit("/", 1)[-1][:60]
    season = info.get("seasonNumber")
    return f"Season {season}" if season is not None else "?"


def searching_for(arr: Arr, rule: str, item_id, season=None) -> str:
    """The key under which attempts at one title are remembered."""
    scope = f"{getattr(arr, 'service_id', None) or arr.kind}"
    tail = "" if season is None else f":s{season}"
    return f"{scope}:{rule}:{item_id}{tail}"


def _settled(ctx: dict, key: str, cfg: dict) -> dict | None:
    """Has this already been searched for, often enough, without result?

    Nothing has to tell this module whether a search helped. The full pass
    answers it for free: a title that was searched for last time and is *still*
    on the list is a title the search did not help. After a few of those there
    is a conclusion to draw — there is no better copy out there — and going on
    reporting it as something to be done about is how a page of findings stops
    being read.

    The judgement is never silent. The finding is still reported, it simply
    says what it now knows and stops being acted on by itself.
    """
    store = ctx.get("store")
    if store is None:
        return None
    limit = int(cfg.get("search_attempts", 3))
    if limit <= 0:
        return None
    record = store.attempt(key)
    if not record:
        return None
    # The tries have to be spread out as well as numerous. Three searches in
    # three days are evidence that nothing is out there; three searches in
    # one minute are three presses of a button. Rows written before this was
    # checked carry exactly that — seventy-one tries inside forty-five
    # seconds — so the span is judged here rather than trusted from the
    # count, and a verdict that does not survive it is taken back.
    tries = int(record.get("tries") or 0)
    span = _hours_between(record.get("first_try"), record.get("last_try"))
    enough = tries >= limit and span >= (limit - 1) * SEARCH_GAP_HOURS
    if enough and not record.get("settled"):
        store.settle(key)
        record = {**record, "settled": 1}
    elif not enough and record.get("settled"):
        store.settle(key, False)
        record = {**record, "settled": 0}
    return record


#: How far apart two searches have to be before they count as two. A full
#: pass runs every ninety minutes by default, so this is several of them: the
#: point is several separate occasions on which the answer was the same.
SEARCH_GAP_HOURS = 12.0


def _hours_between(first, last) -> float:
    try:
        a = datetime.fromisoformat(str(first).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return abs((b - a).total_seconds()) / 3600


def _settled_bits(record: dict | None) -> tuple[bool, dict, dict]:
    """``(settled, extra params, extra data)`` for a finding."""
    if not record or not record.get("settled"):
        return False, {}, {}
    tries = int(record.get("tries") or 0)
    since = str(record.get("first_try") or "")[:10]
    return True, {"tries": tries, "since": since}, {
        "settled": True, "tries": tries, "searched_since": since}


def quality_of(row: dict) -> str:
    """What is on disk, as the service names it."""
    info = row.get("movieFile") or row.get("episodeFile") or {}
    return ((info.get("quality") or {}).get("quality") or {}).get("name") or ""


def cutoff_of(profile: dict) -> str:
    """The quality a profile stops upgrading at, by name.

    ``cutoff`` is an id, and it can name either a single quality or a group of
    them. Both shapes live in ``items``, so both are looked for — a message
    that says "below the quality the profile asks for" without saying which
    quality tells nobody anything they did not already suspect.
    """
    wanted = profile.get("cutoff")
    if wanted is None:
        return ""
    for entry in (profile.get("items") or []):
        if entry.get("id") == wanted and entry.get("name"):
            # A group is named by what is in it. Profile managers name the
            # group after the profile, so "the profile 1080p Heimkino would
            # like 1080p Heimkino" was the whole of the explanation.
            members = [str((child.get("quality") or {}).get("name"))
                       for child in (entry.get("items") or [])
                       if (child.get("quality") or {}).get("name")]
            allowed = [str((child.get("quality") or {}).get("name"))
                       for child in (entry.get("items") or [])
                       if child.get("allowed")
                       and (child.get("quality") or {}).get("name")]
            chosen = allowed or members
            return " / ".join(chosen) if chosen else str(entry["name"])
        quality = entry.get("quality") or {}
        if quality.get("id") == wanted:
            return str(quality.get("name") or "")
        for nested in (entry.get("items") or []):
            inner = nested.get("quality") or nested
            if inner.get("id") == wanted:
                return str(inner.get("name") or "")
    return ""


def _season_groups(rows: list[dict]) -> dict[tuple, dict]:
    """Collect episodes into the season they belong to.

    Twenty episodes of one season below the cutoff are one thing that went
    wrong, not twenty. Reported one by one they fill the page with identical
    sentences and bury everything else — which is exactly what they did.
    """
    groups: dict[tuple, dict] = {}
    for row in rows:
        series = row.get("series") or {}
        series_id = row.get("seriesId") or series.get("id")
        if not series_id:
            continue
        season = row.get("seasonNumber")
        group = groups.setdefault((series_id, season), {
            "series": series, "series_id": series_id, "season": season,
            "episode_ids": [], "numbers": [], "qualities": []})
        if not group["series"] and series:
            group["series"] = series
        if row.get("id"):
            group["episode_ids"].append(row["id"])
        if row.get("episodeNumber") is not None:
            group["numbers"].append(row["episodeNumber"])
        found = quality_of(row)
        if found and found not in group["qualities"]:
            group["qualities"].append(found)
    return groups


def _episode_span(numbers: list[int]) -> str:
    """"E02–E07" for a run, "E02, E05, E09" for a handful, a count beyond that."""
    if not numbers:
        return ""
    ordered = sorted(numbers)
    if len(ordered) > 1 and ordered[-1] - ordered[0] == len(ordered) - 1:
        return f"E{ordered[0]:02d}–E{ordered[-1]:02d}"
    if len(ordered) <= 4:
        return ", ".join(f"E{n:02d}" for n in ordered)
    return f"E{ordered[0]:02d}–E{ordered[-1]:02d}"


def _wanted_entry(arr: Arr, item: dict) -> tuple[int | None, str, dict]:
    """Read one row of "missing" or "below the cutoff".

    Radarr answers both with movies, where ``id`` is the movie. Sonarr answers
    with **episodes**, where ``id`` is the episode and the series is a field on
    it. The two are not interchangeable and handing one to the other is how an
    episode id ended up in a series search.
    """
    if arr.kind != "sonarr":
        return item.get("id"), item.get("title") or "?", {"year": item.get("year")}
    series = item.get("series") or {}
    series_id = item.get("seriesId") or series.get("id")
    season, number = item.get("seasonNumber"), item.get("episodeNumber")
    label = (f"S{int(season):02d}E{int(number):02d}"
             if season is not None and number is not None else "")
    title = " ".join(x for x in (series.get("title") or "?", label) if x)
    return series_id, title, {"year": series.get("year"), "season": season,
                              "episode_ids": [item["id"]] if item.get("id") else []}


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


def _not_an_upgrade(entry: dict) -> bool:
    """Is this entry held back because the file would be no improvement?

    Both waiting states. The services park it in ``importPending`` and retry,
    or give up and call it ``importBlocked`` — which of the two depends on the
    version, and a rule that only knew the first let the second sit there for
    good.
    """
    return (entry.get("trackedDownloadState") in ("importPending", "importBlocked")
            and "upgrade" in _messages(entry).lower())


def check_not_an_upgrade(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """The import would not be an upgrade, so it sits there forever."""
    findings = []
    for entry in ctx["queue"]:
        if not _not_an_upgrade(entry):
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


def _first_available(entry: dict) -> str | None:
    """The earliest moment this entry's content existed anywhere.

    Read off the item the service already sent with the queue entry: the
    cinema, digital and disc dates for a film, the broadcast date for an
    episode. Missing dates are not treated as "not out" — plenty of items
    carry none, and a rule that reads silence as evidence finds only noise.
    """
    episode = entry.get("episode") or {}
    if episode.get("airDateUtc"):
        return str(episode["airDateUtc"])
    item = _item(entry)
    dates = [str(item[key]) for key in
             ("inCinemas", "digitalRelease", "physicalRelease", "firstAired")
             if item.get(key)]
    return min(dates) if dates else None


def check_premature_grab(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """A release for something that has not come out yet.

    There is no honest copy of a film that is still three weeks from its
    cinema date, and no honest copy of an episode that has not aired. What
    turns up under those names is a re-encode of a trailer, a different film
    with the right name on it, or nothing at all inside a working archive.

    A day of grace on purpose: release dates are held without a time zone, the
    world does not release things at midnight UTC, and something that came out
    this morning is not a fake. Reporting by default — a legitimate early
    release in one region is rare but real, and the safe direction is to say so
    rather than to act.
    """
    grace = float(cfg.get("premature_grace_hours", 24))
    findings = []
    for entry in ctx["queue"]:
        moment = _first_available(entry)
        if not moment:
            continue
        minutes = _age_minutes(moment)
        if minutes is None or minutes > -grace * 60:
            continue
        days = round(-minutes / 1440, 1)
        item = _item(entry)
        findings.append(Finding(
            rule="premature_grab", severity="error", service=arr.kind,
            entry_id=entry["id"], title=item.get("title", "?"),
            message="finding.premature_grab",
            params={"days": f"{days:g}", "date": moment[:10]},
            data={"release": entry.get("title"), "available": moment,
                  "days_early": days, "gb": _gb(entry),
                  # Three weeks out is a judgement call, three months is not.
                  "confidence": round(min(1.0, days / 21), 3)},
        ))
    return findings


def check_collection_pack(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """The release is a box set, and one film was asked for.

    ``Transformers.2007-2018.COMPLETE.UHD.BluRay.2160p`` against a request for
    *Transformers* (2007): sixty-eight gigabytes of which one film is wanted.

    Nothing else catches it. The year check cannot — 2007 is in the name and
    2007 is the right year, so as far as it is concerned the release agrees
    with the film. The title check cannot either, because the name does start
    with the title; it just carries on and names four more. The service
    downloads the lot and then fails to import it, or imports the wrong one.

    The judgement lives in :mod:`app.packs`, which measures every signal
    against the item's own title so that *Blade Runner 2049* is not accused of
    being a box set for having a year in its name, and a film actually called
    *The Collection* is not accused for being called that.

    Acting by default, because the alternative is watching sixty-eight
    gigabytes arrive and then throwing them away by hand — but only above a
    confidence, which is set for a fresh install rather than left at zero.
    """
    findings = []
    for entry in ctx["queue"]:
        item = _item(entry)
        release = entry.get("title") or ""
        verdict = packs.judge(release, item)
        if not verdict.is_pack:
            continue
        findings.append(Finding(
            rule="collection_pack", severity="error", service=arr.kind,
            entry_id=entry["id"], title=item.get("title", "?"),
            message=("finding.collection_pack" if verdict.span
                     else "finding.collection_pack_no_span"),
            params={"reason": ", ".join(t(key, "en") for key in verdict.reasons),
                    "span": verdict.span,
                    "gb": f"{_gb(entry):.1f}"},
            data={"release": release, "confidence": verdict.confidence,
                  "reasons": list(verdict.reasons), "span": verdict.span,
                  "item_id": item.get("id"), "gb": _gb(entry)},
        ))
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


# ---------------------------------------------------------------------------
# Stuck in the queue — the net under every other queue rule
# ---------------------------------------------------------------------------
#: Tracked states that mean the service has stopped moving this on by itself,
#: or is waiting on something. ``downloading``, ``imported`` and ``ignored``
#: are not among them: the first is somebody else's question (``stalled``),
#: the other two are finished.
STUCK_STATES = frozenset({"importBlocked", "importPending", "importing",
                          "failedPending", "failed"})

#: States the service leaves on its own within a minute or two when all is
#: well. Only time spent *observed* in them counts. Everything else is final:
#: the service has said it will not go on, and how long ago the download was
#: added is as good a measure of how long it has been waiting as any.
_SELF_RESOLVING = frozenset({"client_unavailable", "failed_pending",
                             "importing", "import_pending", "tba_title",
                             "path_problem", "other"})

#: The texts the download client writes when a job is beyond saving. They
#: arrive in the queue entry's ``errorMessage``, word for word as SABnzbd put
#: them. Checked *after* the disk and path wording on purpose: "Unpacking
#: failed, write error or disk is full?" is not a broken release, and
#: blocklisting it would throw away a good one because a disk ran full.
_DEAD_DOWNLOAD = ("aborted", "cannot be completed", "out of retention",
                  "missing articles", "not complete", "password", "encrypted",
                  "unpacking failed", "crc", "repair failed", "failed to repair",
                  "download failed", "unwanted extension")

_PATH_PROBLEM = ("not a valid local path", "remote path mapping",
                 "does not exist or is not accessible", "access to the path",
                 "is denied", "permission", "intermediate path",
                 "destination already exists", "disk is full", "no space",
                 "not enough free space", "read-only file system")

#: Cause → (what to suggest, whether that is certain enough to do unasked).
#: "Certain" is kept narrow on purpose. A download the service or the client
#: has declared dead can only be replaced; a file that is already in the
#: library can only be tidied away. Anything that brings a file *into* the
#: library on weaker evidence than the service itself had is offered, never
#: done — that is exactly the judgement the service declined to make.
STUCK_CAUSES: dict[str, tuple[str, bool]] = {
    "client_unavailable": (policy.REPORT, False),
    "not_grabbed":        (policy.REPORT, False),
    "path_problem":       (policy.REPORT, False),
    "failed":             ("remove", True),
    "failed_pending":     ("blocklist_and_search", True),
    "already_imported":   ("remove", True),
    "not_an_upgrade":     ("blocklist", True),
    "no_files":           ("blocklist_and_search", True),
    "sample":             ("blocklist_and_search", False),
    "matched_by_id":      ("import", False),
    "unknown_item":       ("import", False),
    "missing_episodes":   ("import", False),
    "tba_title":          ("import", False),
    "manual":             ("import", False),
    "import_pending":     ("import", False),
    "importing":          (policy.REPORT, False),
    "other":              (policy.REPORT, False),
}


def stuck_cause(entry: dict) -> str:
    """Why this queue entry is not moving, read off what the service says.

    The order matters, and each step is there because of a case that would be
    mishandled one step later:

    * an unreachable download client first — nothing about the download is
      known while it cannot be asked, so nothing is to be done to it;
    * "wasn't grabbed by Radarr" — somebody else's download, not ours to touch;
    * disk and path trouble before any verdict on the download itself, because
      the client describes a full disk as a failed unpack;
    * the failure states, then the rejections of an import attempt, most
      specific first: "Not a Custom Format upgrade" is about upgrades, not
      about custom formats.
    """
    text = _messages(entry).lower()
    status = str(entry.get("status") or "").lower()
    state = entry.get("trackedDownloadState") or ""
    if status == "downloadclientunavailable" or "client not available" in text \
            or "client is unavailable" in text:
        return "client_unavailable"
    if "wasn't grabbed by" in text or "was not grabbed by" in text:
        return "not_grabbed"
    if any(phrase in text for phrase in _PATH_PROBLEM):
        return "path_problem"
    if state == "failed":
        return "failed"
    if state == "failedPending" or status == "failed" \
            or any(phrase in text for phrase in _DEAD_DOWNLOAD):
        return "failed_pending"
    if "already imported" in text or "has already been imported" in text:
        return "already_imported"
    if "upgrade" in text:
        return "not_an_upgrade"
    if "sample" in text:
        return "sample"
    if "no files found are eligible" in text:
        return "no_files"
    if "matched to movie by id" in text or "matched to series by id" in text \
            or "via grab history" in text:
        return "matched_by_id"
    if ("title mismatch" in text or "unable to parse" in text
            or "unknown movie" in text or "unknown series" in text
            or "unable to identify" in text or not _item(entry).get("id")):
        return "unknown_item"
    if "tba title" in text:
        return "tba_title"
    if "episodes expected" in text or "missing from the release" in text \
            or "were not imported or missing" in text:
        return "missing_episodes"
    if "manual import" in text or "automatic import is not possible" in text:
        return "manual"
    if state == "importing":
        return "importing"
    if state == "importPending":
        return "import_pending"
    return "other"


def _stuck_candidate(entry: dict) -> bool:
    """Is this entry in a state that can be stuck at all?"""
    if entry.get("trackedDownloadState") in STUCK_STATES:
        return True
    if str(entry.get("trackedDownloadStatus") or "").lower() in ("warning", "error"):
        return True
    return str(entry.get("status") or "").lower() in (
        "failed", "warning", "downloadclientunavailable")


def _service_texts(entry: dict) -> list[str]:
    """What the service itself says, one line each, without repeats."""
    out: list[str] = []
    for part in (entry.get("statusMessages") or []):
        for line in (part.get("messages") or []):
            if line and line not in out:
                out.append(str(line)[:300])
    if entry.get("errorMessage") and entry["errorMessage"] not in out:
        out.append(str(entry["errorMessage"])[:300])
    return out[:8]


def _enabled(cfg: dict, name: str) -> bool:
    return bool(((cfg.get("rules") or {}).get(name) or {}).get("enabled", True))


def check_stuck_in_queue(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """A queue entry that has sat in a waiting or failed state for hours.

    The net under every other queue rule. Each of those knows one situation
    and knows it well; this one exists for everything they do not know, and
    for the cases where they decline — a release matched to its film by ID,
    say, with a name that does not quite agree, which ``manual_import`` rightly
    will not import unasked and which then sat there for two days with nobody
    being told. Nothing in the queue that the service has flagged is allowed
    to wait unnoticed any more.

    What to do is read off the service's own words (:func:`stuck_cause`) and
    stored on the finding as ``suggested``, together with the words themselves.
    Only the causes whose remedy is certain are acted on unasked; everything
    else is held back with the suggestion on it, one button away.

    An entry another rule is already dealing with is left to that rule, so the
    same download is not blocklisted by one rule and imported by another.
    """
    threshold = float(cfg.get("stuck_hours", 3))
    store = ctx.get("store")
    scope = f"stuck:{getattr(arr, 'service_id', None) or arr.kind}:"
    findings, active = [], set()
    for entry in ctx.get("queue", []):
        if not _stuck_candidate(entry):
            continue
        cause = stuck_cause(entry)
        if cause == "not_an_upgrade" and _enabled(cfg, "not_an_upgrade") \
                and _not_an_upgrade(entry):
            continue
        if _asks_for_manual_import(entry) and _enabled(cfg, "manual_import") \
                and _manual_import_fits(entry, cfg):
            continue

        # How long it has sat in exactly this state. The store remembers a
        # fingerprint of state and messages; it restarts the clock the moment
        # either changes, so an entry that is making progress through the
        # states is never mistaken for one that is not.
        texts = _service_texts(entry)
        key = f"{scope}{entry.get('downloadId') or entry.get('id')}"
        active.add(key)
        fingerprint = zlib.crc32("|".join(
            [str(entry.get("status")), str(entry.get("trackedDownloadStatus")),
             str(entry.get("trackedDownloadState")), *texts]).encode("utf-8"))
        observed = store.check_progress(key, fingerprint) / 60 if store else 0.0
        suggested, certain = STUCK_CAUSES[cause]
        if cause == "not_an_upgrade" and not _enabled(cfg, "not_an_upgrade"):
            # Somebody switched the rule for this off. Saying so is still
            # right; doing its work behind its back is not.
            certain = False
        waited = observed
        if cause not in _SELF_RESOLVING and not certain:
            # Reported straight away rather than hours after an update: an
            # entry the service has given up on has been waiting since it
            # arrived, as far as anyone can tell. Only for reporting — what
            # is acted on unasked has to have been *seen* stuck that long.
            added = _age_minutes(entry.get("added"))
            if added is not None:
                waited = max(waited, added / 60)
        if waited < threshold:
            continue

        item = _item(entry)
        if suggested == "import" and not (item.get("id") and entry.get("downloadId")):
            # Nothing to import it into, or no way to name the download. The
            # service's own words are still worth passing on.
            suggested, certain = policy.REPORT, False
        data = {"release": entry.get("title"), "cause": cause,
                "suggested": suggested, "certain": certain,
                "messages": texts,
                "state": entry.get("trackedDownloadState"),
                "status": entry.get("status"),
                "tracked_status": entry.get("trackedDownloadStatus"),
                "downloadId": entry.get("downloadId"),
                "item_id": item.get("id"), "gb": _gb(entry),
                "age_hours": round(waited, 1)}
        if suggested != policy.REPORT and not certain:
            data["hold"] = "policy.needs_a_look"
        findings.append(Finding(
            rule="stuck_in_queue",
            severity="error" if cause.startswith("failed") else "warning",
            service=arr.kind, entry_id=entry.get("id"),
            title=item.get("title") or entry.get("title") or "?",
            message=f"finding.stuck.{cause}",
            params={"hours": f"{waited:.1f}",
                    "text": " / ".join(texts)[:400] or "—"},
            data=data))
    if store is not None:
        store.prune_progress(active, scope)
    return findings


# ===========================================================================
# Category: import
# ===========================================================================
#: How the services say "somebody has to import this by hand". Radarr ends its
#: sentences with "Manual Import required"; Sonarr says the same thing as
#: "Automatic import is not possible" and never uses the other phrase, so a
#: rule that only knew Radarr's wording could not fire for a series at all.
_MANUAL_IMPORT_WORDING = ("manual import", "automatic import is not possible",
                          "matched to movie by id", "matched to series by id")


def _asks_for_manual_import(entry: dict) -> bool:
    if entry.get("trackedDownloadState") != "importBlocked":
        return False
    text = _messages(entry).lower()
    return any(phrase in text for phrase in _MANUAL_IMPORT_WORDING)


def _known_titles(item: dict) -> list[str]:
    """Every name the service knows this title by.

    The alternate titles are the point. A German release of a film carries the
    German title, the service holds it as an alternate title, and comparing
    only against the original meant a release named exactly like one of the
    service's own spellings was judged "not similar enough" and left waiting.
    """
    names = [item.get("title") or "", item.get("originalTitle") or ""]
    for alternate in (item.get("alternateTitles") or []):
        if isinstance(alternate, dict) and alternate.get("title"):
            names.append(str(alternate["title"]))
    return [name for name in names if name]


def _manual_import_fits(entry: dict, cfg: dict) -> bool:
    """Do the year AND the title of the release both agree with the item?"""
    tolerance = int(cfg.get("year_tolerance", 1))
    threshold = float(cfg.get("title_similarity", 0.45))
    item = _item(entry)
    release = entry.get("title", "")
    if not item:
        return False
    # Importing without being asked needs the year to be stated AND to fit.
    # A name that mentions no year at all is not evidence of anything, and
    # this is the gate in front of an action, not a reason to report one.
    verdict = years.judge(release, item, tolerance)
    # A film release states the year it came out, and a name that states
    # none is no evidence of anything. An episode states an episode
    # instead, and demanding a year of it meant this rule could never fire
    # for a series at all — every episode sat waiting for somebody to press
    # the button by hand.
    if EPISODE_MARKER.search(release):
        year_ok = not verdict.wrong
    else:
        year_ok = bool(verdict.found) and not verdict.wrong
    title_ok = max((_similarity(release, name) for name in _known_titles(item)),
                   default=0.0) >= threshold
    return year_ok and title_ok


def check_manual_import(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """Manual work is demanded although title and year line up.

    Imported only when BOTH the year and the title similarity check out.
    Everything else belongs to ``wrong_year`` or ``wrong_title`` — this rule
    must never cement a wrong match.
    """
    findings = []
    for entry in ctx["queue"]:
        if not _asks_for_manual_import(entry) or not _manual_import_fits(entry, cfg):
            continue
        item = _item(entry)
        release = entry.get("title", "")
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

    Compared by language, not by text. The setting says "German"; Radarr
    writes the tracks as ``ger/eng``, or ``deu/eng`` for the same tracks muxed
    by something else. Compared as text, every German file in a German library
    lacked German — and "en" was found inside "french".
    """
    raw = [w.strip() for w in (cfg.get("audio_languages") or "").split(",")
           if w.strip()]
    wanted = {languages.key(w) for w in raw}
    if not wanted:
        return []
    findings = []
    for item, file_info, label in _library_files(arr, ctx):
        media_info = file_info.get("mediaInfo") or {}
        tracks = languages.keys(media_info.get("audioLanguages") or "")
        if not tracks:
            continue                    # no data, or undetermined: no judgement
        if wanted & set(tracks):
            continue
        _done, said, noted = _settled_bits(_settled(
            ctx, searching_for(arr, "missing_audio_language", item.get("id")),
            cfg))
        findings.append(Finding(
            rule="missing_audio_language", severity="warning", service=arr.kind,
            title=label,
            message="finding.missing_audio_language",
            params={"found": media_info.get("audioLanguages"),
                    "wanted": ", ".join(raw), **said},
            data={"item_id": item.get("id"),
                  "languages": media_info.get("audioLanguages"),
                  "file": file_info.get("relativePath"), **noted},
        ))
    return findings


def check_unreadable_file(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """The file could not be read — often a sign of damage.

    Only said when the service reads files at all. Analysing video files can
    be switched off in Radarr and Sonarr, and then no file carries media
    information — which read file by file is every file in the library
    reported as damaged, at the highest severity there is.
    """
    everything = list(_library_files(arr, ctx))
    if not any(file_info.get("mediaInfo") for _item, file_info, _label in everything):
        return []
    findings = []
    for item, file_info, label in everything:
        if file_info.get("mediaInfo"):
            continue
        findings.append(Finding(
            rule="unreadable_file", severity="error", service=arr.kind,
            title=label,
            message="finding.unreadable_file",
            data={"item_id": item.get("id"), "file": file_info.get("relativePath"),
                  "gb": round((file_info.get("size") or 0) / 1024 ** 3, 2)},
        ))
    return findings


def check_below_profile(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """The existing file would not be accepted under today's rules.

    Re-scores the release name of the file on disk against the profile. When
    it carries a format the profile refuses, the file no longer matches what
    is being asked for — after a profile change, for instance.

    A refusal is whatever no bonus can climb back over, not only -999999: the
    profiles built by this program refuse with a few thousand points, and were
    never looked at here.
    """
    profiles = {p["id"]: p for p in ctx["profiles"]}
    formats = {f["name"]: f for f in ctx["formats"]}
    refusals = {pid: refused(profile) for pid, profile in profiles.items()}
    findings = []
    for item, file_info, label in _library_files(arr, ctx):
        profile = profiles.get(item.get("qualityProfileId"))
        if not profile:
            continue
        name = file_info.get("sceneName") or file_info.get("relativePath") or ""
        if not name:
            continue
        synthetic = {"title": name, "size": file_info.get("size"),
                     "quality": file_info.get("quality"), "movie": item}
        found = matched(synthetic, profile, formats)
        no = refusals.get(item.get("qualityProfileId")) or set()
        blocking = [f"{n} ({w:+})" for n, w in found if n in no]
        if not blocking:
            continue
        total = sum(w for _n, w in found)
        hits = [f"{n} ({w:+})" for n, w in found]
        # Leave pure size violations out: the lower bounds in a profile say what
        # should still be GRABBED — they are not a verdict on what is already
        # there. Measured against the owner's own library that would otherwise
        # be 170 of 189 findings, and the search for better copies is running
        # anyway.
        without_size = [h for h in blocking
                        if not re.search(r"(under|over)\s[\d.,]+\s?GB", h, re.IGNORECASE)]
        if not without_size:
            continue
        _done, said, noted = _settled_bits(_settled(
            ctx, searching_for(arr, "below_profile", item.get("id")), cfg))
        findings.append(Finding(
            rule="below_profile", severity="warning", service=arr.kind,
            title=label,
            message="finding.below_profile",
            params={"profile": profile.get("name"),
                    "reason": ", ".join(without_size), **said},
            data={"item_id": item.get("id"), "file": name, "score": total,
                  "hits": hits, **noted},
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

    Episodes are reported per season rather than one at a time. Nine missing
    episodes of one season are one gap, and nine identical lines saying so
    bury everything else on the page.

    Only what is actually out. The services list every monitored title that
    has no file, including the ones that have not been released — measured on
    a live Radarr: *Avatar 5* (2031), *Avengers: Secret Wars* (2027) and a
    handful of films still in cinemas were all reported as missing, and
    searched for six times each. There is nothing to find for a film that
    does not exist yet, and every one of those searches costs an indexer
    query. Radarr says which ones are available; for an episode, the
    broadcast date says it.
    """
    rows = [row for row in ctx.get("missing", []) if _out_yet(row)]
    if arr.kind == "sonarr":
        return _season_findings(
            arr, rows, rule="missing_items",
            message="finding.missing_episodes", severity="info",
            ctx=ctx, cfg=cfg,
            settled_message="finding.missing_episodes_nothing_out_there")

    findings = []
    for item in rows:
        item_id, title, extra = _wanted_entry(arr, item)
        if not item_id:
            continue
        done, said, noted = _settled_bits(_settled(
            ctx, searching_for(arr, "missing_items", item_id), cfg))
        findings.append(Finding(
            rule="missing_items", severity="info", service=arr.kind, title=title,
            message="finding.missing_nothing_out_there" if done
                    else "finding.missing_items",
            params={"year": item.get("year") or "?", **said},
            data={"item_id": item_id, **extra, **noted},
        ))
    return findings


def _out_yet(row: dict) -> bool:
    """Has this been released, so that looking for it makes sense?

    Radarr answers this itself with ``isAvailable``, which takes the film's
    minimum availability setting into account — in cinemas, digitally, on
    disc. An episode has aired when its broadcast date has passed. Anything
    that says neither is given the benefit of the doubt, because a title that
    is reported when it should not be is a nuisance, and one that is never
    reported is a gap nobody knows about.
    """
    if row.get("isAvailable") is False:
        return False
    aired = row.get("airDateUtc")
    if aired:
        minutes = _age_minutes(aired)
        if minutes is not None and minutes < 0:
            return False
    return True


def _season_findings(arr: Arr, rows: list[dict], *, rule: str, message: str,
                     severity: str, extra_params=None, ctx=None,
                     cfg=None, settled_message: str = "") -> list[Finding]:
    """One finding per season, carrying every episode in it."""
    findings = []
    for group in _season_groups(rows).values():
        series = group["series"] or {}
        season = group["season"]
        label = f"Season {season}" if season is not None else "?"
        span = _episode_span(group["numbers"])
        params = {"count": len(group["episode_ids"]) or len(group["numbers"]),
                  "season": season if season is not None else "?",
                  "episodes": span or "—"}
        if extra_params:
            params.update(extra_params(group))

        done, said, noted = False, {}, {}
        if ctx is not None and settled_message:
            done, said, noted = _settled_bits(_settled(
                ctx, searching_for(arr, rule, group["series_id"], season),
                cfg or {}))
        findings.append(Finding(
            rule=rule, severity=severity, service=arr.kind,
            title=f"{series.get('title') or '?'} — {label}",
            message=settled_message if done else message,
            params={**params, **said},
            data={"item_id": group["series_id"], "season": season,
                  "episode_ids": group["episode_ids"],
                  "count": len(group["episode_ids"]),
                  "quality": ", ".join(group["qualities"]) or None,
                  "year": series.get("year"), **noted},
        ))
    return findings


def check_cutoff_unmet(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """There is a file, but it is below the quality the profile asks for.

    The services keep this list themselves and will act on it when asked — the
    problem is that nobody asks. A cutoff that is never met is invisible: the
    title looks complete in every view, it plays, and the only sign that
    something better was wanted is a number on a page nobody opens.

    Reporting by default. The action is a search, and a search for every title
    below its cutoff at once is a lot of queries — that should be somebody's
    decision, not a default.

    What it says matters as much as that it says it. "Below the quality the
    profile asks for" names neither quality, which leaves a reader with a page
    of identical sentences and nothing to decide from. Both ends are named
    here: what is on disk, and what the profile stops at.
    """
    profiles = {p["id"]: p for p in ctx.get("profiles", [])}
    cutoffs = {pid: cutoff_of(profile) for pid, profile in profiles.items()}

    def wanted_for(row: dict) -> tuple[str, str]:
        source = row.get("series") or row
        profile = profiles.get(source.get("qualityProfileId"))
        if not profile:
            return "", ""
        return (str(profile.get("name") or ""),
                cutoffs.get(source.get("qualityProfileId")) or "")

    if arr.kind == "sonarr":
        rows = ctx.get("below_cutoff", [])

        def season_extras(group: dict) -> dict:
            profile, cutoff = wanted_for({"series": group["series"]})
            return {"quality": ", ".join(group["qualities"]) or "?",
                    "profile": profile or "?", "cutoff": cutoff or "?"}

        return _season_findings(arr, rows, rule="cutoff_unmet",
                                message="finding.cutoff_unmet_season",
                                severity="info", extra_params=season_extras,
                                ctx=ctx, cfg=cfg,
                                settled_message="finding.cutoff_nothing_better")

    # Radarr sends the list of titles below their cutoff without the files —
    # asked for or not, measured against a live instance — so "on disk as ?"
    # was all it could say. The movie list the library rules read carries
    # every file, so the quality is looked up there.
    library = {i.get("id"): i for i in ctx.get("items", []) if i.get("id")}
    findings = []
    for item in ctx.get("below_cutoff", []):
        item_id, title, extra = _wanted_entry(arr, item)
        if not item_id:
            continue
        if not quality_of(item) and library.get(item_id):
            item = {**item, "movieFile": library[item_id].get("movieFile")}
        profile, cutoff = wanted_for(item)
        done, said, noted = _settled_bits(_settled(
            ctx, searching_for(arr, "cutoff_unmet", item_id), cfg))
        findings.append(Finding(
            rule="cutoff_unmet", severity="info", service=arr.kind, title=title,
            message="finding.cutoff_nothing_better" if done
                    else "finding.cutoff_unmet",
            params={"quality": quality_of(item) or "?",
                    "profile": profile or "?", "cutoff": cutoff or "?", **said},
            data={"item_id": item_id, "quality": quality_of(item),
                  "profile": profile, "cutoff": cutoff, **extra, **noted},
        ))
    return findings


def _seasons_of(item: dict) -> list[dict]:
    return [s for s in (item.get("seasons") or []) if isinstance(s, dict)]


def check_season_gaps(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """A season that is partly there.

    Distinct from "nothing has been downloaded yet", and the distinction is the
    whole point: a season with no files at all is simply on the list. A season
    with eight of ten episodes is a season pack that imported partly, or two
    episodes that failed months ago and were never noticed, and nothing about
    the usual views makes that visible — the series shows a tick, the season
    shows a number nobody reads.

    Costs nothing to ask: the per-season counts arrive with the series list
    that the library rules already fetch.
    """
    minimum = int(cfg.get("season_gap_min", 1))
    findings = []
    for item in ctx.get("items", []):
        if not item.get("monitored"):
            continue
        for season in _seasons_of(item):
            if not season.get("monitored"):
                continue
            stats = season.get("statistics") or {}
            have = int(stats.get("episodeFileCount") or 0)
            total = int(stats.get("episodeCount") or 0)
            missing = total - have
            if not have or missing < max(1, minimum):
                continue
            number = season.get("seasonNumber")
            # Searched for on several separate occasions and still a gap:
            # nobody has the episodes. Still reported, no longer searched for
            # by itself on every full pass.
            _done, said, noted = _settled_bits(_settled(
                ctx, searching_for(arr, "season_gaps", item.get("id"), number),
                cfg))
            findings.append(Finding(
                rule="season_gaps", severity="warning", service=arr.kind,
                title=f"{item.get('title', '?')} — Season {number}",
                message="finding.season_gaps",
                params={"missing": missing, "total": total, "season": number,
                        **said},
                data={"item_id": item.get("id"), "season": number,
                      "missing": missing, "have": have, "total": total,
                      **noted},
            ))
    return findings


def check_series_incomplete(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """A series that has finished airing and is still not complete.

    Worth separating from an ordinary gap because it will not fix itself. A
    running series missing last week's episode is waiting for an indexer to
    catch up; a series that ended four years ago is missing those episodes
    permanently unless somebody goes looking.
    """
    findings = []
    for item in ctx.get("items", []):
        if not item.get("ended") or not item.get("monitored"):
            continue
        stats = item.get("statistics") or {}
        have = int(stats.get("episodeFileCount") or 0)
        total = int(stats.get("episodeCount") or 0)
        if not total or have >= total:
            continue
        _done, said, noted = _settled_bits(_settled(
            ctx, searching_for(arr, "series_incomplete", item.get("id")), cfg))
        findings.append(Finding(
            rule="series_incomplete", severity="info", service=arr.kind,
            title=item.get("title", "?"),
            message="finding.series_incomplete",
            params={"have": have, "total": total, "missing": total - have,
                    **said},
            data={"item_id": item.get("id"), "have": have, "total": total,
                  "missing": total - have, **noted},
        ))
    return findings


def check_stale_blocklist(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """An old blocklist entry that may be why a title never arrives.

    The blocklist is permanent and nothing ever reviews it. A release refused
    months ago because it failed to unpack once, or because a profile said no
    to something the profile now says yes to, is still refused today — and if
    it was the only copy anyone had, the title simply never comes.

    Only raised where it can still matter: the entry is old, and the thing it
    was for is *still* missing. A blocklisted release for something that has
    since arrived is doing its job.
    """
    days = float(cfg.get("blocklist_stale_days", 60))
    if days <= 0:
        return []
    items = {i["id"]: i for i in ctx.get("items", []) if i.get("id")}
    findings, seen = [], set()
    for entry in ctx.get("blocklist", []):
        minutes = _age_minutes(entry.get("date"))
        if minutes is None or minutes < days * 1440:
            continue
        item_id = entry.get("movieId") or entry.get("seriesId")
        item = items.get(item_id)
        if item is None:
            continue
        if not _still_missing(item):
            continue
        key = (item_id, (entry.get("sourceTitle") or "")[:60])
        if key in seen:
            continue
        seen.add(key)
        findings.append(Finding(
            rule="stale_blocklist", severity="info", service=arr.kind,
            title=item.get("title", "?"),
            message="finding.stale_blocklist",
            params={"release": (entry.get("sourceTitle") or "?")[:70],
                    "days": int(minutes / 1440)},
            data={"item_id": item_id, "blocklist_id": entry.get("id"),
                  "release": entry.get("sourceTitle"),
                  "age_hours": round(minutes / 60, 1),
                  "indexer": entry.get("indexer")},
        ))
    return findings


def _still_missing(item: dict) -> bool:
    """Is the thing this entry was blocklisted for still not there?"""
    if "hasFile" in item:
        return not item.get("hasFile")
    stats = item.get("statistics") or {}
    total = int(stats.get("episodeCount") or 0)
    return bool(total) and int(stats.get("episodeFileCount") or 0) < total


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
    for item, file_info, _label in _library_files(arr, ctx):
        if file_info.get("size"):
            library.append((int(file_info["size"]), item, file_info))
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

            same_size = [item for s, item, _f in library if abs(s - size) <= size * 0.01]
            same_files = {item.get("id"): info for s, item, info in library
                          if abs(s - size) <= size * 0.01}
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
                    file_info = same_files.get(item.get("id")) or {}
                    origin = (file_info.get("sceneName")
                              or file_info.get("originalFilePath") or "")
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


#: SABnzbd's names for a job that is finished downloading and still being
#: worked on. Each is normal for minutes; none of them is normal for hours.
_POSTPROCESSING = frozenset({"queued", "quickcheck", "verifying", "repairing",
                             "fetching", "extracting", "moving", "running"})

#: Cause → (what to suggest, whether that is certain enough to do unasked).
DOWNLOADER_CAUSES: dict[str, tuple[str, bool]] = {
    "dl_paused":             ("resume", False),
    "dl_encrypted":          ("blocklist_and_search", True),
    "dl_unwanted":           ("blocklist_and_search", True),
    "dl_fetching":           (policy.REPORT, False),
    "dl_postprocessing":     (policy.REPORT, False),
    "dl_failed_unnoticed":   ("blocklist_and_search", True),
    "dl_completed_unclaimed": ("import", False),
}


def _grab_ledger(ctx: dict) -> tuple[dict[str, dict], set[str]]:
    """Which download ids a service grabbed, and which it has finished with.

    The grab history is the one place that knows for certain which title a
    download was fetched for — the name can be anything, the download id is
    the id. "Finished with" means imported, marked failed or ignored: in each
    case the service has seen the end of it and a leftover history row in the
    download client is not a stuck download.
    """
    grabs: dict[str, dict] = {}
    done: set[str] = set()
    for source in ctx.get("arr_history") or []:
        for row in source.get("rows") or []:
            download = str(row.get("downloadId") or "").lower()
            if not download:
                continue
            if compat.event_is(row, compat.GRABBED):
                grabs.setdefault(download, {
                    "kind": source.get("kind"), "instance": source.get("instance"),
                    "item_id": row.get("movieId") or row.get("seriesId"),
                    "release": row.get("sourceTitle") or ""})
            elif (compat.event_is(row, compat.IMPORTED)
                  or compat.event_is(row, compat.FAILED)
                  or row.get("eventType") == "downloadIgnored"):
                done.add(download)
    return grabs, done


def _in_library(name: str, ctx: dict) -> bool:
    """Does a library file say it came from this download?

    A file imported by hand — by this program, say — leaves no import event
    carrying the download id, so the ledger alone would call it unclaimed.
    The file remembers its scene name, and that is enough to know better.
    """
    if not name:
        return False
    for item in ctx.get("items") or []:
        info = item.get("movieFile") or {}
        origin = str(info.get("sceneName") or info.get("originalFilePath") or "")
        if origin and (origin == name or origin.startswith(name[:40])):
            return True
    return False


def check_stuck_in_downloader(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """A job in the download client that nobody is moving on.

    The queue rules see what the services see. This sees what they do not:

    * a job paused on its own while the client runs — the client pauses a job
      it has found to be **encrypted** or to contain an **unwanted extension**,
      and the service goes on showing it as merely paused, for ever;
    * a job still fetching its NZB, or stuck in post-processing, for hours;
    * a job the client gave up on (**Failed**) that the service which grabbed
      it never noticed — usually because its category was changed on the way;
    * a job the client **completed** that the service which grabbed it never
      picked up — the same category or path mismatch, the other way round.

    What the services grabbed is read from their history by download id, never
    guessed from the name (the grab history is the most reliable source there
    is). A job no service grabbed is somebody's own and is left alone. Nothing
    here deletes: the one remedy that clears a job is blocklisting it through
    the service that grabbed it, and that only for a job the client itself has
    already declared dead.
    """
    threshold = float(cfg.get("stuck_hours", 3))
    store = ctx.get("store")
    tracked = {str(e.get("downloadId") or "").lower(): e
               for e in ctx.get("all_queues") or [] if e.get("downloadId")}
    grabs, done = _grab_ledger(ctx)
    findings, active = [], set()

    def observed_hours(key: str, state: str) -> float:
        active.add(key)
        if store is None:
            return 0.0
        return store.check_progress(key, zlib.crc32(state.encode("utf-8"))) / 60

    def finding(client: dict, slot: dict, cause: str, hours: float,
                gb: float, extra: dict, ours: bool = True) -> Finding:
        suggested, certain = DOWNLOADER_CAUSES[cause] if ours \
            else (policy.REPORT, False)
        extra = dict(extra)
        service = extra.pop("_service", None) or "sabnzbd"
        entry_id = extra.pop("_entry_id", None)
        name = str(slot.get("filename") or slot.get("name") or "?")
        data = {"client": client["name"], "nzo_id": slot.get("nzo_id"),
                "release": name, "cause": cause, "suggested": suggested,
                "certain": certain, "age_hours": round(hours, 1), "gb": gb,
                "status": slot.get("status"),
                "messages": [str(x)[:300] for x in
                             ([slot.get("fail_message")] + list(slot.get("labels") or []))
                             if x][:8],
                **extra}
        if suggested != policy.REPORT and not certain:
            data["hold"] = "policy.needs_a_look"
        return Finding(
            rule="stuck_in_downloader",
            severity="error" if certain else "warning",
            service=service, entry_id=entry_id,
            title=name[:70], message=f"finding.stuck.{cause}",
            params={"hours": f"{hours:.1f}", "client": client["name"],
                    "text": " / ".join(data["messages"])[:400] or "—"},
            data=data)

    def owner(download: str) -> dict:
        """Where acting on this job has to go, and what it concerns."""
        entry = tracked.get(download)
        if entry is not None:
            return {"_service": entry.get("_kind"), "_entry_id": entry.get("id"),
                    "_instance": entry.get("_instance"),
                    "item_id": _item(entry).get("id")}
        grab = grabs.get(download)
        if grab is not None:
            return {"_service": grab["kind"], "_instance": grab["instance"],
                    "item_id": grab["item_id"]}
        return {}

    for client in ctx.get("downloaders") or []:
        status = client.get("status") or {}
        everything_paused = bool(status.get("paused") or status.get("paused_all"))
        scope = f"stuckdl:{client['name']}:"

        for slot in client.get("slots") or []:
            download = str(slot.get("nzo_id") or "")
            state = str(slot.get("status") or "")
            labels = " ".join(str(x) for x in (slot.get("labels") or [])).upper()
            if state.lower() == "paused":
                # The whole client being paused is downloader_paused's
                # business; every job in it would otherwise be reported too.
                if everything_paused:
                    continue
                cause = ("dl_encrypted" if "ENCRYPTED" in labels
                         else "dl_unwanted" if "UNWANTED" in labels
                         else "dl_paused")
            elif state.lower() in ("grabbing", "fetching"):
                cause = "dl_fetching"
            else:
                continue
            hours = observed_hours(f"{scope}q:{download}", f"{state}|{labels}")
            if hours < threshold:
                continue
            extra = owner(download.lower())
            # Dead, but not ours to replace when no service is tracking it:
            # there is nobody to blocklist it with and nothing to search for.
            ours = "_entry_id" in extra or cause not in ("dl_encrypted", "dl_unwanted")
            findings.append(finding(client, slot, cause, hours,
                                    _mb_to_gb(slot.get("mb")), extra, ours))

        for slot in client.get("history") or []:
            download = str(slot.get("nzo_id") or "")
            key = download.lower()
            state = str(slot.get("status") or "").lower()
            name = str(slot.get("name") or "")
            gb = round(int(slot.get("bytes") or 0) / 1024 ** 3, 2)
            if key in tracked:
                # The service sees it — whatever is wrong is on its queue, and
                # stuck_in_queue speaks for it with the service's own words.
                continue
            if state in _POSTPROCESSING:
                hours = observed_hours(f"{scope}h:{download}", state)
                if hours >= threshold:
                    findings.append(finding(client, slot, "dl_postprocessing",
                                            hours, gb, {}))
                continue
            if state not in ("failed", "completed"):
                continue
            if key not in grabs or key in done:
                continue
            completed = slot.get("completed") or 0
            hours = (time.time() - completed) / 3600 if completed else 0.0
            if hours < threshold:
                continue
            extra = owner(key)
            if state == "failed":
                findings.append(finding(client, slot, "dl_failed_unnoticed",
                                        hours, gb, extra))
            elif not _in_library(name, ctx):
                findings.append(finding(
                    client, slot, "dl_completed_unclaimed", hours, gb,
                    {**extra, "path": slot.get("storage") or slot.get("path")}))
        if store is not None:
            store.prune_progress(active, scope)
    return findings


def _mb_to_gb(value) -> float:
    try:
        return round(float(value or 0) / 1024, 2)
    except (TypeError, ValueError):
        return 0.0


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
#: How many grabs under one profile it takes before "none of them reached the
#: target" says something about the profile rather than about chance.
PROFILE_SAMPLES = 5


def _grab_score(entry: dict) -> int | None:
    raw = (entry.get("data") or {}).get("customFormatScore")
    if raw in (None, ""):
        raw = entry.get("customFormatScore")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _allowed_quality_ids(profile: dict) -> set:
    ids = set()
    for entry in profile.get("items") or []:
        children = entry.get("items") or []
        if entry.get("allowed"):
            if entry.get("quality"):
                ids.add((entry["quality"] or {}).get("id"))
            else:
                ids.add(entry.get("id"))
        for child in children:
            if child.get("allowed") or entry.get("allowed"):
                ids.add((child.get("quality") or {}).get("id"))
    ids.discard(None)
    return ids


def retune_key(arr: Arr, profile_id) -> str:
    return f"retune:{getattr(arr, 'service_id', None) or arr.kind}:{profile_id}"


def check_profile_loop(arr: Arr, ctx: dict, cfg: dict) -> list[Finding]:
    """A quality profile that makes the service fetch the same thing forever.

    Found on a live library before it was a rule. Six of seven profiles said
    *keep upgrading until a file scores 520000*, and the best German release
    those indexers carried scored 381600 — not one of 224 grabs reached the
    target. So every file was permanently "not good enough yet", and the same
    episode was fetched eleven times in one hour.

    One setting decides whether that is possible: *upgrade until custom
    format score*. Anything above zero is a bet that a file keeps the score its
    release had and that the score is reachable at all. This rule measures the
    bet against the grabs the service has actually made, so the finding says
    what was reached and not only what was asked for.

    Also caught: a cutoff pointing at a quality the profile does not allow, and
    a floor no release can clear. Both make the service search for good.

    A profile manager such as Profilarr writes its own values back on every
    sync. When this rule has corrected a profile and finds it set back, it does
    not correct it again — it says where the value has to be changed instead.
    A tug of war with another program every hour helps nobody.
    """
    profiles = ctx.get("profiles") or []
    if not profiles:
        return []
    items = ctx.get("items") or []
    profile_of = {i.get("id"): i.get("qualityProfileId") for i in items
                  if i.get("id")}
    usage = Counter(profile_of.values())

    best: dict = {}
    samples: Counter = Counter()
    per_title: Counter = Counter()
    for entry in ctx.get("history", []):
        if not compat.event_is(entry, compat.GRABBED):
            continue
        title_id = entry.get("movieId") or entry.get("seriesId")
        pid = profile_of.get(title_id)
        if pid is None:
            continue
        # Per episode on Sonarr, per film on Radarr: that is the unit that is
        # fetched again.
        per_title[(pid, entry.get("episodeId") or title_id)] += 1
        score = _grab_score(entry)
        if score is None:
            continue
        samples[pid] += 1
        best[pid] = max(best.get(pid, score), score)
    repeats = Counter(pid for (pid, _t), n in per_title.items() if n >= 3)

    store = ctx.get("store")
    findings = []
    for profile in profiles:
        pid = profile.get("id")
        if items and not usage.get(pid):
            continue                      # nothing uses it, nothing can loop
        name = str(profile.get("name") or pid)
        target = int(profile.get("cutoffFormatScore") or 0)
        floor = int(profile.get("minFormatScore") or 0)
        upgrades = bool(profile.get("upgradeAllowed"))
        reachable = sum(max(0, int(f.get("score") or 0))
                        for f in profile.get("formatItems") or [])
        allowed = _allowed_quality_ids(profile)
        base = {"profile_id": pid, "profile": name, "target": target,
                "floor": floor, "best": best.get(pid), "samples": samples[pid],
                "repeats": repeats[pid], "titles": usage.get(pid, 0)}

        if upgrades and target > 0:
            record = store.attempt(retune_key(arr, pid)) if store else None
            if record:
                # Corrected here once already, and set back since.
                findings.append(Finding(
                    rule="profile_loop", severity="error", service=arr.kind,
                    title=name, message="finding.profile_loop_set_back",
                    params={"target": target},
                    data={**base, "problem": "set_back",
                          "hold": "policy.managed_elsewhere"}))
                continue
            measured = samples[pid] >= PROFILE_SAMPLES
            if measured and best.get(pid, 0) < target:
                message, severity, problem = ("finding.profile_loop_unreachable",
                                              "error", "unreachable")
            elif repeats[pid]:
                message, severity, problem = ("finding.profile_loop_repeating",
                                              "error", "repeating")
            else:
                message, severity, problem = ("finding.profile_loop_risk",
                                              "warning", "risk")
            findings.append(Finding(
                rule="profile_loop", severity=severity, service=arr.kind,
                title=name, message=message,
                params={"target": target, "best": best.get(pid, "?"),
                        "samples": samples[pid], "repeats": repeats[pid],
                        "titles": usage.get(pid, 0)},
                data={**base, "problem": problem}))

        if upgrades and allowed and profile.get("cutoff") not in allowed:
            findings.append(Finding(
                rule="profile_loop", severity="error", service=arr.kind,
                title=name, message="finding.profile_cutoff_disabled",
                data={**base, "problem": "cutoff_disabled"}))

        if floor > reachable:
            findings.append(Finding(
                rule="profile_loop", severity="error", service=arr.kind,
                title=name, message="finding.profile_floor_unreachable",
                params={"floor": floor, "reachable": reachable},
                # Changing the floor changes what is grabbed at all; that is
                # a decision about taste, not a repair, and is left alone.
                data={**base, "problem": "floor",
                      "hold": "policy.needs_a_person"}))
    return findings
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
    # Acting by default, and on every fast pass rather than only the full one:
    # the point is to stop it before the sixty-eight gigabytes arrive, not to
    # report on them afterwards. The confidence it ships with is high on
    # purpose — this one throws a download away.
    Rule("collection_pack", "queue", check_collection_pack,
         actions=(policy.REPORT, "remove", "blocklist", "blocklist_and_search"),
         default_action="blocklist_and_search",
         conditions=("max_gb", "min_confidence"),
         default_conditions={"min_confidence": 0.8},
         only_kinds=("radarr",)),
    # Reporting by default: an early release in one region is rare but real,
    # and being wrong here throws away the only copy there is.
    Rule("premature_grab", "queue", check_premature_grab,
         actions=(policy.REPORT, "remove", "blocklist", "blocklist_and_search"),
         conditions=("max_gb", "min_confidence")),
    # Nothing to do here on purpose: the cause is in the profile, and no
    # action taken on the queue can fix that.
    Rule("grab_loop", "queue", check_grab_loop, deep=True),
    # The net under all of the above. Each finding carries its own remedy, so
    # "do what it suggests" is the default — and only the remedies that are
    # certain are carried out unasked; the rest wait for a button.
    Rule("stuck_in_queue", "queue", check_stuck_in_queue,
         actions=(policy.REPORT, policy.AS_SUGGESTED, "import", "remove",
                  "blocklist", "blocklist_and_search"),
         default_action=policy.AS_SUGGESTED,
         conditions=("min_age_hours", "max_gb")),

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
    # These three used to say "Radarr only". That was never a statement about
    # the question — a series file lacks a language, fails to read and falls
    # below a profile exactly the way a film does — it was a statement about
    # this program, which only knew how to find a movie's file. It knows how to
    # find a series' files now.
    Rule("missing_audio_language", "library", check_missing_audio_language,
         actions=(policy.REPORT, "search"), deep=True),
    Rule("unreadable_file", "library", check_unreadable_file,
         actions=(policy.REPORT, "refresh", "search"), conditions=("max_gb",),
         deep=True),
    Rule("below_profile", "library", check_below_profile,
         actions=(policy.REPORT, "search"), deep=True),
    Rule("missing_items", "library", check_missing_items,
         actions=(policy.REPORT, "search"), deep=True),
    # Searching for every title below its cutoff at once is a great many
    # queries. That belongs to whoever pays for them.
    Rule("cutoff_unmet", "library", check_cutoff_unmet,
         actions=(policy.REPORT, "search"), deep=True),
    Rule("season_gaps", "library", check_season_gaps,
         actions=(policy.REPORT, "search"), only_kinds=("sonarr",), deep=True),
    Rule("series_incomplete", "library", check_series_incomplete,
         actions=(policy.REPORT, "search"), only_kinds=("sonarr",), deep=True),
    # Un-blocklisting is not destructive, but it does undo somebody's earlier
    # refusal, so it is offered rather than assumed.
    Rule("stale_blocklist", "library", check_stale_blocklist,
         actions=(policy.REPORT, "unblocklist"), deep=True),

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
    # Deep pass only: telling a forgotten job from a finished one takes the
    # services' history, and that is not worth fetching every minute for
    # something measured in hours.
    Rule("stuck_in_downloader", "downloader", check_stuck_in_downloader,
         actions=(policy.REPORT, policy.AS_SUGGESTED, "resume", "import",
                  "blocklist_and_search"),
         default_action=policy.AS_SUGGESTED,
         conditions=("min_age_hours", "max_gb"), scope="once", deep=True),

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
    # Acting by default: this is the one thing that turns a library into a
    # loop, and the correction — stop upgrading on score — takes nothing away
    # except the loop. Resolution upgrades are untouched.
    Rule("profile_loop", "system", check_profile_loop,
         actions=(policy.REPORT, "retune_profile"),
         default_action="retune_profile", deep=True),
    Rule("service_health", "system", check_service_health),
    Rule("disk_space", "system", check_disk_space),
    # Reporting only on purpose, and permanently: the thing that needs
    # changing when this fires is this program, not anything on the server.
    Rule("api_changes", "system", check_api_changes, deep=True),
)

BY_NAME: dict[str, Rule] = {r.name: r for r in ALL}
