"""Deciding whether a release is one film or a boxful of them.

The case this exists for, in full:

    Transformers.2007-2018.COMPLETE.UHD.BluRay.2160p.TrueHD.Atmos.7.1.HEVC-GRP

Asked for: *Transformers* (2007). What is being fetched: every Transformers
film ever made, sixty-eight gigabytes of them, of which one is wanted.

Nothing catches this on its own. The year check does not, and cannot: 2007 is
in the name and 2007 is the right year, so as far as it is concerned the
release agrees with the film. The title check does not either — the name starts
with the title, because it *is* the title, five times over. The service will
download the lot and then fail to import it, or import the wrong one, and
either way the disc is full and the film is still missing.

What is looked for
------------------
Four signals, each of which is close to conclusive on its own, and each of
which is measured **against the item's own title** so that a film genuinely
called *The Complete History of the Soviet Union* is not accused of being a box
set:

1. **A span of years.** ``2007-2018``. One film does not span eleven years.
   This is the strongest signal there is, and it is the one in the case above.
2. **A word that means "more than one".** Collection, trilogy, quadrilogy,
   anthology, saga, box set, Gesamtedition, Filmreihe.
3. **A numbered range.** ``1-5``, ``Teil 1-3``, ``I-V``.
4. **Several separate years.** ``2007.2009.2011`` is three films in a trench
   coat.

One signal is enough to say so. Two or more and there is nothing left to
discuss, so the confidence rises with each — which matters, because the
finding's action is to throw the download away and go and look for another,
and that should only happen when the answer is not in doubt.

What is deliberately NOT looked at
----------------------------------
**Size.** It is the obvious idea and it does not work: a single 2160p remux is
sixty to eighty gigabytes on its own, which is the same range as a 1080p box
set of five. A threshold that caught the box would throw away every good 4K
release, and one that spared the 4K release would never fire. The size is
reported, because "cancelling a 68 GB download" is the useful half of the
sentence, but it is not evidence and is not treated as any.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

#: The window a four digit number has to fall in to be a year at all. Same
#: reasoning as in :mod:`app.years`: it is here to rule out what is plainly not
#: a year, not to be clever.
FIRST, LAST = 1900, 2100

#: ``2007-2018``, ``2007 - 2018``, ``2007–2018``. The separator matters: a full
#: stop is not one, because ``2007.2160p`` is a year followed by a resolution
#: and not a span of anything.
_SPAN = re.compile(r"(?<![0-9])((?:19|20)[0-9]{2})\s*[-–—]\s*"
                   r"((?:19|20)[0-9]{2})(?![0-9])")

_FOUR_DIGITS = re.compile(r"(?<![0-9])((?:19|20)[0-9]{2})(?![0-9])")

#: A resolution written out in full contains something in the year window.
_RESOLUTION = re.compile(r"\b\d{3,4}\s*[xX×]\s*\d{3,4}\b")

#: Words that mean "this is more than one film". Split into the ones that are
#: conclusive and the ones that are merely suggestive, because "complete" turns
#: up on plenty of single releases — a complete disc, a complete rip — while
#: nobody writes "quadrilogy" by accident.
_CONCLUSIVE = re.compile(
    r"(?<![a-z0-9])("
    r"collection|kollektion|anthology|anthologie|trilogy|trilogie"
    r"|duology|dilogie|quadrilogy|quadrilogie|tetralogy|tetralogie"
    r"|pentalogy|pentalogie|hexalogy|hexalogie"
    r"|saga|box[. _-]?set|boxset|gesamtedition|gesamtbox|filmreihe"
    r"|movie[. _-]?pack|film[. _-]?pack|all[. _-]?movies|alle[. _-]?filme"
    # Not "complete edition": that is one film with everything on the disc,
    # and reading it as a box set threw the film away.
    r"|complete[. _-]?(collection|series|saga|set|pack|movies)"
    r")(?![a-z0-9])", re.IGNORECASE)

_SUGGESTIVE = re.compile(
    r"(?<![a-z0-9])(complete|komplett|vollstaendig|vollständig)"
    r"(?![a-z0-9])", re.IGNORECASE)

#: ``1-5``, ``Teil 1-3``, ``Parts 1-4``, ``I-V``. A bare ``1-5`` is only read
#: this way when something in front of it says it is about parts, because
#: ``5.1`` and ``7-1`` turn up in names for entirely different reasons.
_NUMBERED = re.compile(
    r"(?<![a-z0-9])(teile?|parts?|filme?|movies?|kapitel|chapters?|vol(ume)?s?)"
    r"[. _-]*([0-9]{1,2}|[ivx]{1,4})\s*[-–—]\s*([0-9]{1,2}|[ivx]{1,4})"
    r"(?![a-z0-9])", re.IGNORECASE)

#: ``I-V`` standing on its own, which only ever means a range of films.
_ROMAN_RANGE = re.compile(
    r"(?<![a-z0-9])(i{1,3}|iv|v|vi{1,3}|ix|x)\s*[-–—]\s*"
    r"(i{1,3}|iv|v|vi{1,3}|ix|x)(?![a-z0-9])", re.IGNORECASE)

#: How sure each signal is on its own.
WEIGHTS = {
    "span": 0.90,
    "collection_word": 0.85,
    "several_years": 0.85,
    "numbered_range": 0.70,
    "complete": 0.55,
}

#: How much each signal beyond the first adds. Two independent ways of saying
#: the same thing is not twice as much evidence, but it is more.
TOGETHER = 0.06

#: At or above this, it is a box set.
SURE = 0.60


@dataclass(frozen=True)
class Verdict:
    """What to make of one release name."""
    is_pack: bool
    confidence: float = 0.0
    #: Translation keys, one per signal, in the order they were found.
    reasons: tuple[str, ...] = ()
    #: The span of years, when there was one, for the message.
    span: str = ""
    #: How many separate years the name mentions that the title does not.
    years: int = 0


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[^0-9a-zäöüß]+",
                                (text or "").lower()) if w]


def _spare_years(release: str, title: str) -> list[int]:
    """The years in the name that the title does not account for.

    The same allowance as in :mod:`app.years`, for the same reason: *Blade
    Runner 2049* carries a year in its title and is not a box set for it.
    """
    cleaned = _RESOLUTION.sub(" ", release or "")
    found = [int(y) for y in _FOUR_DIGITS.findall(cleaned)
             if FIRST <= int(y) <= LAST]
    budget: dict[int, int] = {}
    for word in _words(title):
        if len(word) == 4 and word.isdigit() and FIRST <= int(word) <= LAST:
            budget[int(word)] = budget.get(int(word), 0) + 1
    kept = []
    for year in found:
        if budget.get(year):
            budget[year] -= 1
            continue
        kept.append(year)
    return kept


#: Anything between two words of a title in a release name: a full stop, a
#: space, a dash, a colon that became nothing, " - ".
_SEP = r"[^0-9a-zäöüß]+"

#: Each German letter, and what a release name makes of it.
_GERMAN = {"ä": "(?:ä|ae|a)", "ö": "(?:ö|oe|o)", "ü": "(?:ü|ue|u)",
           "ß": "(?:ß|ss)"}

#: An article a release name keeps or drops as it pleases.
_ARTICLES = ("the", "der", "die", "das", "a", "an", "le", "la", "les", "el",
             "il")

_LETTERS = str.maketrans({"ø": "o", "æ": "ae", "œ": "oe", "ł": "l"})


def _plain(text: str) -> str:
    """Lower case, apostrophes out, accents off everything but ä, ö, ü, ß."""
    text = re.sub(r"['’`´]", "", (text or "").lower().translate(_LETTERS))
    out = []
    for char in text:
        if char in "äöüß":
            out.append(char)
            continue
        out.extend(c for c in unicodedata.normalize("NFD", char)
                   if unicodedata.category(c) != "Mn")
    return "".join(out)


def _title_pattern(title: str) -> str | None:
    """A pattern for a title however a release name spells it.

    Taken literally, the title was only found when the release wrote it the
    way the service does. *The Twilight Saga: New Moon* is
    ``The.Twilight.Saga.New.Moon`` in a release — no colon — and every release
    of it was a box set for the word "Saga", which by default is blocklisted
    and searched for again.
    """
    words = re.findall(r"[0-9a-zäöüß]+", _plain(title).replace("&", " and "))
    if not words:
        return None
    parts = []
    for index, word in enumerate(words):
        if word in ("and", "und"):
            # "&", "and", "und", or left out altogether.
            parts.append(rf"(?:(?:and|und|&){_SEP})?")
            continue
        body = "".join(_GERMAN.get(c, re.escape(c)) for c in word)
        if index == 0 and word in _ARTICLES and len(words) > 1:
            parts.append(rf"(?:{body}{_SEP})?")
            continue
        parts.append(body if index == len(words) - 1 else body + _SEP)
    articles = "|".join(_ARTICLES)
    return (rf"^[^0-9a-zäöüß]*(?:(?:{articles}){_SEP})?"
            + "".join(parts) + r"(?![0-9a-zäöüß])")


def _titles(item: dict) -> list[str]:
    """Every name the item goes by: its title, the original, the others.

    A German library files *The Twilight Saga: New Moon* as *New Moon – Biss
    zur Mittagsstunde*, and the release still carries the original.
    """
    names = [item.get("title"), item.get("originalTitle")]
    names += [a.get("title") for a in (item.get("alternateTitles") or [])
              if isinstance(a, dict)]
    return [str(n) for n in names if n]


def _without_title(release: str, item_or_title) -> str:
    """The release name with the item's own title taken out of it.

    So that a film called *The Collection* is not accused by its own name. The
    title is removed once, from the front, where it always sits. Every name
    the item goes by is tried, and the one that takes the most away wins.
    """
    names = ([item_or_title] if isinstance(item_or_title, str)
             else _titles(item_or_title or {}))
    plain = _plain(release)
    best = release or ""
    for name in names:
        pattern = _title_pattern(name)
        if not pattern:
            continue
        found = re.match(pattern, plain, flags=re.IGNORECASE)
        if found and len(plain) - found.end() < len(best):
            best = " " + plain[found.end():]
    return best


def judge(release: str, item: dict | None = None) -> Verdict:
    """Is this release a box set rather than the one film that was asked for?"""
    release = release or ""
    item = item or {}
    rest = _without_title(release, item)

    found: list[tuple[str, float]] = []
    span_text = ""

    match = _SPAN.search(rest)
    if match:
        start, end = int(match.group(1)), int(match.group(2))
        # A span has to go forwards and be longer than a single year. "2007-07"
        # is a date and "2018-2007" is somebody being careless with a hyphen.
        if FIRST <= start <= LAST and start < end <= LAST:
            span_text = f"{start}–{end}"
            found.append(("pack.reason.span", WEIGHTS["span"]))

    spare = _spare_years(rest, "")
    # Three separate years and no span is the same claim made differently.
    if not span_text and len(set(spare)) >= 3:
        found.append(("pack.reason.several_years", WEIGHTS["several_years"]))

    if _CONCLUSIVE.search(rest):
        found.append(("pack.reason.collection_word", WEIGHTS["collection_word"]))
    elif _SUGGESTIVE.search(rest):
        found.append(("pack.reason.complete", WEIGHTS["complete"]))

    if _NUMBERED.search(rest) or _ROMAN_RANGE.search(rest):
        found.append(("pack.reason.numbered_range", WEIGHTS["numbered_range"]))

    if not found:
        return Verdict(is_pack=False)

    best = max(weight for _key, weight in found)
    confidence = round(min(0.99, best + TOGETHER * (len(found) - 1)), 3)
    return Verdict(
        is_pack=confidence >= SURE, confidence=confidence,
        reasons=tuple(key for key, _weight in found),
        span=span_text, years=len(set(spare)))
