"""How much room a profile takes, and how long the disk will last.

Everything here is worked out in **megabytes per minute of running time**,
because that is the unit the services themselves use for their quality
definitions, and because it is the one number that stays put between a
ninety minute film and a three hour one.

Where the numbers come from
---------------------------
Five sources, best first. Each estimate says which one it used.

``profile``
    For a profile that already exists and holds files: what those files
    weigh. It knows which rung the profile ends up on in practice, which its
    ladder does not — see :func:`profile_rate`.
``measured``
    The files already in the library. The middle of what this user's files at
    that quality actually weigh, per minute. Nothing beats it: it already holds
    the groups they end up with, the codecs, the sound.
``service``
    The *preferred* size the service has been told for that quality. When
    somebody has set it, it is what the service leans towards when it picks
    between two releases, so it is what arrives.
``scaled``
    No files at this quality, but some at the same resolution. The reference
    value, moved by as much as this library's files at that resolution differ
    from the reference.
``reference``
    The table below, and nothing else.

Why the service's own sizes are mostly not used
-----------------------------------------------
Because left alone they say nothing. Measured on a Radarr 6 and a Sonarr 4 at
their defaults: every quality prefers 1990 and 990 MB a minute respectively,
with a maximum of 2000 and 1000 — the top of the slider, which the interface
shows as "unlimited". Taken at its word, a two hour film at WEB-DL 1080p is
233 GB. So a preferred size at the top of the scale counts as not set, and a
maximum there counts as no maximum.

The reference table
-------------------
Measured on a library of 479 films and 443 episodes, 1080p only, since that is
what it held: WEB-DL 45 MB a minute (171 films and 321 episodes agree to within
a megabyte), Blu-ray encodes 68. The rest of the table keeps the proportions
between sources and resolutions that release groups work to; a single 2160p
remux there measured 534 and a single 2160p WEB-DL 146, both close to what is
written below.

What "low", "typical" and "high" mean
-------------------------------------
For one quality: the lower quartile, the median and the upper quartile of what
files at that quality weigh — measured where there are files, otherwise the
spread measured there (lower quartile at 80–84 % of the median, upper at
114–122 %) widened a little, because a reference value is less certain.

For a title: *low* is the lowest rung the profile takes, at its lower quartile
— what arrives when nothing better is on offer. *Typical* and *high* are the
best rung, because the services rank by quality first and take the best rung on
offer when they grab. That errs towards more room rather than less, which is
the side to err on when the question is whether it fits.
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import median

from .profiles import RESOLUTIONS, quality_key

log = logging.getLogger(__name__)

MB = 1024 ** 2
GB = 1024 ** 3

#: MB a minute for a typical release, per ``source-resolution``.
REFERENCE: dict[str, float] = {
    "hdtv-720p": 18.0, "webrip-720p": 16.0, "webdl-720p": 22.0,
    "bluray-720p": 35.0,
    "hdtv-1080p": 32.0, "webrip-1080p": 32.0, "webdl-1080p": 45.0,
    "bluray-1080p": 68.0, "remux-1080p": 230.0,
    "hdtv-2160p": 90.0, "webrip-2160p": 100.0, "webdl-2160p": 140.0,
    "bluray-2160p": 260.0, "remux-2160p": 520.0,
}

#: Lower and upper quartile as a share of the median, where nothing is
#: measured. See the module docstring for where these come from.
SPREAD = (0.75, 1.35)

#: How many files at one quality it takes before their middle is trusted over
#: the reference. Below that, one odd file is the whole answer.
MIN_SAMPLES = 5

#: Runtime to assume when the service has none. Five films in five hundred
#: had none; counting them as nothing would say they cost no room.
FILM_MINUTES = 110
EPISODE_MINUTES = 45

#: What one title is in an example: a two hour film, an episode of its kind.
FILM_EXAMPLE = 120


def example_minutes(kind: str, episode: int | None = None) -> int:
    return FILM_EXAMPLE if kind == "radarr" else episode or EPISODE_MINUTES

#: The top of the size sliders. At or near it, a preferred size means "not
#: set" and a maximum means "no maximum" — the services' own defaults.
CEILING = {"radarr": 2000.0, "sonarr": 1000.0}

#: How many series to read episode files for. Sonarr hands those out one
#: series per request, so reading every series of a large library takes
#: minutes; a spread of this many is enough to measure what a minute weighs.
SERIES_SAMPLE = 60

#: History needed before a trend is stated. Less than two weeks, or a handful
#: of imports, and "full in four months" is a guess wearing a number.
MIN_GROWTH_DAYS = 14
MIN_GROWTH_IMPORTS = 5
GROWTH_WINDOW_DAYS = 90

#: File deletions, by name only. The numbers are not the same in both
#: applications — 6 is "file deleted" in Radarr and "file renamed" in Sonarr —
#: and a renamed file taken for a deleted one would count its size as freed.
_DELETED = ("moviefiledeleted", "episodefiledeleted")
_IMPORTED = "downloadfolderimported"


def _resolution_of(key: str) -> str:
    return key.rsplit("-", 1)[-1]


_SOURCE_LABELS = {"hdtv": "HDTV", "webrip": "WEBRip", "webdl": "WEB-DL",
                  "bluray": "Blu-ray", "remux": "Remux"}


def label(key: str) -> str:
    """``bluray-1080p`` as a person writes it. The same in every language."""
    source, _, rung = key.partition("-")
    return f"{_SOURCE_LABELS.get(source, source)} {rung}".strip()


# ---------------------------------------------------------------------------
# Per quality
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Definition:
    """One of the service's quality definitions, with its defaults taken out."""
    key: str
    min_mb: float
    preferred_mb: float | None
    max_mb: float | None


