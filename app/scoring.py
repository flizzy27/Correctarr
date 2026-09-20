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


def _field(spec: dict, name: str) -> Any:
    for f in spec.get("fields", []):
        if f.get("name") == name:
            return f.get("value")
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
        group = release.get("group") or title
        return _matches(str(pattern), group) if pattern else None

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
        wanted, actual = _field(spec, "value"), release.get("source")
        if wanted is None or actual is None:
            return None
        return str(wanted) == str(actual)

    if kind == "QualityModifierSpecification":
        wanted, actual = _field(spec, "value"), release.get("modifier")
        if wanted is None or actual is None:
            return None
        return str(wanted) == str(actual)

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
    group_match = _GROUP_PATTERN.search(title)
    return {
        "title": title,
        "group": group_match.group(1) if group_match else "",
        "gb": (size / 1024 ** 3) if size else None,
        "resolution": _RESOLUTIONS.get(quality.get("resolution")) if quality.get("resolution") else None,
        "source": quality.get("source"),
        "modifier": quality.get("modifier"),
        "year": item.get("year"),
    }


def score(entry: dict, profile: dict,
          formats_by_name: dict[str, dict]) -> tuple[int, list[str]]:
    """Returns (total score, the formats that matched) under today's rules."""
    release = release_from_entry(entry)
    total = 0
    hits: list[str] = []
    for item in profile.get("formatItems", []):
        weight = item.get("score") or 0
        if weight == 0:
            continue                       # no effect, no need to check
        custom_format = formats_by_name.get(item.get("name"))
        if not custom_format:
            continue
        if format_matches(custom_format, release):
            total += weight
            hits.append(f"{item['name']} ({weight:+})")
    return total, hits


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
