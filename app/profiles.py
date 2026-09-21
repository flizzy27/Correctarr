"""Building a quality profile from a handful of answers.

Radarr and Sonarr decide what to grab with two things: a ladder of qualities,
and a pile of *custom formats* that add and subtract points from a release.
Getting that pile right is the part nobody enjoys. It is also the part that
goes wrong quietly — a profile that looks sensible can grab the same release
every hour for a week.

So this asks six questions a person can actually answer — how good, how loud,
which language, which codec, how big, may it be replaced — and writes the rest.

Why a profile loops, and what is done about it here
---------------------------------------------------
A download loop is not a mystery. It happens when the service believes the file
it already has is worse than something it can go and fetch, **and it is wrong
about that**, so it fetches the same release again, and again.

There is exactly one setting that decides whether that is possible:
*Upgrade Until Custom Format Score*. It means "keep upgrading until the file
scores this". Published profiles usually set it to something enormous — ten
thousand, fifty thousand — on the reasoning that you always want the best
available. That works right up until a file scores lower after import than the
release scored before it, which happens whenever the name the file was grabbed
under is not the name it ends up with. Then the file is permanently below the
target, and the service is permanently shopping.

Here it is set to the same number a release had to clear to be grabbed at all.
The reasoning is the whole of it: **a file that is good enough to fetch is good
enough to keep.** Once it has cleared the bar, there is nothing further to
chase, and no arithmetic that can make the service think otherwise.

Quality upgrades still happen — 720p to 1080p — because those are governed by
the ladder, which is finite and ordered. A ladder cannot loop.

Two more guards:

* ``minUpgradeFormatScore`` asks for a *meaningful* improvement before anything
  is replaced, so two releases a handful of points apart cannot take turns.
* :func:`check` refuses to build a profile that could never be satisfied — a
  cutoff pointing at a quality that is switched off, or a requirement no
  release can meet. Both of those make the service search forever.

Why the language is not set as the profile's language
-----------------------------------------------------
Because it does not work, and the failure is silent. Before a file is imported
the service reads the language out of the **name**, and the German marker
``.DL.`` (German plus original audio) is not something it knows. Those releases
report English. A profile that demands German therefore throws away exactly the
German releases it was set up to find.

So the language is left as "any" and asked for with a custom format instead,
which reads the raw release name where the marker actually is. This is measured,
not assumed: see ``app/scoring.py``.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: Everything this builds is named with it, so a profile it wrote can be told
#: apart from one somebody made by hand — and so it can be written again
#: without leaving a second copy behind.
MARK = "Correctarr"


# ---------------------------------------------------------------------------
# What can be asked for
# ---------------------------------------------------------------------------
#: The rungs of the ladder, worst first. The names are matched against whatever
#: the service calls its qualities, because the numbers behind them are not the
#: same in Radarr and Sonarr and have changed between versions.
RESOLUTIONS = ("720p", "1080p", "2160p")

#: Which of the service's qualities belong to each rung. Matched by name, case
#: insensitively, against the profile schema the service hands out.
QUALITY_NAMES: dict[str, tuple[str, ...]] = {
    "720p": ("HDTV-720p", "WEBDL-720p", "WEBRip-720p", "Bluray-720p"),
    "1080p": ("HDTV-1080p", "WEBDL-1080p", "WEBRip-1080p", "Bluray-1080p",
              "Bluray-1080p Remux", "Remux-1080p"),
    "2160p": ("HDTV-2160p", "WEBDL-2160p", "WEBRip-2160p", "Bluray-2160p",
              "Bluray-2160p Remux", "Remux-2160p"),
}

#: The remux rungs, kept apart because they are enormous and not everybody
#: wants them even at the resolution they asked for.
REMUX_NAMES = ("Bluray-1080p Remux", "Remux-1080p",
               "Bluray-2160p Remux", "Remux-2160p")

#: How good the sound has to be. Four steps, worst first; picking one asks for
#: it **and** everything above it, with more points the further up it goes.
AUDIO_TIERS = ("nah", "ok", "gut", "sehr_gut")

#: What each step is called in the formats written into the service. The keys
#: above are what the interface talks about and can be shown in any language;
#: these end up as names inside Radarr and Sonarr, where everything is English.
AUDIO_TIER_NAMES = {"nah": "basic", "ok": "ok", "gut": "good",
                    "sehr_gut": "very good"}

#: The languages offered, in the order they are offered. German and English
#: first because that is what this is used for; the rest are there so nobody
#: has to go without.
LANGUAGES = ("de", "en", "fr", "es", "it", "nl", "pl", "pt", "ru", "ja", "ko")

CODECS = ("any", "x265", "x264")


@dataclass(frozen=True)
class Wish:
    """The answers. Everything else is derived from these."""
    name: str = "Correctarr 1080p"
    resolutions: tuple[str, ...] = ("1080p",)
    allow_remux: bool = False
    audio: str = "gut"
    codec: str = "any"
    languages: tuple[str, ...] = ("de",)
    language_required: bool = False
    allow_3d: bool = False
    prefer_hdr: bool = False
    block_rubbish: bool = True
    min_gb: float = 0.0
    max_gb: float = 0.0
    upgrade: bool = True

    def tidy(self) -> Wish:
        """The same wish with anything nonsensical straightened out."""
        resolutions = tuple(r for r in RESOLUTIONS if r in self.resolutions)
        languages = tuple(code for code in LANGUAGES if code in self.languages)
        return Wish(
            name=(self.name or "Correctarr").strip()[:60],
            resolutions=resolutions or ("1080p",),
            allow_remux=bool(self.allow_remux),
            audio=self.audio if self.audio in AUDIO_TIERS else "gut",
            codec=self.codec if self.codec in CODECS else "any",
            languages=languages,
            language_required=bool(self.language_required and languages),
            allow_3d=bool(self.allow_3d),
            prefer_hdr=bool(self.prefer_hdr),
            block_rubbish=bool(self.block_rubbish),
            min_gb=max(0.0, min(2000.0, float(self.min_gb or 0))),
            max_gb=max(0.0, min(2000.0, float(self.max_gb or 0))),
            upgrade=bool(self.upgrade),
        )


# ---------------------------------------------------------------------------
# The points
# ---------------------------------------------------------------------------
# Deliberately small numbers. The published profiles work in tens of thousands
# so that one preference can overwhelm every other; the cost of that is that
# nobody can hold the arithmetic in their head, including the person who wrote
# it. Here a release is worth its language, plus a little for sound, codec and
# range — and anything unwanted is worth less than nothing by a margin no
# collection of bonuses can climb back over.
WANTED_LANGUAGE = 1000       # the first language asked for
OTHER_LANGUAGE = 700         # any further one
AUDIO_STEP = 100             # per step above the one asked for
CODEC_BONUS = 150
HDR_BONUS = 120
UNWANTED = -3000             # 3D when it is not wanted, rubbish, wrong size

#: How much better a release has to be before anything already on disk is
#: replaced. Without a step, two releases a few points apart can take turns
#: being "the better one" for as long as both exist.
UPGRADE_STEP = 100


@dataclass
class Format:
    """One custom format, as this program wants it to exist."""
    name: str
    score: int
    #: ``(implementation, fields, negate, required)`` per condition.
    conditions: list[tuple[str, dict[str, Any], bool, bool]] = field(
        default_factory=list)
    #: Why it is there, in the user's language, for the preview.
    why: str = ""
    why_params: dict = field(default_factory=dict)

    @property
    def full_name(self) -> str:
        return f"{MARK}: {self.name}"


@dataclass
class Blueprint:
    """Everything that has to exist for one profile, and what it will do."""
    wish: Wish
    formats: list[Format]
    min_score: int
    cutoff_score: int
    resolutions: tuple[str, ...]
    notes: list[tuple[str, dict]] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "name": self.wish.name,
            "formats": [{"name": f.full_name, "score": f.score,
                         "why": f.why, "why_params": f.why_params}
                        for f in self.formats],
            "min_score": self.min_score,
            "cutoff_score": self.cutoff_score,
            "upgrade_step": UPGRADE_STEP,
            "resolutions": list(self.resolutions),
            "upgrade": self.wish.upgrade,
            "notes": [{"key": key, "params": params} for key, params in self.notes],
        }


# ---------------------------------------------------------------------------
# The patterns
# ---------------------------------------------------------------------------
# Written against the raw release name, which is where these markers live. The
# service itself does not always see them — its parser splits the recognised
# title off first and a marker in front of the year goes with it — but a custom
# format's title condition is applied to the whole name, so they work here.
LANGUAGE_PATTERNS: dict[str, str] = {
    # German, including the dual-language spellings. ".DL." is German plus the
    # original audio and is by far the most common shape in practice; a rule
    # that only looks for the word "German" misses most of them. The bracket
    # form at the end is what the trackers that label both tracks use.
    #
    # One (?i) and it is at the front. Inline flags part way through are
    # accepted by .NET and by the `regex` module and refused by Python's own,
    # which is what :func:`check` validates with — on purpose, because it is
    # the strictest of the three and a pattern that passes it works everywhere.
    "de": r"(?i)(?:(?<![a-z0-9])(german|deutsch|ger[. _-]?dub|synchro"
          r"|g(?:er)?[. _-]?dl|dl)(?![a-z0-9])"
          r"|\[(?:de|ger)(?:\s*[+,]\s*[a-z]{2,3})*\])",
    "en": r"(?i)(?<![a-z0-9])(english|eng)(?![a-z0-9])",
    "fr": r"(?i)(?<![a-z0-9])(french|vff|vfq|truefrench)(?![a-z0-9])",
    "es": r"(?i)(?<![a-z0-9])(spanish|castellano|espanol)(?![a-z0-9])",
    "it": r"(?i)(?<![a-z0-9])(italian|ita)(?![a-z0-9])",
    "nl": r"(?i)(?<![a-z0-9])(dutch|nl[. _-]?subbed)(?![a-z0-9])",
    "pl": r"(?i)(?<![a-z0-9])(polish|pl[. _-]?dub|lektor)(?![a-z0-9])",
    "pt": r"(?i)(?<![a-z0-9])(portuguese|dublado)(?![a-z0-9])",
    "ru": r"(?i)(?<![a-z0-9])(russian|rus)(?![a-z0-9])",
    "ja": r"(?i)(?<![a-z0-9])(japanese|jap|jpn)(?![a-z0-9])",
    "ko": r"(?i)(?<![a-z0-9])(korean|kor)(?![a-z0-9])",
}

#: Sound, worst tier first. Picking a tier asks for that one and everything
#: above it; each step up is worth another :data:`AUDIO_STEP`.
AUDIO_PATTERNS: dict[str, str] = {
    "nah": r"(?i)(?<![a-z0-9])(mp3|aac2|aac[. _-]?2[. _-]?0|opus)(?![a-z0-9])",
    "ok": r"(?i)(?<![a-z0-9])(ac3|dd[0-9]?|dolby[. _-]?digital|aac)(?![a-z0-9])",
    # "DTS" on its own, but not DTS-HD and not DTS-X. The digits in the
    # lookahead are not decoration: "Movie.2020.DTS.x264" is plain DTS next to
    # an x264 video stream, and reading that "x" as DTS-X promotes every
    # ordinary release to the top tier.
    "gut": r"(?i)(?<![a-z0-9])(eac3|ddp|dd\+"
           r"|dts(?![. _-]?hd)(?![. _-]?x(?![0-9]))"
           r"|dolby[. _-]?digital[. _-]?plus)(?![a-z0-9])",
    "sehr_gut": r"(?i)(?<![a-z0-9])(truehd|true[. _-]?hd|atmos"
                r"|dts[. _-]?x(?![0-9])"
                r"|dts[. _-]?hd([. _-]?ma)?|flac)(?![a-z0-9])",
}

CODEC_PATTERNS = {
    "x265": r"(?i)(?<![a-z0-9])(x265|h[. _-]?265|hevc)(?![a-z0-9])",
    "x264": r"(?i)(?<![a-z0-9])(x264|h[. _-]?264|avc)(?![a-z0-9])",
}

#: Anything with two pictures in it. Worth saying out loud: the service cannot
#: see most of these on its own, because its parser swallows a marker that
#: stands in front of the year — measured, and written up in ``app/scoring.py``.
THREE_D_PATTERN = (r"(?i)(?<![a-z0-9])(3d|hsbs|h[. _-]?sbs|sbs|htab|h[. _-]?tab"
                   r"|hou|half[. _-]?ou)(?![a-z0-9])")

#: A camera in a cinema, a screener, a disc image, and a small picture blown up
#: to look like a big one. None of these is what anybody meant.
RUBBISH_PATTERN = (r"(?i)(?<![a-z0-9])(cam(rip)?|hdcam|ts|telesync|tc|telecine"
                   r"|scr|screener|dvdscr|workprint|br[. _-]?disk|bdmv|avchd"
                   r"|upscaled?|ai[. _-]?upscale)(?![a-z0-9])")

HDR_PATTERN = (r"(?i)(?<![a-z0-9])(hdr10\+?|hdr|dv|dovi|dolby[. _-]?vision"
               r"|pq|hlg)(?![a-z0-9])")


def _title(pattern: str, *, negate: bool = False,
           required: bool = False) -> tuple[str, dict, bool, bool]:
    return ("ReleaseTitleSpecification", {"value": pattern}, negate, required)


def _size(low: float, high: float) -> tuple[str, dict, bool, bool]:
    # The service compares "greater than min, less than or equal to max".
    return ("SizeSpecification", {"min": low, "max": high}, False, False)


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------
def build(wish: Wish) -> Blueprint:
    """Turn the answers into the formats and the numbers they imply."""
    wish = wish.tidy()
    formats: list[Format] = []
    notes: list[tuple[str, dict]] = []

    # -- language ----------------------------------------------------------
    for index, code in enumerate(wish.languages):
        score = WANTED_LANGUAGE if index == 0 else OTHER_LANGUAGE
        formats.append(Format(
            name=f"Language {code.upper()}", score=score,
            conditions=[_title(LANGUAGE_PATTERNS[code])],
            why="profiles.why.language", why_params={"language": code.upper()}))

    # -- sound -------------------------------------------------------------
    floor = AUDIO_TIERS.index(wish.audio)
    for step, tier in enumerate(AUDIO_TIERS[floor:], start=1):
        formats.append(Format(
            name=f"Audio {AUDIO_TIER_NAMES[tier]}", score=AUDIO_STEP * step,
            conditions=[_title(AUDIO_PATTERNS[tier])],
            why="profiles.why.audio", why_params={"tier": tier}))

    # -- codec -------------------------------------------------------------
    if wish.codec != "any":
        formats.append(Format(
            name=f"Codec {wish.codec}", score=CODEC_BONUS,
            conditions=[_title(CODEC_PATTERNS[wish.codec])],
            why="profiles.why.codec", why_params={"codec": wish.codec}))

    # -- range -------------------------------------------------------------
    if wish.prefer_hdr:
        formats.append(Format(
            name="HDR", score=HDR_BONUS,
            conditions=[_title(HDR_PATTERN)], why="profiles.why.hdr"))

    # -- what is not wanted ------------------------------------------------
    # These are worth less than nothing by a margin no collection of bonuses
    # can climb back over, so a release carrying one is refused rather than
    # merely ranked last. Ranking it last still grabs it when it is the only
    # thing there.
    if not wish.allow_3d:
        formats.append(Format(
            name="3D", score=UNWANTED,
            conditions=[_title(THREE_D_PATTERN)], why="profiles.why.no_3d"))
    if wish.block_rubbish:
        formats.append(Format(
            name="Cam and upscale", score=UNWANTED,
            conditions=[_title(RUBBISH_PATTERN)], why="profiles.why.rubbish"))

    if wish.min_gb > 0:
        formats.append(Format(
            name=f"Under {_tidy(wish.min_gb)} GB", score=UNWANTED,
            conditions=[_size(0.0, wish.min_gb)],
            why="profiles.why.too_small", why_params={"gb": _tidy(wish.min_gb)}))
    if wish.max_gb > 0:
        formats.append(Format(
            name=f"Over {_tidy(wish.max_gb)} GB", score=UNWANTED,
            conditions=[_size(wish.max_gb, 2000.0)],
            why="profiles.why.too_large", why_params={"gb": _tidy(wish.max_gb)}))

    # -- the two numbers that decide whether this can loop -----------------
    #
    # The floor a release has to clear to be grabbed at all. Zero unless a
    # language was made compulsory, in which case it is exactly the first
    # language's score: enough to demand it, never more.
    min_score = WANTED_LANGUAGE if wish.language_required else 0

    # And the one that matters. "Keep upgrading until the file scores this."
    # Set to the same floor, which says: a file that was good enough to fetch
    # is good enough to keep. Anything higher is a standing instruction to go
    # shopping, and it is obeyed forever the moment a file scores lower after
    # import than its release scored before it.
    cutoff_score = min_score

    if wish.language_required:
        notes.append(("profiles.note.language_required",
                      {"language": wish.languages[0].upper()}))
    if wish.max_gb and wish.min_gb and wish.max_gb <= wish.min_gb:
        notes.append(("profiles.note.size_window", {}))
    if "2160p" in wish.resolutions and not wish.max_gb:
        notes.append(("profiles.note.big_files", {}))

    return Blueprint(wish=wish, formats=formats, min_score=min_score,
                     cutoff_score=cutoff_score, resolutions=wish.resolutions,
                     notes=notes)


def _tidy(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


# ---------------------------------------------------------------------------
# Refusing to build something that cannot work
# ---------------------------------------------------------------------------
def check(blueprint: Blueprint) -> list[tuple[str, dict]]:
    """Everything wrong with this profile, as translation keys.

    Not style. Each of these is a way for the service to end up searching or
    grabbing without end, which is the failure this whole module exists to
    avoid.
    """
    problems: list[tuple[str, dict]] = []
    wish = blueprint.wish

    if not blueprint.resolutions:
        problems.append(("profiles.problem.no_quality", {}))

    # A target above the floor is the loop. Stated as a check rather than left
    # to the builder so that it stays true if the builder ever changes.
    if blueprint.cutoff_score > blueprint.min_score:
        problems.append(("profiles.problem.cutoff_above_floor", {
            "cutoff": blueprint.cutoff_score, "floor": blueprint.min_score}))

    # A floor nothing can reach means nothing is ever grabbed, and the service
    # keeps looking for something that is not there.
    best = sum(f.score for f in blueprint.formats if f.score > 0)
    if blueprint.min_score > best:
        problems.append(("profiles.problem.floor_too_high", {
            "floor": blueprint.min_score, "best": best}))

    if wish.min_gb and wish.max_gb and wish.min_gb >= wish.max_gb:
        problems.append(("profiles.problem.impossible_size", {
            "min": _tidy(wish.min_gb), "max": _tidy(wish.max_gb)}))

    if wish.language_required and not wish.languages:
        problems.append(("profiles.problem.language_without_one", {}))

    for pattern in _every_pattern(blueprint):
        try:
            re.compile(pattern)
        except re.error as e:
            problems.append(("profiles.problem.bad_pattern", {"error": str(e)}))
    return problems


def _every_pattern(blueprint: Blueprint):
    for entry in blueprint.formats:
        for implementation, fields, _negate, _required in entry.conditions:
            if implementation == "ReleaseTitleSpecification":
                yield str(fields.get("value") or "")


# ---------------------------------------------------------------------------
# Putting it there
# ---------------------------------------------------------------------------
def _schema_for(schemas: list[dict], implementation: str) -> dict | None:
    for entry in schemas:
        if entry.get("implementation") == implementation:
            return entry
    return None


def specification(schemas: list[dict], implementation: str, values: dict,
                  *, negate: bool, required: bool) -> dict:
    """One condition, built from the service's own template.

    The field names inside a specification are not guessed. The service hands
    out a template for each kind it supports, and this fills that in — so a
    version that renames a field, or adds one, is a non-event rather than a
    rejected profile.
    """
    template = _schema_for(schemas, implementation)
    if template is None:
        raise ValueError(f"error.unknown_specification|{implementation}")
    fields = []
    for entry in template.get("fields") or []:
        name = entry.get("name")
        fields.append({"name": name,
                       "value": values.get(name, entry.get("value"))})
    return {
        "name": template.get("implementationName") or implementation,
        "implementation": implementation,
        "implementationName": template.get("implementationName", implementation),
        "infoLink": template.get("infoLink"),
        "negate": negate, "required": required, "fields": fields,
    }


def custom_format_body(schemas: list[dict], entry: Format) -> dict:
    return {
        "name": entry.full_name,
        "includeCustomFormatWhenRenaming": False,
        "specifications": [
            specification(schemas, implementation, values,
                          negate=negate, required=required)
            for implementation, values, negate, required in entry.conditions
        ],
    }


def quality_items(schema_items: list[dict], wanted: tuple[str, ...],
                  allow_remux: bool) -> tuple[list[dict], int | None]:
    """The ladder, and the rung to stop at.

    Built from the ladder the service itself hands out, with everything the
    person did not ask for switched off. The numbers behind the names are not
    the same in Radarr and Sonarr and have moved between versions, so nothing
    here is hard coded except the names.
    """
    names: set[str] = set()
    for rung in wanted:
        for name in QUALITY_NAMES.get(rung, ()):
            if not allow_remux and name in REMUX_NAMES:
                continue
            names.add(name.lower())

    items: list[dict] = []
    best_id: int | None = None
    for raw in schema_items:
        entry = dict(raw)
        nested = entry.get("items") or []
        if nested:
            children = []
            group_on = False
            for child_raw in nested:
                child = dict(child_raw)
                quality = child.get("quality") or {}
                on = str(quality.get("name", "")).lower() in names
                child["allowed"] = on
                group_on = group_on or on
                children.append(child)
                if on:
                    best_id = quality.get("id", best_id)
            entry["items"] = children
            entry["allowed"] = group_on
            if group_on:
                best_id = entry.get("id", best_id)
        else:
            quality = entry.get("quality") or {}
            on = str(quality.get("name", "")).lower() in names
            entry["allowed"] = on
            entry["items"] = []
            if on:
                best_id = quality.get("id", best_id)
        items.append(entry)
    return items, best_id


def profile_body(blueprint: Blueprint, items: list[dict], cutoff: int,
                 format_items: list[dict], language: dict | None) -> dict:
    """The profile itself.

    ``language`` is deliberately whatever the service calls "any". Demanding a
    language here throws away the releases it was meant to find: before the
    import the language is read out of the name, and the German marker ``.DL.``
    is not one the parser knows. The language is asked for with a custom format
    instead, which sees the whole name.
    """
    body = {
        "name": blueprint.wish.name,
        "upgradeAllowed": blueprint.wish.upgrade,
        "cutoff": cutoff,
        "items": items,
        "minFormatScore": blueprint.min_score,
        "cutoffFormatScore": blueprint.cutoff_score,
        "minUpgradeFormatScore": UPGRADE_STEP,
        "formatItems": format_items,
    }
    if language is not None:
        body["language"] = language
    return body


def any_language(languages: list[dict]) -> dict | None:
    """Whatever this service calls "any language"."""
    for entry in languages:
        if str(entry.get("name", "")).lower() in ("any", "original"):
            return {"id": entry.get("id"), "name": entry.get("name")}
    return None


# ---------------------------------------------------------------------------
# Writing it into a service
# ---------------------------------------------------------------------------
def apply_to(arr, blueprint: Blueprint) -> dict:
    """Create or update the formats and the profile on one service.

    Everything it writes is named after this program, and anything by that
    name that is already there is **updated** rather than added beside. Saving
    twice is meant to be dull: the second time changes the same profile, it
    does not leave a second one called "Deutsch 1080p (1)" behind.

    Anything not named after this program is never touched. A profile somebody
    made by hand is theirs.
    """
    problems = check(blueprint)
    if problems:
        raise ValueError("|".join(key for key, _params in problems))

    schemas = arr.custom_format_schema()
    existing = {str(f.get("name")): f for f in arr.custom_formats()}

    written: list[dict] = []
    for entry in blueprint.formats:
        body = custom_format_body(schemas, entry)
        there = existing.get(entry.full_name)
        saved = arr.save_custom_format(body, there.get("id") if there else None)
        written.append({"id": saved.get("id") or (there or {}).get("id"),
                        "name": entry.full_name, "score": entry.score})

    # Every format the profile knows about, not only ours: a profile that
    # lists some of them and not others is rejected by the service, and one
    # that silently drops somebody else's format would quietly change what
    # their other profiles do.
    by_name = {str(f.get("name")): f for f in arr.custom_formats()}
    scores = {row["name"]: row["score"] for row in written}
    format_items = [{"format": f.get("id"), "name": name,
                     "score": scores.get(name, 0)}
                    for name, f in by_name.items()]

    schema = arr.quality_profile_schema()
    items, best = quality_items(schema.get("items") or [],
                                blueprint.resolutions,
                                blueprint.wish.allow_remux)
    if best is None:
        raise ValueError("profiles.problem.no_quality")

    language = any_language(arr.languages())
    body = profile_body(blueprint, items, best, format_items, language)

    mine = next((p for p in arr.profiles()
                 if str(p.get("name")) == blueprint.wish.name), None)
    saved = arr.save_quality_profile(body, mine.get("id") if mine else None)
    return {"service": arr.name, "kind": arr.kind,
            "profile": saved.get("name") or blueprint.wish.name,
            "profile_id": saved.get("id") or (mine or {}).get("id"),
            "formats": len(written), "updated": mine is not None}
