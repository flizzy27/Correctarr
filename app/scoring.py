"""Re-score a release against the custom formats of a quality profile.

Why this exists: a release is scored exactly once — when it is grabbed. Change
a profile afterwards and downloads already in flight are never re-examined;
they run to completion and then sit there. This module reproduces the check so
waiting entries can be held against the rules that apply *today*.

The combining logic per custom format:

  * every condition with ``required=true`` must match
  * of the remaining ones at least one must match (if there are any)
  * ``negate`` inverts the result of the individual condition
"""
from __future__ import annotations

import logging
from typing import Any

# Deliberately the ``regex`` module rather than the built-in ``re``: Radarr
# runs on .NET, and the patterns from the profile databases use variable width
# lookbehind throughout, for example (?<=^|[\s.-])YIFY\b. The built-in ``re``
# refuses those — measured, 523 of 814 patterns failed (64 percent), so the
# scoring was silently incomplete. ``regex`` handles them the way .NET does:
# 814 of 814.
import regex as re

log = logging.getLogger(__name__)

# Specifications that can be checked from queue data alone. Everything else is
# deliberately skipped so nothing is ever wrongly blocked.
CHECKABLE = frozenset({
    "ReleaseTitleSpecification", "SizeSpecification", "ResolutionSpecification",
    "ReleaseGroupSpecification", "YearSpecification", "SourceSpecification",
    "QualityModifierSpecification", "EditionSpecification",
})

# Deliberately NOT checked: LanguageSpecification.
#
# Before the import, the language is taken from the file name. The German
# marker ".DL." (German plus original audio) is not something the parser knows,
# so those entries often report only ["English"] even though the file does have
# a German track. Only after the import are the real audio tracks read.
#
# Checking it here would throw away good releases in bulk. Measured: 19 false
# positives in a single run. Proof: "Zodiac.DC.2007.1080p.BluRay.AC3.DL.x264-HDC"
# reports English in the queue; the same group reports German+English after the
# import.

_RESOLUTIONS = {360: "360", 480: "480", 540: "540", 576: "576",
                720: "720", 1080: "1080", 2160: "2160"}

_GROUP_PATTERN = re.compile(r"-([A-Za-z0-9_.]+)$")

_EXTENSION = re.compile(r"\.(mkv|mp4|avi|m4v|ts|mov|wmv|mpg|mpeg)$", re.IGNORECASE)

#: Source words that are the end of a name without being a group:
#: ``Movie.2020.1080p.WEB-DL`` has no group, not a group called "DL".
_NOT_A_GROUP = re.compile(r"(?i)web-(dl|rip)$")

#: A score at or below this is a refusal whatever else the profile says.
#: Published profiles write -999999 for "never".
BLOCKED = -900000


def _field(spec: dict, name: str) -> Any:
    for f in spec.get("fields", []):
        if f.get("name") == name:
            return f.get("value")
    return None


def _option_name(spec: dict, name: str = "value") -> str | None:
    """The name of the chosen entry of a select field, lower case.

    Source and quality modifier are stored as numbers, and the numbers are the
    service's own: 7 is WEB-DL in Radarr and a Blu-ray remux in Sonarr. A
    release reports the same thing as a word ("webdl", "blurayRaw"). Compared
    as they were, no source condition ever held — and a negated one held for
    everything. The field carries its own list of what each number means, so
    that list is what is read; without it nothing is guessed.
    """
    for entry in spec.get("fields", []):
        if entry.get("name") != name:
            continue
        value = entry.get("value")
        if isinstance(value, str) and not value.isdigit():
            return value.lower()
        for option in entry.get("selectOptions") or []:
            if str(option.get("value")) == str(value):
                return str(option.get("name") or "").lower() or None
        return None
    return None


def _matches(pattern: str, text: str) -> bool:
    try:
        return re.search(pattern, text, re.IGNORECASE) is not None
    except re.error as e:
        # A broken pattern must not abort the whole run.
        log.warning("Skipping invalid pattern (%s): %s", e, pattern[:60])
        return False


def _condition(spec: dict, release: dict) -> bool | None:
    """True, False, or None when it cannot be decided."""
    kind = spec.get("implementation")
    title = release.get("title") or ""

    if kind in ("ReleaseTitleSpecification", "EditionSpecification"):
        pattern = _field(spec, "value")
        return _matches(str(pattern), title) if pattern else None

    if kind == "ReleaseGroupSpecification":
        pattern = _field(spec, "value")
        if not pattern:
            return None
        # The service matches the group and nothing else; a release without
        # one matches no group. Matched against the whole name instead, a
        # film called *Ghost* belonged to the group of that name.
        group = release.get("group") or ""
        return _matches(str(pattern), group) if group else False

    if kind == "SizeSpecification":
        gb = release.get("gb")
        low, high = _field(spec, "min"), _field(spec, "max")
        if gb is None or low is None or high is None:
            return None
        # Radarr: greater than min, less than or equal to max.
        return float(low) < gb <= float(high)

    if kind == "ResolutionSpecification":
        wanted, actual = _field(spec, "value"), release.get("resolution")
        if wanted is None or actual is None:
            return None
        return str(actual) == str(wanted)

    if kind == "YearSpecification":
        year = release.get("year")
        low, high = _field(spec, "min"), _field(spec, "max")
        if year is None or low is None or high is None:
            return None
        return int(low) <= int(year) <= int(high)

    if kind == "SourceSpecification":
        wanted, actual = _option_name(spec), release.get("source")
        if wanted is None or actual is None:
            return None
        return wanted == str(actual).lower()

    if kind == "QualityModifierSpecification":
        wanted, actual = _option_name(spec), release.get("modifier")
        if wanted is None or actual is None:
            return None
        return wanted == str(actual).lower()

    return None


