"""Deciding whether the year in a release name contradicts the title.

This looks like a one-line comparison and is not. The naive version — pull the
four digits out of the name, compare them to the year the service holds — is
wrong in both directions, and both are expensive:

**It rejects good releases.** The year a metadata service shows is computed
from release dates, and there are several defensible answers. A film that
premiered at a festival in one year and reached cinemas in the next is filed
under the later year by one database and the earlier one by another, and
release groups follow the earlier. A film that opened in a handful of cinemas
on the 25th of December and went wide in January is filed under December. A
Japanese film released at home a year or two before it reached the West carries
its home year. None of these is an error, and rejecting them throws away
perfectly good releases that then get grabbed again, rejected again, and so on.

**It accepts bad ones, and rejects good ones, over titles that contain a
number.** ``1917``, ``2012``, ``Blade Runner 2049``, ``2001: A Space Odyssey``
— read the first four digits and every one of those is judged against its own
title. ``1917.German.DL.1080p`` has no release year in it at all, but the naive
reading finds 1917 and compares it to 2019.

So two things are built here. :func:`candidates` works out which numbers in a
name could actually be a release year, discarding the ones that belong to the
title or to a resolution. :func:`accepted` works out every year that
legitimately belongs to an item, using the dates the service already holds
rather than a blind window. A release is only ever questioned when it has a
year, and none of its candidate years is one of the accepted ones.

The bias throughout is towards saying nothing. A year that cannot be read, a
title that cannot be compared, an item with no year at all — each of those ends
in no finding rather than a guess.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

#: Cinema is older than most people assume and release names occasionally run
#: ahead of the calendar, so the window is wide. It exists to rule out things
#: that are plainly not years, not to be clever.
FIRST, LAST = 1870, 2100

#: A standalone run of exactly four digits. The lookarounds matter: without
#: them ``12345`` yields 1234, and ``x264`` yields nothing only by luck.
_FOUR_DIGITS = re.compile(r"(?<![\d])(\d{4})(?![\d])")

#: Resolutions written out in full. ``1920x1080`` contains a number in the year
#: window, and reading it as one is a false accusation on every release that
#: spells its resolution that way.
_RESOLUTION = re.compile(r"\b\d{3,4}\s*[xX×]\s*\d{3,4}\b")

#: Anything that is not a letter or a digit separates words in a release name.
_SEPARATORS = re.compile(r"[^0-9a-z]+")


@dataclass(frozen=True)
class Verdict:
    """What to make of one release name's year."""
    #: True when the name carries a year and none of them fits the item.
    wrong: bool
    #: The years that could be the release year, after discarding the rest.
    found: tuple[int, ...] = ()
    #: The year the item is filed under, for the message.
    expected: int | None = None
    #: How far the closest candidate is from anything acceptable.
    distance: int = 0
    #: 0 to 1. Two years apart is a judgement call; twenty is not.
    confidence: float = 0.0


def _words(text: str) -> list[str]:
    return [w for w in _SEPARATORS.split((text or "").lower()) if w]


def candidates(release: str, title: str = "") -> list[int]:
    """The years in a release name that could be the year it was released.

    Numbers belonging to the title are removed — but only as often as the title
    contains them. ``Blade.Runner.2049.2017.UHD`` keeps 2017, because the title
    accounts for one 2049 and nothing else. ``2012.2009.BluRay`` keeps 2009 for
    the same reason. And ``1917.German.DL.1080p`` ends up with nothing at all,
    which is the right answer: that name states no release year, so there is
    nothing to disagree with.
    """
    cleaned = _RESOLUTION.sub(" ", release or "")
    found = [int(match) for match in _FOUR_DIGITS.findall(cleaned)]
    found = [year for year in found if FIRST <= year <= LAST]
    if not found:
        return []

    # Count how many of each year the title itself accounts for, and drop that
    # many from the front. Dropping every occurrence would lose the real year
    # of a re-release named after the year in its own title.
    budget: dict[int, int] = {}
    for word in _words(title):
        if len(word) == 4 and word.isdigit():
            year = int(word)
            if FIRST <= year <= LAST:
                budget[year] = budget.get(year, 0) + 1

    kept = []
    for year in found:
        if budget.get(year):
            budget[year] -= 1
            continue
        kept.append(year)
    return kept