def definitions(raw: list[dict], kind: str) -> dict[str, Definition]:
    ceiling = max([CEILING.get(kind, 0.0)]
                  + [_number(d.get("maxSize")) for d in raw])
    out: dict[str, Definition] = {}
    for entry in raw:
        key = quality_key((entry.get("quality") or {}).get("name", ""))
        if key is None:
            continue
        highest = _number(entry.get("maxSize"))
        preferred = _number(entry.get("preferredSize"))
        highest = highest if 0 < highest < ceiling else None
        # "Near" the top rather than at it: the defaults stop ten short of
        # the maximum, and a slider dragged all the way right lands a little
        # below it as well.
        if preferred <= 0 or preferred >= 0.95 * ceiling or (
                highest and preferred >= highest):
            preferred = None
        out[key] = Definition(key=key, min_mb=max(0.0, _number(entry.get("minSize"))),
                              preferred_mb=preferred, max_mb=highest)
    return out


@dataclass(frozen=True)
class Rate:
    """What a minute at one quality weighs, in MB."""
    key: str
    low: float
    typical: float
    high: float
    basis: str
    samples: int = 0

    def as_dict(self) -> dict:
        return {"quality": self.key, "name": label(self.key),
                "low": round(self.low, 1),
                "typical": round(self.typical, 1), "high": round(self.high, 1),
                "basis": self.basis, "samples": self.samples}


def rates(defs: dict[str, Definition],
          per_minute: dict[str, list[float]]) -> dict[str, Rate]:
    """The weight of a minute at every quality this knows about.

    ``per_minute`` is what :func:`samples` measured on the library.
    """
    measured = {key: sorted(values) for key, values in per_minute.items()
                if key in REFERENCE and len(values) >= MIN_SAMPLES}
    by_resolution: dict[str, list[float]] = defaultdict(list)
    for key, values in measured.items():
        by_resolution[_resolution_of(key)].append(
            median(values) / REFERENCE[key])

    out: dict[str, Rate] = {}
    for key, reference in REFERENCE.items():
        definition = defs.get(key)
        if key in measured:
            values = measured[key]
            low, typical, high = (_quantile(values, 0.25), median(values),
                                  _quantile(values, 0.75))
            basis, count = "measured", len(values)
        else:
            count = 0
            if definition and definition.preferred_mb:
                typical, basis = definition.preferred_mb, "service"
            elif by_resolution.get(_resolution_of(key)):
                typical = reference * median(by_resolution[_resolution_of(key)])
                basis = "scaled"
            else:
                typical, basis = reference, "reference"
            low, high = typical * SPREAD[0], typical * SPREAD[1]
        if definition:
            # The service refuses anything outside its own window, so nothing
            # outside it can arrive whatever the files elsewhere weigh.
            ceiling = definition.max_mb or float("inf")
            low, typical, high = (min(max(v, definition.min_mb), ceiling)
                                  for v in (low, typical, high))
        out[key] = Rate(key=key, low=low, typical=typical, high=high,
                        basis=basis, samples=count)
    return out