def format_matches(custom_format: dict, release: dict) -> bool:
    """Apply the combining logic of one custom format."""
    specs = custom_format.get("specifications") or []
    if not specs:
        return False
    # An unheckable specification makes the whole format uncheckable — better
    # not to score at all than to score wrongly.
    if any(s.get("implementation") not in CHECKABLE for s in specs):
        return False

    required, optional = [], []
    for spec in specs:
        result = _condition(spec, release)
        if result is None:
            return False
        satisfied = (not result) if spec.get("negate") else result
        (required if spec.get("required") else optional).append(satisfied)

    if required and not all(required):
        return False
    if optional and not any(optional):
        return False
    return bool(required or optional)


def release_from_entry(entry: dict) -> dict:
    """Reduce a queue entry to the fields the conditions look at."""
    quality = (entry.get("quality") or {}).get("quality") or {}
    item = entry.get("movie") or entry.get("series") or {}
    size = entry.get("size") or 0
    title = entry.get("title") or ""
    bare = _EXTENSION.sub("", title)
    group_match = None if _NOT_A_GROUP.search(bare) else _GROUP_PATTERN.search(bare)
    return {
        "title": title,
        "group": group_match.group(1) if group_match else "",
        "gb": (size / 1024 ** 3) if size else None,
        "resolution": _RESOLUTIONS.get(quality.get("resolution")) if quality.get("resolution") else None,
        "source": quality.get("source"),
        "modifier": quality.get("modifier"),
        "year": item.get("year"),
    }


def matched(entry: dict, profile: dict,
            formats_by_name: dict[str, dict]) -> list[tuple[str, int]]:
    """``(name, score)`` of every format with a score that the entry meets."""
    release = release_from_entry(entry)
    out: list[tuple[str, int]] = []
    for item in profile.get("formatItems", []):
        weight = item.get("score") or 0
        if weight == 0:
            continue                       # no effect, no need to check
        custom_format = formats_by_name.get(item.get("name"))
        if not custom_format:
            continue
        if format_matches(custom_format, release):
            out.append((str(item["name"]), int(weight)))
    return out


def score(entry: dict, profile: dict,
          formats_by_name: dict[str, dict]) -> tuple[int, list[str]]:
    """Returns (total score, the formats that matched) under today's rules."""
    hits = matched(entry, profile, formats_by_name)
    return (sum(weight for _name, weight in hits),
            [f"{name} ({weight:+})" for name, weight in hits])


def refused(profile: dict) -> set[str]:
    """The formats that are a "no" in this profile, by name.

    A format is a refusal when a release carrying it cannot reach the
    profile's floor however many bonuses it collects besides — which is what
    the service itself makes of it. Published profiles say so with -999999;
    a profile built by this program says so with a few thousand, worked out
    to beat every bonus it hands out. Read only as the first, the refusals of
    a profile built here were never seen at all.
    """
    items = profile.get("formatItems") or []
    bonuses = sum(max(0, int(item.get("score") or 0)) for item in items)
    floor = int(profile.get("minFormatScore") or 0)
    out = set()
    for item in items:
        weight = int(item.get("score") or 0)
        if weight < 0 and (weight <= BLOCKED or weight + bonuses < floor):
            out.add(str(item.get("name")))
    return out


# ---------------------------------------------------------------------------
# Why this module exists at all
# ---------------------------------------------------------------------------
# Radarr does not apply ReleaseTitleSpecification to the full release name. It
# applies it to what is left after the recognised movie title has been split
# off. A marker placed BEFORE the year is swallowed by the parser.
#
# Verified through /api/v3/parse:
#   "Black.Panther.3D.HOU.2018.German.DTS.DL.1080p.BluRay.x264-LeetHD"
#       -> parsed title:      "Black Panther 3D HOU"
#       -> formats detected:  ['1080p Bluray', 'DTS']      (3D NOT detected)
#   "Meg.2018.HSBS.German.Dubbed.AC3.DL.1080p.BluRay.x264-miHD"
#       -> parsed title:      "Meg"
#       -> formats detected:  [... '3D Formats' ...]       (3D detected)
#
# The published 3D patterns work around this by requiring the marker to appear
# after the year: (?<=\b[12]\d{3}\b).*\b(...3d|sbs...)\b
#
# So Radarr structurally cannot reject such a release. This module checks the
# RAW release name instead and catches what Radarr misses.