def _year_of(value) -> int | None:
    """The year out of whatever shape a date arrived in."""
    if not value:
        return None
    if isinstance(value, int):
        return value if FIRST <= value <= LAST else None
    match = _FOUR_DIGITS.search(str(value)[:10])
    if not match:
        return None
    year = int(match.group(1))
    return year if FIRST <= year <= LAST else None


#: Date fields the services carry on a library item. Every one of them is a
#: real, defensible release year for that item, which makes this list strictly
#: better than guessing a window: it is the service's own data.
DATE_FIELDS = ("inCinemas", "physicalRelease", "digitalRelease", "releaseDate",
               "firstAired", "premiereDate")


def accepted(item: dict, tolerance: int = 1) -> set[int]:
    """Every year that legitimately belongs to this item.

    Three sources, in order of how much they are worth:

    1. ``year`` and ``secondaryYear``. The second is the service's own
       allowance for exactly this problem — it holds the premiere year when it
       differs from the filed year, and matching against it is what the service
       does itself.
    2. The release dates it holds. A film with a December cinema date and a
       March disc date legitimately appears under either year.
    3. A window around the filed year, as a last resort for everything the
       first two miss — a home release a year ahead of the Western one, mostly.

    Tolerance can be set to 0 to switch the window off and rely only on the
    dates the service actually holds.
    """
    years: set[int] = set()
    for key in ("year", "secondaryYear"):
        year = _year_of(item.get(key))
        if year:
            years.add(year)
    for key in DATE_FIELDS:
        year = _year_of(item.get(key))
        if year:
            years.add(year)

    if tolerance > 0:
        filed = _year_of(item.get("year"))
        if filed:
            years.update(range(filed - tolerance, filed + tolerance + 1))

    years |= _broadcast_span(item)
    return years


def _looks_like_a_series(item: dict) -> bool:
    """Is this a thing that runs, rather than a thing that came out once?"""
    return (item.get("seasons") is not None
            or bool(item.get("seriesType"))
            or bool(item.get("firstAired")))


def _broadcast_span(item: dict) -> set[int]:
    """Every year a series was on the air.

    A series is not *from* a year the way a film is — it runs. An episode of a
    show that started in 2015 and is still going carries this year's date, and
    daily programmes are named by date outright:
    ``Show.Name.2024.03.04.1080p.WEB``. Held against the year the series is
    filed under, every one of those reads as a different programme — and the
    rule that compares them blocklists and searches again by default, so a
    show running longer than a year had its episodes thrown away as fast as
    they arrived.

    The span is left open at the top unless the service says the series has
    ended *and* says when it last aired. Narrowing it on a guess is how the
    problem started.
    """
    if not _looks_like_a_series(item):
        return set()
    start = _year_of(item.get("firstAired")) or _year_of(item.get("year"))
    if not start:
        return set()
    end = datetime.now(UTC).year
    if item.get("ended"):
        known = [y for y in (_year_of(item.get("lastAired")),
                             _year_of(item.get("previousAiring"))) if y]
        if known:
            end = max(known)
    return set(range(start, max(start, end) + 1))


def _confidence(distance: int, tolerance: int) -> float:
    """How sure the mismatch is, from 0 to 1.

    Deliberately slow to rise. A release two years out is the ordinary festival
    or international case and deserves doubt; one twenty years out is a
    different film. The scale reaches certainty at ten years past the window,
    so an automatic action guarded by a confidence condition only fires on the
    obvious ones unless it is told otherwise.
    """
    over = distance - tolerance
    if over <= 0:
        return 0.0
    return round(min(1.0, over / 10), 3)


def judge(release: str, item: dict, tolerance: int = 1) -> Verdict:
    """Does this release name's year contradict the item it was matched to?

    Returns a verdict with ``wrong`` false whenever there is nothing to say —
    no year in the name, no year on the item, or a year that fits. Callers do
    not need to check the inputs themselves.
    """
    filed = _year_of(item.get("year"))
    if not filed:
        return Verdict(wrong=False)

    title = item.get("title") or ""
    found = candidates(release, title)
    if not found:
        return Verdict(wrong=False, expected=filed)

    fits = accepted(item, tolerance)
    if any(year in fits for year in found):
        return Verdict(wrong=False, found=tuple(found), expected=filed)

    # The closest candidate to anything acceptable, so the message talks about
    # the near miss rather than whichever number happened to come first.
    distance = min(abs(year - other) for year in found for other in fits)
    return Verdict(wrong=True, found=tuple(found), expected=filed,
                   distance=distance,
                   confidence=_confidence(distance, tolerance))
