"""Telling one film from a boxful of them.

The case this was written for:

    Transformers.2007-2018.COMPLETE.UHD.BluRay.2160p.TrueHD.Atmos.7.1.HEVC-GRP

asked for as *Transformers* (2007). Sixty-eight gigabytes, of which one film is
wanted, and neither the year check nor the title check can see anything wrong:
2007 is in the name and 2007 is the right year, and the name does start with
the title — it simply carries on and names four more.

The false positives matter more than the true ones here. The rule's action is
to throw a download away and go looking for another, so a film with a year in
its own title, or one actually called *The Collection*, has to come through
untouched. Half of what follows is exactly that.
"""
from __future__ import annotations

import pytest

from app import packs


def movie(title: str, year: int | None = None) -> dict:
    return {"title": title, "year": year}


# ---------------------------------------------------------------------------
# What is a box set
# ---------------------------------------------------------------------------
BOXES = [
    ("Transformers.2007-2018.COMPLETE.UHD.BluRay.2160p.TrueHD.Atmos.7.1.HEVC-GRP",
     "Transformers"),
    ("The.Lord.of.the.Rings.Trilogy.2001-2003.1080p.BluRay.x264-GRP",
     "The Lord of the Rings: The Fellowship of the Ring"),
    ("Alien.Anthology.1979.1986.1992.1997.1080p.BluRay.x264-GRP", "Alien"),
    ("Harry.Potter.Complete.Collection.2001-2011.2160p.UHD-GRP",
     "Harry Potter and the Philosopher's Stone"),
    ("Rocky.I-V.1976-1990.1080p.BluRay.x264-GRP", "Rocky"),
    ("Star.Wars.Teil.1-6.German.DL.1080p.BluRay.x264-GRP", "Star Wars"),
    ("Indiana.Jones.Quadrilogy.1981-2008.1080p-GRP", "Raiders of the Lost Ark"),
    ("Die.Hard.Collection.1988-2013.German.DL.1080p.BluRay-GRP", "Die Hard"),
    ("Mad.Max.Filmreihe.1979-2015.1080p.BluRay-GRP", "Mad Max"),
    ("Matrix.Gesamtedition.1999-2003.German.DL.2160p-GRP", "The Matrix"),
]


@pytest.mark.parametrize("name,title", BOXES)
def test_a_box_set_is_recognised(name, title):
    verdict = packs.judge(name, movie(title))
    assert verdict.is_pack, f"{name} was not seen as a box set"
    assert verdict.confidence >= packs.SURE


@pytest.mark.parametrize("name,title", BOXES)
def test_a_box_set_says_why(name, title):
    assert packs.judge(name, movie(title)).reasons


def test_a_span_of_years_is_the_strongest_signal():
    """One film does not span eleven years."""
    verdict = packs.judge("Transformers.2007-2018.2160p.BluRay-GRP",
                          movie("Transformers", 2007))
    assert verdict.is_pack
    assert "pack.reason.span" in verdict.reasons
    assert verdict.span == "2007–2018"


def test_two_signals_are_surer_than_one():
    one = packs.judge("Rocky.1976-1990.1080p-GRP", movie("Rocky"))
    two = packs.judge("Rocky.I-V.1976-1990.1080p-GRP", movie("Rocky"))
    assert two.confidence > one.confidence


def test_the_confidence_never_reaches_certainty():
    """Nothing read off a file name is ever beyond doubt."""
    worst = packs.judge(
        "X.Collection.Trilogy.Teil.1-5.2001-2011.1080p-GRP", movie("X"))
    assert worst.confidence < 1.0


# ---------------------------------------------------------------------------
# What is not — the half that matters
# ---------------------------------------------------------------------------
SINGLES = [
    # The same film, on its own. This is what should be grabbed instead.
    ("Transformers.2007.2160p.UHD.BluRay.x265-GRP", "Transformers"),
    # A title with a year in it. The year is the title's, not a span.
    ("Blade.Runner.2049.2017.2160p.UHD.BluRay.x265-GRP", "Blade Runner 2049"),
    ("1917.2019.1080p.BluRay.x264-GRP", "1917"),
    ("2012.2009.1080p.BluRay.x264-GRP", "2012"),
    ("2001.A.Space.Odyssey.1968.2160p.UHD.BluRay-GRP", "2001: A Space Odyssey"),
    # A film actually called that.
    ("The.Collection.2012.1080p.BluRay.x264-GRP", "The Collection"),
    ("The.Complete.Unknown.2024.1080p.WEB-DL-GRP", "The Complete Unknown"),
    # German dual language, which is what most of the library looks like.
    ("Zodiac.DC.2007.1080p.BluRay.AC3.DL.x264-HDC", "Zodiac"),
    ("Der.Untergang.2004.German.DL.1080p.BluRay.x264-GRP", "Der Untergang"),
    # Channel counts and dates that are not ranges.
    ("Movie.2020.1080p.BluRay.DTS-HD.MA.7.1.x264-GRP", "Movie"),
    ("Movie.2020.1080p.WEB-DL.DDP5.1.H.264-GRP", "Movie"),
]


@pytest.mark.parametrize("name,title", SINGLES)
def test_one_film_is_left_alone(name, title):
    verdict = packs.judge(name, movie(title))
    assert not verdict.is_pack, (
        f"{name} would have been thrown away: {verdict.confidence} "
        f"{verdict.reasons}")


def test_a_single_disc_that_says_complete_is_not_a_box_set():
    """"COMPLETE UHD BLURAY" is a whole disc, not a whole series — a very
    common way to say "nothing was left out of this one film"."""
    verdict = packs.judge("The.Matrix.1999.COMPLETE.UHD.BLURAY-GRP",
                          movie("The Matrix", 1999))
    assert not verdict.is_pack
    assert verdict.confidence < packs.SURE, "but it is worth noticing"


def test_the_word_in_the_films_own_title_does_not_count_against_it():
    """Otherwise every film called "Collection" or "Trilogy" is thrown away."""
    assert not packs.judge("The.Collection.2012.1080p-GRP",
                           movie("The Collection")).is_pack
    assert not packs.judge("Trilogy.of.Terror.1975.1080p-GRP",
                           movie("Trilogy of Terror")).is_pack


def test_a_year_in_the_films_own_title_does_not_make_a_span():
    """"Blade Runner 2049 (2017)" reads as 2049–2017 to anybody not paying
    attention, which is backwards and also not a span."""
    verdict = packs.judge("Blade.Runner.2049.2017.2160p-GRP",
                          movie("Blade Runner 2049", 2017))
    assert not verdict.is_pack


def test_a_backwards_span_is_not_a_span():
    assert not packs.judge("Movie.2018-2007.1080p-GRP", movie("Movie")).is_pack


def test_a_date_is_not_a_span():
    """"2007-06" is a month, not eleven years of films."""
    assert not packs.judge("Show.2007-06-12.1080p.WEB-GRP", movie("Show")).is_pack


def test_nothing_at_all_is_said_about_an_ordinary_name():
    verdict = packs.judge("Movie.2020.1080p.BluRay.x264-GRP", movie("Movie"))
    assert verdict.is_pack is False
    assert verdict.confidence == 0.0
    assert verdict.reasons == ()


def test_an_empty_name_is_survivable():
    assert not packs.judge("", {}).is_pack
    assert not packs.judge("", None).is_pack
    assert not packs.judge("Something", {}).is_pack


def test_a_resolution_written_out_is_not_a_pile_of_years():
    assert not packs.judge("Movie.2020.1920x1080.BluRay-GRP", movie("Movie")).is_pack
