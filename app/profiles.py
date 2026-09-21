"""Building a quality profile from a handful of answers.

Radarr and Sonarr decide what to grab with two things: a ladder of qualities,
and a pile of *custom formats* that add and subtract points from a release.
Getting that pile right is the part nobody enjoys. It is also the part that
goes wrong quietly — a profile that looks sensible can grab the same release
every hour for a week.

So this asks questions a person can actually answer — how many pixels, off
what, how loud, in which language, from whom, how big, may it be replaced — and
writes the rest: a ladder narrowed to what was asked for, and two dozen custom
formats with numbers on them that agree with one another.

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

#: Where a release came from, worst first. This is the other half of the
#: ladder: "1080p" says how many pixels, a source says what they were made
#: from, and a disc rip and a broadcast capture at the same resolution are not
#: the same thing at all.
SOURCES = ("hdtv", "webrip", "webdl", "bluray", "remux")

#: Which of the service's quality names belong to each source, per rung.
SOURCE_NAMES: dict[str, tuple[str, ...]] = {
    "hdtv": ("HDTV-720p", "HDTV-1080p", "HDTV-2160p"),
    "webrip": ("WEBRip-720p", "WEBRip-1080p", "WEBRip-2160p"),
    "webdl": ("WEBDL-720p", "WEBDL-1080p", "WEBDL-2160p"),
    "bluray": ("Bluray-720p", "Bluray-1080p", "Bluray-2160p"),
    "remux": ("Bluray-1080p Remux", "Remux-1080p",
              "Bluray-2160p Remux", "Remux-2160p"),
}

#: How much colour. "dv" also asks for the HDR10 fallback, because a Dolby
#: Vision file without one plays green and washed out on a television that
#: does not speak Dolby Vision — which is most of them.
RANGES = ("sdr", "hdr", "dv")

#: The services people actually pull from, for anyone who has a preference.
#: These are release-name markers, not the companies.
STREAMERS = ("amzn", "nf", "dsnp", "atvp", "max", "hulu", "pcok", "sky")

#: Editions worth preferring when they exist.
EDITIONS = ("none", "extended", "theatrical", "imax")

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
    sources: tuple[str, ...] = ("webdl", "bluray")
    audio: str = "gut"
    surround: bool = True
    codec: str = "any"
    languages: tuple[str, ...] = ("de",)
    language_required: bool = False
    colour: str = "sdr"
    edition: str = "none"
    streamers: tuple[str, ...] = ()
    good_groups: bool = True
    prefer_repack: bool = True
    allow_3d: bool = False
    block_rubbish: bool = True
    block_hardcoded_subs: bool = True
    block_retagged: bool = True
    min_gb: float = 0.0
    max_gb: float = 0.0
    upgrade: bool = True

    #: Kept so a wish stored by an earlier build still loads. Remux is a
    #: source now, not a switch beside the resolutions.
    allow_remux: bool = False

    def tidy(self) -> Wish:
        """The same wish with anything nonsensical straightened out."""
        resolutions = tuple(r for r in RESOLUTIONS if r in self.resolutions)
        languages = tuple(code for code in LANGUAGES if code in self.languages)
        sources = tuple(s for s in SOURCES if s in self.sources)
        if self.allow_remux and "remux" not in sources:
            sources = (*sources, "remux")
        return Wish(
            name=(self.name or "Correctarr").strip()[:60],
            resolutions=resolutions or ("1080p",),
            sources=sources or ("webdl", "bluray"),
            audio=self.audio if self.audio in AUDIO_TIERS else "gut",
            surround=bool(self.surround),
            codec=self.codec if self.codec in CODECS else "any",
            languages=languages,
            language_required=bool(self.language_required and languages),
            colour=self.colour if self.colour in RANGES else "sdr",
            edition=self.edition if self.edition in EDITIONS else "none",
            streamers=tuple(x for x in STREAMERS if x in self.streamers),
            good_groups=bool(self.good_groups),
            prefer_repack=bool(self.prefer_repack),
            allow_3d=bool(self.allow_3d),
            block_rubbish=bool(self.block_rubbish),
            block_hardcoded_subs=bool(self.block_hardcoded_subs),
            block_retagged=bool(self.block_retagged),
            min_gb=max(0.0, min(2000.0, float(self.min_gb or 0))),
            max_gb=max(0.0, min(2000.0, float(self.max_gb or 0))),
            upgrade=bool(self.upgrade),
            allow_remux="remux" in sources,
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
SURROUND_BONUS = 80
CODEC_BONUS = 150
SOURCE_STEP = 60             # per step up the source order
HDR_BONUS = 120
DV_BONUS = 140
GROUP_BONUS = 200            # a group with a reputation for getting it right
GROUP_SECOND = 90
STREAMER_BONUS = 70
EDITION_BONUS = 110
REPACK_BONUS = 60
#: The least a refusal is ever worth. The real figure is worked out per
#: profile, because it has to beat every bonus that profile hands out put
#: together — see :func:`refusal`. A fixed number was wrong the moment the
#: builder learned a new preference: with enough of them, a release with a
#: camera in front of the screen could collect more in bonuses than the
#: refusal took away and be grabbed on merit.
UNWANTED = -3000

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

HDR_PATTERN = (r"(?i)(?<![a-z0-9])(hdr10\+?|hdr|pq|hlg)(?![a-z0-9])")

DV_PATTERN = (r"(?i)(?<![a-z0-9])(dv|dovi|dolby[. _-]?vision)(?![a-z0-9])")

#: Dolby Vision carried on its own, with no HDR10 layer underneath it. On a
#: television that does not speak Dolby Vision — which is most of them — such a
#: file plays washed out and green. Worth refusing rather than ranking last,
#: because it looks like the best release in the list right up until it plays.
DV_NO_FALLBACK = (r"(?i)^(?=.*(?<![a-z0-9])(dv|dovi|dolby[. _-]?vision)(?![a-z0-9]))"
                  r"(?!.*(?<![a-z0-9])(hdr10|hdr|hybrid)(?![a-z0-9])).*$")

#: Where the picture came from. "WEB-DL" is the stream as it was served;
#: "WEBRip" is that stream re-encoded by somebody, which is not the same.
SOURCE_PATTERNS: dict[str, str] = {
    "hdtv": r"(?i)(?<![a-z0-9])(hdtv|pdtv|dsr|dtheater)(?![a-z0-9])",
    "webrip": r"(?i)(?<![a-z0-9])(web[. _-]?rip|webhd)(?![a-z0-9])",
    "webdl": r"(?i)(?<![a-z0-9])(web[. _-]?dl|web(?![. _-]?rip))(?![a-z0-9])",
    "bluray": r"(?i)(?<![a-z0-9])(blu[. _-]?ray|bd[. _-]?rip|br[. _-]?rip"
              r"|bdr(?![a-z0-9]))(?![a-z0-9])",
    "remux": r"(?i)(?<![a-z0-9])(remux)(?![a-z0-9])",
}

#: Surround, as opposed to two speakers. Written as a channel count because
#: that is how release names state it.
SURROUND_PATTERN = (r"(?i)(?<![0-9])(5[. _]?1|7[. _]?1|6[. _]?1"
                    r"|ddp?5|ddp?7|atmos)(?![0-9])")

#: Groups with a reputation for getting the encode right, in two tiers. Kept
#: short and uncontroversial on purpose: a long list of names is a list that
#: goes out of date, and a group that has gone quiet scoring nothing is
#: harmless while a bad guess is not.
GOOD_GROUPS_FIRST = (r"(?i)-(framestor|flux|ctrlhd|decibel|hqmux|esir|tayto"
                     r"|nima4k|zq|blurayd(esu)?m|w4nk3r|hifi|geek)(?![a-z0-9])")
GOOD_GROUPS_SECOND = (r"(?i)-(sartre|hone|ntb|tepes|t6d|nosivid|dvsux|kitsune"
                      r"|playweb|scenetime|cmrg|evo(?!lve)|ggez|ggwp|flame"
                      r"|trollhd|trolluhd)(?![a-z0-9])")

#: Burned into the picture and impossible to switch off.
HARDCODED_SUBS = (r"(?i)(?<![a-z0-9])(hc|hardcoded|hard[. _-]?sub(bed|s)?"
                  r"|korsub|vostfr|subbed)(?![a-z0-9])")

#: A name that has been scrambled to get past a filter, or stripped of the
#: group that made it. What is inside is anybody's guess, and the service
#: cannot parse it either.
RETAGGED = (r"(?i)(?<![a-z0-9])(obfuscated|scrambled|postbot|xpost|rartv"
            r"|rarbg|1xbet|mrn|qxr|nogr(ou)?p|nogrp)(?![a-z0-9])")

#: A release put out again because the first one was broken. Worth a little:
#: it is the same thing, fixed.
REPACK_PATTERN = r"(?i)(?<![a-z0-9])(repack[0-9]?|proper[0-9]?|real)(?![a-z0-9])"

EDITION_PATTERNS: dict[str, str] = {
    "extended": r"(?i)(?<![a-z0-9])(extended|director[' ._-]?s?[. _-]?cut"
                r"|uncut|unrated|langfassung|final[. _-]?cut)(?![a-z0-9])",
    "theatrical": r"(?i)(?<![a-z0-9])(theatrical|kinofassung)(?![a-z0-9])",
    "imax": r"(?i)(?<![a-z0-9])(imax|open[. _-]?matte)(?![a-z0-9])",
}

STREAMER_PATTERNS: dict[str, str] = {
    "amzn": r"(?i)(?<![a-z0-9])(amzn|amazon)(?![a-z0-9])",
    "nf": r"(?i)(?<![a-z0-9])(nf|netflix)(?![a-z0-9])",
    "dsnp": r"(?i)(?<![a-z0-9])(dsnp|dsny|disney)(?![a-z0-9])",
    "atvp": r"(?i)(?<![a-z0-9])(atvp|appletv)(?![a-z0-9])",
    "max": r"(?i)(?<![a-z0-9])(hmax|max|hbo)(?![a-z0-9])",
    "hulu": r"(?i)(?<![a-z0-9])(hulu)(?![a-z0-9])",
    "pcok": r"(?i)(?<![a-z0-9])(pcok|peacock)(?![a-z0-9])",
    "sky": r"(?i)(?<![a-z0-9])(sky(show)?|now[. _-]?tv)(?![a-z0-9])",
}


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

    if wish.surround:
        formats.append(Format(
            name="Surround", score=SURROUND_BONUS,
            conditions=[_title(SURROUND_PATTERN)], why="profiles.why.surround"))

    # -- codec -------------------------------------------------------------
    if wish.codec != "any":
        formats.append(Format(
            name=f"Codec {wish.codec}", score=CODEC_BONUS,
            conditions=[_title(CODEC_PATTERNS[wish.codec])],
            why="profiles.why.codec", why_params={"codec": wish.codec}))

    # -- where it came from --------------------------------------------------
    # A preference in the order the sources are listed in, rather than a
    # requirement: the ladder already refuses anything not asked for, and this
    # decides between two releases that are both acceptable.
    for step, source in enumerate(
            [s for s in SOURCES if s in wish.sources], start=1):
        formats.append(Format(
            name=f"Source {source}", score=SOURCE_STEP * step,
            conditions=[_title(SOURCE_PATTERNS[source])],
            why="profiles.why.source", why_params={"source": source}))

    # -- how much colour -----------------------------------------------------
    if wish.colour in ("hdr", "dv"):
        formats.append(Format(
            name="HDR", score=HDR_BONUS,
            conditions=[_title(HDR_PATTERN)], why="profiles.why.hdr"))
    if wish.colour == "dv":
        formats.append(Format(
            name="Dolby Vision", score=DV_BONUS,
            conditions=[_title(DV_PATTERN)], why="profiles.why.dv"))

    # -- who made it ---------------------------------------------------------
    if wish.good_groups:
        formats.append(Format(
            name="Known good group", score=GROUP_BONUS,
            conditions=[_title(GOOD_GROUPS_FIRST)], why="profiles.why.groups"))
        formats.append(Format(
            name="Solid group", score=GROUP_SECOND,
            conditions=[_title(GOOD_GROUPS_SECOND)],
            why="profiles.why.groups_second"))

    if wish.prefer_repack:
        formats.append(Format(
            name="Repack or proper", score=REPACK_BONUS,
            conditions=[_title(REPACK_PATTERN)], why="profiles.why.repack"))

    if wish.edition != "none":
        formats.append(Format(
            name=f"Edition {wish.edition}", score=EDITION_BONUS,
            conditions=[_title(EDITION_PATTERNS[wish.edition])],
            why="profiles.why.edition", why_params={"edition": wish.edition}))

    for code in wish.streamers:
        formats.append(Format(
            name=f"Source {code.upper()}", score=STREAMER_BONUS,
            conditions=[_title(STREAMER_PATTERNS[code])],
            why="profiles.why.streamer", why_params={"streamer": code.upper()}))

    # -- the floor -----------------------------------------------------------
    #
    # What a release has to clear to be grabbed at all. Zero unless a language
    # was made compulsory, in which case it is exactly the first language's
    # score: enough to demand it, never more.
    min_score = WANTED_LANGUAGE if wish.language_required else 0

    # -- what is not wanted --------------------------------------------------
    # Worth less than nothing by a margin no collection of bonuses can climb
    # back over, so a release carrying one is *refused* rather than merely
    # ranked last — ranking it last still grabs it when it is the only thing
    # there. The margin is worked out from the bonuses this profile actually
    # hands out, because a fixed number stops being enough as soon as there
    # are more preferences than there used to be.
    refuse = refusal(formats, min_score)

    if wish.colour in ("hdr", "dv"):
        # Refused rather than ranked last, because it looks like the best
        # release in the list right up until it plays green on a television
        # that does not speak Dolby Vision.
        formats.append(Format(
            name="Dolby Vision without fallback", score=refuse,
            conditions=[_title(DV_NO_FALLBACK)], why="profiles.why.dv_only"))
    if not wish.allow_3d:
        formats.append(Format(
            name="3D", score=refuse,
            conditions=[_title(THREE_D_PATTERN)], why="profiles.why.no_3d"))
    if wish.block_rubbish:
        formats.append(Format(
            name="Cam and upscale", score=refuse,
            conditions=[_title(RUBBISH_PATTERN)], why="profiles.why.rubbish"))
    if wish.block_hardcoded_subs:
        formats.append(Format(
            name="Hardcoded subtitles", score=refuse,
            conditions=[_title(HARDCODED_SUBS)], why="profiles.why.hardcoded"))
    if wish.block_retagged:
        formats.append(Format(
            name="Retagged or scrambled", score=refuse,
            conditions=[_title(RETAGGED)], why="profiles.why.retagged"))

    if wish.min_gb > 0:
        formats.append(Format(
            name=f"Under {_tidy(wish.min_gb)} GB", score=refuse,
            conditions=[_size(0.0, wish.min_gb)],
            why="profiles.why.too_small", why_params={"gb": _tidy(wish.min_gb)}))
    if wish.max_gb > 0:
        formats.append(Format(
            name=f"Over {_tidy(wish.max_gb)} GB", score=refuse,
            conditions=[_size(wish.max_gb, 2000.0)],
            why="profiles.why.too_large", why_params={"gb": _tidy(wish.max_gb)}))

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
    if wish.codec == "x265" and "2160p" not in wish.resolutions:
        # Widely held, and true: at 1080p the saving is small and a fair number
        # of players and televisions have to transcode it, which looks worse
        # than the x264 it replaced.
        notes.append(("profiles.note.x265_at_1080p", {}))
    if wish.colour == "dv" and "2160p" not in wish.resolutions:
        notes.append(("profiles.note.dv_needs_2160p", {}))
    if wish.sources == ("remux",):
        notes.append(("profiles.note.remux_only", {}))

    return Blueprint(wish=wish, formats=formats, min_score=min_score,
                     cutoff_score=cutoff_score, resolutions=wish.resolutions,
                     notes=notes)


def refusal(formats: list[Format], min_score: int) -> int:
    """What one unwanted marker has to be worth for the answer to be no.

    Every bonus the profile can hand out, added together, plus the floor, plus
    one. A release carrying a single refused marker then cannot reach the floor
    however many other boxes it ticks — which is the difference between "we
    would rather not" and "no".
    """
    bonuses = sum(f.score for f in formats if f.score > 0)
    needed = bonuses + max(0, min_score) + 1
    return -max(needed, -UNWANTED)


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
    if not wish.sources:
        problems.append(("profiles.problem.no_source", {}))

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

    # The refusals have to beat every bonus put together. Otherwise a camera
    # recording with the right language, the right sound and a well-regarded
    # group on it scores its way over the floor and is grabbed on merit.
    bonuses = sum(f.score for f in blueprint.formats if f.score > 0)
    worst = min((f.score for f in blueprint.formats if f.score < 0), default=0)
    if worst and bonuses + worst >= blueprint.min_score:
        problems.append(("profiles.problem.refusal_too_weak", {
            "refusal": worst, "bonuses": bonuses}))

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
                  allow_remux: bool,
                  sources: tuple[str, ...] = ()) -> tuple[list[dict], int | None]:
    """The ladder, and the rung to stop at.

    Built from the ladder the service itself hands out, with everything the
    person did not ask for switched off. The numbers behind the names are not
    the same in Radarr and Sonarr and have moved between versions, so nothing
    here is hard coded except the names.

    Two things narrow it: the resolutions, and where the picture came from. A
    disc rip and a broadcast capture at 1080p are both "1080p" and are not the
    same thing, so asking for one without the other has to be possible.
    """
    allowed_sources: set[str] = set()
    if sources:
        for source in sources:
            allowed_sources |= {n.lower() for n in SOURCE_NAMES.get(source, ())}

    names: set[str] = set()
    for rung in wanted:
        for name in QUALITY_NAMES.get(rung, ()):
            if not allow_remux and name in REMUX_NAMES:
                continue
            if allowed_sources and name.lower() not in allowed_sources:
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
                                blueprint.wish.allow_remux,
                                blueprint.wish.sources)
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