def _quantile(values: list[float], share: float) -> float:
    if not values:
        return 0.0
    position = share * (len(values) - 1)
    below = int(position)
    above = min(below + 1, len(values) - 1)
    return values[below] + (values[above] - values[below]) * (position - below)


def _number(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Per title
# ---------------------------------------------------------------------------
#: A size window per quality, in GB, as the profile would apply it. Zero is
#: "no limit".
Limits = dict[str, tuple[float, float]]


@dataclass(frozen=True)
class Estimate:
    """What one film or one episode will weigh, in GB."""
    low: float
    typical: float
    high: float
    basis: str
    quality: str
    #: The profile's size window cut into the typical size: most releases
    #: of the best quality would be refused, and what arrives is smaller.
    capped: bool = False

    def as_dict(self, minutes: float | None = None) -> dict:
        out = {"low_gb": round(self.low, 2), "typical_gb": round(self.typical, 2),
               "high_gb": round(self.high, 2), "basis": self.basis,
               "quality": self.quality, "capped": self.capped}
        if minutes is not None:
            out["minutes"] = round(minutes)
        return out


def estimate(minutes: float, keys: list[str], table: dict[str, Rate],
             limits: Limits | None = None,
             own: Rate | None = None) -> Estimate | None:
    """One title at this profile. ``keys`` is the ladder, worst first.

    ``own`` is what the profile's own files weigh, see :func:`profile_rate`.
    Where there is one it is used instead of the ladder: it already knows
    which rung this profile ends up on in practice.
    """
    known = [key for key in keys if key in table]
    if not known or minutes <= 0:
        return None
    limits = limits or {}
    worst, best = table[known[0]], table[known[-1]]

    def fit(value_mb: float, key: str) -> float:
        low, high = limits.get(key, (0.0, 0.0))
        gb = value_mb * minutes / 1024
        if high:
            gb = min(gb, high)
        return max(gb, low)

    if own is not None:
        return Estimate(low=fit(own.low, best.key),
                        typical=fit(own.typical, best.key),
                        high=fit(own.high, best.key), basis=own.basis,
                        quality=best.key)
    typical = fit(best.typical, best.key)
    return Estimate(low=fit(worst.low, worst.key), typical=typical,
                    high=fit(best.high, best.key), basis=best.basis,
                    quality=best.key,
                    capped=typical < best.typical * minutes / 1024 - 0.01)


def profile_rate(chosen: list[Title]) -> Rate | None:
    """What a minute weighs in the files a profile already holds.

    The ladder says what a profile *may* take; this says what it did. They
    differ more than one would think. Measured: a profile that allowed 1080p
    remux held 280 films and not one remux, because its custom formats kept
    them out. The ladder alone put a missing film at 25 GB; the profile's own
    files, at under 6.
    """
    values = sorted(
        title.size / MB / (title.minutes * title.files)
        for title in chosen
        if title.files and title.size and title.runtime_known and title.minutes)
    if len(values) < MIN_SAMPLES:
        return None
    return Rate(key="profile", low=_quantile(values, 0.25),
                typical=median(values), high=_quantile(values, 0.75),
                basis="profile", samples=len(values))


def wish_limits(wish, keys: list[str]) -> Limits:
    return {key: wish.limits_at(_resolution_of(key)) for key in keys}


def profile_keys(profile: dict) -> tuple[list[str], str | None]:
    """The rungs an existing profile takes, worst first, and its cutoff.

    In the service's own order, which is its ranking and can differ from the
    usual one. The members of a group rank the same; they are put in the
    usual order among themselves so the last one is the largest, and the
    cutoff is the first member of the group it points at, because any of
    them meets it. A quality
    the services do not name the way this does (SDTV, DVD) has no rate and
    is left out.
    """
    keys: list[str] = []
    cutoff_id = profile.get("cutoff")
    cutoff: str | None = None
    for entry in profile.get("items") or []:
        children = entry.get("items") or []
        members = children if children else [entry]
        here = sorted({
            key for key in (
                quality_key((member.get("quality") or {}).get("name", ""))
                for member in members
                if member.get("allowed") or entry.get("allowed"))
            if key and key not in keys}, key=_rank)
        keys += here
        ids = {entry.get("id")} if children else set()
        ids |= {(m.get("quality") or {}).get("id") for m in members}
        if here and cutoff_id in ids:
            cutoff = here[0]
    return keys, cutoff


def _rank(key: str) -> tuple[int, int]:
    source, rung = key.split("-", 1)
    order = ("hdtv", "webrip", "webdl", "bluray", "remux")
    return (RESOLUTIONS.index(rung) if rung in RESOLUTIONS else -1,
            order.index(source) if source in order else -1)


def profile_limits(profile: dict, formats: list[dict],
                   keys: list[str]) -> Limits:
    """The size windows an existing profile enforces, read off its formats.

    Only formats that refuse outright count: a size condition worth a few
    points less decides between releases, it does not keep any out. Refusing
    means the release cannot reach the floor however many bonuses it collects.
    """
    by_id = {f.get("id"): f for f in formats}
    floor = int(profile.get("minFormatScore") or 0)
    items = profile.get("formatItems") or []
    bonuses = sum(max(0, int(i.get("score") or 0)) for i in items)
    limits: Limits = dict.fromkeys(keys, (0.0, 0.0))
    for item in items:
        score = int(item.get("score") or 0)
        if score >= 0 or bonuses + score >= floor:
            continue
        specs = (by_id.get(item.get("format")) or {}).get("specifications") or []
        sizes = [s for s in specs if s.get("implementation") == "SizeSpecification"
                 and not s.get("negate")]
        rungs = {f"{_field(s, 'value')}p" for s in specs
                 if s.get("implementation") == "ResolutionSpecification"
                 and not s.get("negate")}
        others = [s for s in specs if s.get("implementation") not in (
            "SizeSpecification", "ResolutionSpecification")]
        if len(sizes) != 1 or others:
            continue
        low, high = _number(_field(sizes[0], "min")), _number(_field(sizes[0], "max"))
        for key in keys:
            if rungs and _resolution_of(key) not in rungs:
                continue
            floor_gb, ceiling_gb = limits[key]
            if low <= 0 < high:
                # Refuses (0, high]: nothing at or under ``high`` is taken.
                floor_gb = max(floor_gb, high)
            elif low > 0:
                # Refuses (low, …]: nothing over ``low`` is taken.
                ceiling_gb = min(ceiling_gb, low) if ceiling_gb else low
            limits[key] = (floor_gb, ceiling_gb)
    return limits


def _field(spec: dict, name: str):
    for entry in spec.get("fields") or []:
        if entry.get("name") == name:
            return entry.get("value")
    return None


# ---------------------------------------------------------------------------
# The library
# ---------------------------------------------------------------------------
@dataclass
class Title:
    """One film or one series, as far as room is concerned."""
    id: int
    title: str
    year: int | None
    profile_id: int | None
    #: Per film, or per episode.
    minutes: float
    runtime_known: bool
    #: One for a film; the episodes the service wants for a series.
    units: int
    files: int
    size: int
    monitored: bool
    root: str
    #: ``(quality key, bytes)`` per file, where known. Always there for a
    #: film with a file; for a series only when its episodes were read.
    file_qualities: list[tuple[str | None, int]] | None = None


def runtime_minutes(text) -> float | None:
    """``1:49:23`` or ``55:17`` as minutes — what media analysis reports."""
    try:
        parts = [float(p) for p in str(text or "").split(":")]
    except ValueError:
        return None
    if len(parts) == 3:
        return parts[0] * 60 + parts[1] + parts[2] / 60
    if len(parts) == 2:
        return parts[0] + parts[1] / 60
    return None


def titles(kind: str, items: list[dict],
           episode_files: dict[int, list[dict]] | None = None) -> list[Title]:
    """Every title with what it weighs and would weigh.

    ``episode_files`` maps a series id to its files, for the series that were
    read; see :data:`SERIES_SAMPLE`.
    """
    fallback = FILM_MINUTES if kind == "radarr" else EPISODE_MINUTES
    known = [float(i.get("runtime") or 0) for i in items if i.get("runtime")]
    usual = median(known) if known else fallback
    out = []
    for item in items:
        runtime = float(item.get("runtime") or 0)
        common = {"id": item.get("id"), "title": str(item.get("title") or ""),
                  "year": item.get("year"),
                  "profile_id": item.get("qualityProfileId"),
                  "minutes": runtime or usual, "runtime_known": runtime > 0,
                  "monitored": bool(item.get("monitored")),
                  "root": str(item.get("rootFolderPath") or item.get("path") or "")}
        if kind == "radarr":
            movie_file = item.get("movieFile") or {}
            has = bool(item.get("hasFile") or movie_file)
            size = int(item.get("sizeOnDisk") or movie_file.get("size") or 0)
            quality = quality_key(
                ((movie_file.get("quality") or {}).get("quality") or {}).get("name"))
            out.append(Title(**common, units=1, files=1 if has else 0, size=size,
                             file_qualities=[(quality, size)] if has else None))
        else:
            stats = item.get("statistics") or {}
            files = int(stats.get("episodeFileCount") or 0)
            wanted = max(int(stats.get("episodeCount") or 0), files)
            read = (episode_files or {}).get(item.get("id"))
            out.append(Title(
                **common, units=wanted, files=files,
                size=int(stats.get("sizeOnDisk") or 0),
                file_qualities=None if read is None else [
                    (quality_key(((f.get("quality") or {}).get("quality") or {})
                                 .get("name")), int(f.get("size") or 0))
                    for f in read]))
    return out


def samples(kind: str, items: list[dict],
            episode_files: dict[int, list[dict]] | None = None
            ) -> dict[str, list[float]]:
    """MB a minute of every file whose quality and length are known.

    The length is the file's own where media analysis has read it, and the
    title's runtime otherwise: a film's metadata runtime and its file differ
    by the credits, a few percent, and a series' runtime is a round number
    for every episode.
    """
    out: dict[str, list[float]] = defaultdict(list)

    def add(record: dict, runtime) -> None:
        key = quality_key(((record.get("quality") or {}).get("quality") or {})
                          .get("name"))
        size = _number(record.get("size"))
        minutes = (runtime_minutes((record.get("mediaInfo") or {}).get("runTime"))
                   or _number(runtime))
        # A few minutes is a sample file or an extra, not the thing itself.
        if key and size > 0 and minutes >= 5:
            out[key].append(size / MB / minutes)

    if kind == "radarr":
        for item in items:
            if item.get("movieFile"):
                add(item["movieFile"], item.get("runtime"))
    else:
        runtimes = {i.get("id"): i.get("runtime") for i in items}
        for series_id, files in (episode_files or {}).items():
            for record in files:
                add(record, runtimes.get(series_id))
    return dict(out)


def series_to_read(items: list[dict], limit: int = SERIES_SAMPLE) -> list[int]:
    """Which series to read episode files for: every one, or an even spread.

    Spread over the whole list rather than the first few, because a list
    sorted by title has no reason to be representative at its start.
    """
    with_files = [i["id"] for i in items if i.get("id")
                  and int((i.get("statistics") or {}).get("episodeFileCount") or 0)]
    if len(with_files) <= limit:
        return with_files
    step = len(with_files) / limit
    return [with_files[int(n * step)] for n in range(limit)]


def library(chosen: list[Title], keys: list[str], cutoff: str | None,
            table: dict[str, Rate], limits: Limits | None, *,
            upgrade: bool, detail: bool = False,
            own: Rate | None = None) -> dict:
    """These titles at this profile: what is there, what it will be.

    For every file already there the question is whether the profile would
    replace it. It does when upgrades are on and the file is below the cutoff
    — including a quality the profile does not take at all, which the
    services rank below everything it does. A file the profile would keep
    keeps its own size; nothing here pretends it gets re-encoded.

    A series whose episodes were not read cannot say that per file, and is
    counted as staying as it is.
    """
    cutoff = cutoff or (keys[-1] if keys else None)
    rank = {key: n for n, key in enumerate(keys)}

    def kept(quality: str | None) -> bool:
        if not upgrade or not keys:
            return True
        # SDTV, DVD and anything else outside the ladder rank below all of it.
        if quality not in rank:
            return False
        return rank[quality] >= rank.get(cutoff, 0)

    totals = {"low": 0.0, "typical": 0.0, "high": 0.0}
    counts = Counter()
    rows = []
    current = 0
    for title in chosen:
        current += title.size
        if not title.monitored and not title.files:
            counts["skipped"] += 1
            rows.append(_row(title, "skipped", title.size / GB))
            continue
        if not title.runtime_known:
            counts["without_runtime"] += 1
        each = estimate(title.minutes, keys, table, limits, own)
        stays = 0
        replaced = 0
        # An unmonitored title is never searched for, so never upgraded.
        if title.file_qualities is None or not title.monitored:
            stays = title.size
        else:
            for quality, size in title.file_qualities:
                if kept(quality):
                    stays += size
                else:
                    replaced += 1
        missing = max(0, title.units - title.files) if title.monitored else 0
        new_units = replaced + missing
        if replaced:
            outcome = "replaced"
        elif missing:
            outcome = "fetched"
        elif title.file_qualities is None and title.files:
            outcome = "assumed_kept"
        else:
            outcome = "kept"
        counts[outcome] += 1
        for part in totals:
            gb = getattr(each, part) if each else 0.0
            totals[part] += stays / GB + new_units * gb
        rows.append(_row(title, outcome, stays / GB + new_units * (
            each.typical if each else 0.0)))
    current_gb = current / GB
    out = {
        "titles": len(chosen),
        "current_gb": round(current_gb, 1),
        "expected": {f"{k}_gb": round(v, 1) for k, v in totals.items()},
        "change": {f"{k}_gb": round(v - current_gb, 1) for k, v in totals.items()},
        "counts": {name: counts.get(name, 0) for name in OUTCOMES + (
            "without_runtime",)},
    }
    if detail:
        out["rows"] = rows
    return out


#: What happens to one title under a profile. ``assumed_kept`` is a series
#: whose episodes were not read, see :func:`library`.
OUTCOMES = ("kept", "replaced", "fetched", "skipped", "assumed_kept")


def _row(title: Title, outcome: str, expected_gb: float) -> dict:
    return {"id": title.id, "title": title.title, "year": title.year,
            "profile_id": title.profile_id, "outcome": outcome,
            "current_gb": round(title.size / GB, 2),
            "expected_gb": round(expected_gb, 2)}


def fits(change: dict, free_gb: float | None) -> str:
    """``yes``, ``tight`` (only if releases stay typical), ``no``, ``unknown``."""
    if free_gb is None:
        return "unknown"
    if change["high_gb"] <= free_gb:
        return "yes"
    if change["typical_gb"] <= free_gb:
        return "tight"
    return "no"


# ---------------------------------------------------------------------------
# Disks and growth
# ---------------------------------------------------------------------------
def _mount_of(path: str, disk_space: list[dict]) -> dict | None:
    """The disk a path is on: the longest mount point it starts with."""
    best = None
    for disk in disk_space:
        mount = str(disk.get("path") or "").rstrip("/") or "/"
        inside = path == mount or path.startswith(mount.rstrip("/") + "/")
        if inside and (best is None or len(mount) > len(str(best.get("path") or ""))):
            best = disk
    return best


def root_folders(roots: list[dict], disk_space: list[dict]) -> list[dict]:
    """Root folders with their free and total space.

    The root folder list carries the free space and, on some versions, no
    total; the total comes from the disk the folder sits on.
    """
    out = []
    for root in roots:
        path = str(root.get("path") or "")
        disk = _mount_of(path, disk_space) or {}
        free = root.get("freeSpace", disk.get("freeSpace"))
        total = root.get("totalSpace") or disk.get("totalSpace")
        out.append({"path": path, "accessible": bool(root.get("accessible", True)),
                    "free_gb": round(free / GB, 1) if free is not None else None,
                    "total_gb": round(total / GB, 1) if total else None,
                    "free": free, "total": total})
    return out


def same_disk(a: dict, b: dict) -> bool:
    """Do two root folders, seen from two services, sit on one disk?

    Two containers see the same pool under different paths — ``/movies`` in
    one, ``/tv`` in the other — so the path says nothing. What gives it away
    is the size: measured, the same total to the byte, and free space 55 MB
    apart, which is what was written in the moment between the two requests.
    """
    if not (a.get("total") and a.get("total") == b.get("total")):
        return False
    free_a, free_b = a.get("free") or 0, b.get("free") or 0
    return abs(free_a - free_b) <= max(2 * GB, 0.01 * a["total"])


def disks(roots: list[dict]) -> list[list[dict]]:
    """Root folders grouped by the disk they sit on, see :func:`same_disk`."""
    groups: list[list[dict]] = []
    for root in roots:
        for group in groups:
            if same_disk(group[0], root):
                group.append(root)
                break
        else:
            groups.append([root])
    return groups


def free_space(roots: list[dict], chosen: list[Title]) -> float | None:
    """Free GB where these titles live, each disk counted once."""
    used = [r for r in roots
            if any(_root_for(t.root, [r["path"]]) for t in chosen)] or roots
    free = [group[0]["free"] for group in disks(used)
            if group[0].get("free") is not None]
    return round(sum(free) / GB, 1) if free else None


def growth(history: list[dict], roots: list[str], now: datetime | None = None,
           items: list[dict] | None = None,
           window_days: int = GROWTH_WINDOW_DAYS) -> dict[str, dict]:
    """How fast each root folder has been filling, from the history.

    Imported minus deleted, both as the history records them: an upgrade that
    replaces a 4 GB file with a 9 GB one adds five, not nine. The window is
    what the history actually covers, never more than ``window_days``: a
    history that starts three weeks ago says nothing about three months.
    """
    now = now or datetime.now(UTC)
    start = now - timedelta(days=window_days)
    root_of_item = {}
    for item in items or []:
        root_of_item[item.get("id")] = _root_for(
            str(item.get("path") or item.get("rootFolderPath") or ""), roots)

    oldest = None
    per_root: dict[str, dict] = {root: {"imported": 0, "removed": 0, "imports": 0}
                                 for root in roots}
    for entry in history:
        when = _moment(entry.get("date"))
        if when is None or when < start:
            continue
        oldest = when if oldest is None or when < oldest else oldest
        name = str(entry.get("eventType") or "").lower()
        if name != _IMPORTED and name not in _DELETED:
            continue
        data = entry.get("data") or {}
        size = int(_number(data.get("size")))
        if name == _IMPORTED:
            root = _root_for(str(data.get("importedPath") or ""), roots)
        else:
            root = root_of_item.get(entry.get("movieId") or entry.get("seriesId"))
        root = root or (roots[0] if roots else None)
        if root is None:
            continue
        if name == _IMPORTED:
            per_root[root]["imported"] += size
            per_root[root]["imports"] += 1
        else:
            per_root[root]["removed"] += size

    days = min(window_days, (now - oldest).total_seconds() / 86400) if oldest else 0.0
    return {root: _pace(days, row["imports"], row["imported"], row["removed"])
            for root, row in per_root.items()}


def combined(rows: list[dict]) -> dict:
    """Several root folders of one service as one. They share a history, so
    they share its length."""
    days = max((row["days"] for row in rows), default=0.0)
    return _pace(days, sum(row["imports"] for row in rows),
                 sum(row["imported_gb"] for row in rows) * GB,
                 sum(row["removed_gb"] for row in rows) * GB)


def _pace(days: float, imports: int, imported: float, removed: float) -> dict:
    known = days >= MIN_GROWTH_DAYS and imports >= MIN_GROWTH_IMPORTS
    net = (imported - removed) / GB
    return {
        "days": round(days, 1), "imports": imports,
        "imported_gb": round(imported / GB, 1),
        "removed_gb": round(removed / GB, 1),
        "net_gb": round(net, 1),
        "gb_per_month": round(net / days * 30, 1) if known else None,
        "known": known,
        "reason": None if known else (
            "storage.reason.short_history" if days < MIN_GROWTH_DAYS
            else "storage.reason.few_imports"),
    }


def _root_for(path: str, roots: list[str]) -> str | None:
    best = None
    for root in roots:
        clean = root.rstrip("/")
        if (path == clean or path.startswith(clean + "/")) and (
                best is None or len(clean) > len(best.rstrip("/"))):
            best = root
    return best


def _moment(value) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def outlook(free_gb: float | None, gb_per_month: float | None,
            now: datetime | None = None) -> dict:
    """When the disk is full at the pace it has been filling."""
    if free_gb is None or gb_per_month is None:
        return {"outlook": "unknown", "months_left": None, "full_by": None}
    if gb_per_month <= 0.5:
        # Not filling, or so slowly that a date decades out means nothing.
        return {"outlook": "steady" if gb_per_month >= -0.5 else "shrinking",
                "months_left": None, "full_by": None}
    months = free_gb / gb_per_month
    full = (now or datetime.now(UTC)) + timedelta(days=months * 30)
    return {"outlook": "months", "months_left": round(months, 1),
            "full_by": full.date().isoformat()}


# ---------------------------------------------------------------------------
# What is there now
# ---------------------------------------------------------------------------
def breakdown(chosen: list[Title]) -> dict:
    """The library by resolution and by quality, and its largest titles.

    Counted from the files whose quality is known. For a series library that
    is the series that were read, and the answer says how many.
    """
    by_quality: dict[str, dict] = {}
    for title in chosen:
        for quality, size in title.file_qualities or []:
            row = by_quality.setdefault(quality or "other",
                                        {"files": 0, "bytes": 0})
            row["files"] += 1
            row["bytes"] += size
    by_resolution: dict[str, dict] = {}
    for quality, row in by_quality.items():
        rung = _resolution_of(quality) if quality != "other" else "other"
        into = by_resolution.setdefault(rung, {"files": 0, "bytes": 0})
        into["files"] += row["files"]
        into["bytes"] += row["bytes"]

    def listed(rows: dict, name: str, order) -> list[dict]:
        return [{name: key, "files": row["files"], "gb": round(row["bytes"] / GB, 1)}
                for key, row in sorted(rows.items(), key=lambda kv: order(kv[0]))]

    qualities = listed(by_quality, "quality",
                       lambda q: _rank(q) if q != "other" else (-2, 0))
    for row in qualities:
        row["name"] = label(row["quality"]) if row["quality"] != "other" else None
    largest = sorted((t for t in chosen if t.size), key=lambda t: t.size,
                     reverse=True)[:10]
    return {
        "titles": len(chosen),
        "with_files": sum(1 for t in chosen if t.files),
        "files": sum(t.files for t in chosen),
        "size_gb": round(sum(t.size for t in chosen) / GB, 1),
        "by_resolution": listed(by_resolution, "resolution",
                                lambda r: RESOLUTIONS.index(r)
                                if r in RESOLUTIONS else -1),
        "by_quality": qualities,
        "read": sum(1 for t in chosen if t.file_qualities is not None),
        "largest": [{"id": t.id, "title": t.title, "year": t.year,
                     "gb": round(t.size / GB, 1),
                     "quality": _main_quality(t)} for t in largest],
    }


def _main_quality(title: Title) -> str | None:
    weights: Counter = Counter()
    for quality, size in title.file_qualities or []:
        weights[quality] += size
    return weights.most_common(1)[0][0] if weights else None
