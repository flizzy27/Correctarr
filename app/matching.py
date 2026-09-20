"""Match an orphaned file in the download folder to a library title.

The Radarr parser trips over small things and then reports "Unknown Movie".
Cases seen in production:

    Crank.I.2006...                     sequel number written in Roman numerals
    The.Transporter.The.Mission.2005... leading "The" and a hyphen

Both sat there finished for 13 and 14 hours respectively, 20 GB together. For
Transporter, Radarr had already deleted the old file in anticipation of the
upgrade — the movie was left with no file at all.

How it works
------------
For every title several spellings are generated (umlauts both ways, articles
dropped, Roman numerals converted, edition markers such as "Directors Cut"
removed). The same is done for the file name. Then every variant is compared
against every variant using three independent measures:

    ratio       similarity of the character sequence (difflib)
    word_set    intersection over union of the words
    contained   does the smaller word set sit entirely inside the larger one

A match is only accepted when the year fits, at least two of the three measures
are convincing, and the best candidate is clearly ahead of the runner-up. When
in doubt nothing is matched — a wrong match would, in the worst case, overwrite
a good file with a different movie.
"""
from __future__ import annotations

import difflib
import logging
import re
import unicodedata

log = logging.getLogger(__name__)

THRESHOLD = 0.85          # how good the best measure has to be
CERTAIN = 0.97            # at or above this a match counts as beyond doubt
RUNNER_UP_THRESHOLD = 0.72
MARGIN = 0.08             # how far ahead of the runner-up the best has to be
MIN_MEASURES = 2          # how many measures have to be convincing

ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5",
         "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10"}

ARTICLES = ("the", "der", "die", "das", "ein", "eine", "le", "la", "les",
            "el", "il", "a", "an")

# Markers that describe an edition, not the film itself.
EDITION = re.compile(
    r"\b(directors?|extended|unrated|uncut|uncensored|remastered|theatrical|final|"
    r"ultimate|special|collectors?|anniversary|imax|open[ .]?matte|hybrid|"
    r"kinofassung|langfassung|cut|edition|version|fassung|rerip|repack|proper|real)\b")

# Technical markers — from here on the name describes the file, not the film.
TECHNICAL = re.compile(
    r"\b(19[0-9]{2}|20[0-3][0-9]"
    r"|\d{3,4}p|[0-9]{3,4}i"
    r"|bluray|blu[ .]?ray|bdrip|brrip|bdremux|web[ .]?dl|web[ .]?rip|webhd|hdtv|dvdrip|remux|uhd|hddvd"
    r"|x[ .]?26[45]|h[ .]?26[45]|hevc|avc|xvid|divx|av1|vc1"
    r"|dts|ac3|eac3|aac|truehd|atmos|ddp?[0-9]?|flac|opus|mp3"
    r"|german|deutsch|english|multi|dl|dubbed|synchro|ld|md"
    r"|complete|season|staffel|s[0-9]{2}e[0-9]{2})\b")

PART = re.compile(r"\b(teil|part|kapitel|chapter)\s*([0-9ivx]+)\b")

EXTENSION = re.compile(r"\.(mkv|mp4|avi|m4v|ts|mov|wmv)$", re.IGNORECASE)


def _normalise(text: str) -> str:
    """Lower case, punctuation out, technical markers cut off."""
    value = EXTENSION.sub("", text or "")
    value = re.sub(r"[._]+", " ", value)
    value = value.replace("&", " und ")
    value = re.sub(r"[\[\](){}]", " ", value)
    cut = TECHNICAL.search(value.lower())
    if cut and cut.start() > 2:            # do not cut when it starts there
        value = value[:cut.start()]
    value = PART.sub(r"\2", value)         # "Part 2" -> "2"
    value = EDITION.sub(" ", value.lower())
    value = re.sub(r"[^a-z0-9äöüß ]", " ", value.lower())
    return re.sub(r"\s+", " ", value).strip()


def _expand_umlauts(text: str) -> str:
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        text = text.replace(a, b)
    return text


def _strip_diacritics(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text)
                   if unicodedata.category(c) != "Mn").replace("ß", "ss")


def _convert_roman(text: str) -> str:
    return " ".join(ROMAN.get(word, word) for word in text.split())


def _drop_articles(text: str) -> str:
    words = text.split()
    while words and words[0] in ARTICLES:
        words = words[1:]
    return " ".join(words)


def _usable(variant: str, source: str) -> bool:
    """Reject degenerate variants.

    Titles in non-Latin scripts lose almost every character when normalised.
    The Russian alternate title "Хроники Нарнии 1" boils down to exactly "1" —
    and "1" sits entirely inside "crank 1", giving a 100 percent match with a
    completely different film. Such transliteration ruins must not enter the
    comparison at all.
    """
    if len(variant) < 3:
        return False
    if not re.search(r"[a-zäöüß]{3}", variant):
        return False                       # digits or fragments only
    foreign = re.findall(r"[^\x00-\x7fäöüßÄÖÜ]", source or "")
    if len(foreign) >= 3:
        latin = re.findall(r"[a-zA-ZäöüßÄÖÜ]", source or "")
        if len(variant.replace(" ", "")) < max(3, len(latin) * 0.5):
            return False
    return True


def variants(text: str) -> set[str]:
    """Every spelling the same title might show up under."""
    base = _normalise(text)
    if not base:
        return set()
    out = set()
    for step1 in (base, _expand_umlauts(base), _strip_diacritics(base)):
        for step2 in (step1, _convert_roman(step1)):
            for step3 in (step2, _drop_articles(step2)):
                if step3 and _usable(step3, text):
                    out.add(step3)
    return out


def years(text: str) -> list[int]:
    return [int(y) for y in re.findall(r"(?<![0-9])[0-9]{4}(?![0-9])", text or "")
            if 1900 <= int(y) <= 2035]


# -- the three measures ------------------------------------------------------
def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _word_set(a: str, b: str) -> float:
    wa, wb = set(a.split()), set(b.split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def _contained(a: str, b: str) -> float:
    """1.0 when the shorter word set sits entirely inside the longer one.

    With two guards: the shorter set needs at least two words — a single shared
    word proves nothing, "Saw" sits inside "Saw II" but is a different film. And
    the two sets must not differ too much in length, otherwise every short title
    fits inside every long one.
    """
    wa, wb = set(a.split()), set(b.split())
    if not wa or not wb:
        return 0.0
    small, large = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    if len(small) < 2:
        return 0.0
    if len(large) > len(small) * 2:
        return 0.0
    return len(small & large) / len(small)


MEASURES = (("ratio", _ratio), ("word_set", _word_set), ("contained", _contained))


def _compare(file_variants: set[str], title_variants: set[str]) -> dict[str, float]:
    """Best value per measure across all pairs of variants."""
    best = {name: 0.0 for name, _ in MEASURES}
    for a in file_variants:
        for b in title_variants:
            for name, measure in MEASURES:
                value = measure(a, b)
                if value > best[name]:
                    best[name] = value
    return best


def build_candidates(items: list[dict]) -> list[tuple[dict, set[str]]]:
    """All spellings per title, prepared once.

    Building the variants is the expensive part, and it does not depend on the
    file being matched — so it happens once per run, not once per file.
    """
    out = []
    for item in items:
        names = {item.get("title"), item.get("originalTitle"), item.get("sortTitle")}
        names |= {alt.get("title") for alt in (item.get("alternateTitles") or [])}
        spellings: set[str] = set()
        for name in names:
            if name:
                spellings |= variants(name)
        if spellings:
            out.append((item, spellings))
    return out


def match(file_name: str, candidates: list[tuple[dict, set[str]]],
          year_tolerance: int = 1) -> tuple[dict | None, float, str]:
    """Returns (item, confidence, reason). The item is None when unsure."""
    file_variants = variants(file_name)
    if not file_variants:
        return None, 0.0, "no usable name"
    file_years = years(file_name)

    scored: list[tuple[float, int, dict, dict]] = []
    for item, title_variants in candidates:
        item_year = item.get("year")
        if file_years and item_year and not any(
                abs(y - item_year) <= year_tolerance for y in file_years):
            continue                       # year does not fit: do not even compare
        measures = _compare(file_variants, title_variants)
        convincing = sum(1 for v in measures.values() if v >= RUNNER_UP_THRESHOLD)
        scored.append((max(measures.values()), convincing, item, measures))

    if not scored:
        return None, 0.0, ("no title with a matching year" if file_years
                           else "no year in the name and no match")

    scored.sort(key=lambda row: (-row[0], -row[1]))
    best, convincing, item, measures = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    detail = ", ".join(f"{k} {v:.0%}" for k, v in measures.items())

    if best < THRESHOLD:
        return None, best, f"best match only {best:.0%} ({item.get('title')}; {detail})"
    if convincing < MIN_MEASURES:
        return None, best, (f"only one measure is convincing for {item.get('title')} "
                            f"({detail}) — too thin to act on")
    if not file_years:
        return None, best, f"no year in the name, too uncertain ({item.get('title')})"
    # A practically perfect match wins even against a close runner-up: "Saw II"
    # matches "Saw II" at 100 percent while "Saw III" reaches 92 — the margin is
    # small but the match is beyond doubt. Without this exception the clearest
    # match of all would be rejected.
    beyond_doubt = best >= CERTAIN and convincing >= MIN_MEASURES
    if not beyond_doubt and best - runner_up < MARGIN and runner_up >= THRESHOLD:
        return None, best, (f"not unambiguous: {item.get('title')} {best:.0%} against "
                            f"{scored[1][2].get('title')} {runner_up:.0%}")
    return item, best, f"{item.get('title')} ({item.get('year')}) — {detail}"
