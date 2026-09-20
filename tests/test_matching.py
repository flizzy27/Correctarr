"""Tests for the name matcher.

This is the part of the program with the greatest potential to do damage: a
wrong match can, in the worst case, overwrite a good file with a different
film. Every case here comes from production — each one was a real failure at
some point.
"""
from __future__ import annotations

import pytest

from app.matching import build_candidates, match, variants, years


def item(title, year, item_id=1, alternates=(), original=None):
    return {"id": item_id, "title": title, "year": year,
            "originalTitle": original or title,
            "alternateTitles": [{"title": a} for a in alternates]}


# ---------------------------------------------------------------- variants
def test_roman_numeral_is_converted():
    """Radarr could not find "Crank.I.2006" — the film is "Crank" with the
    alternate title "Crank 1"."""
    assert "crank 1" in variants("Crank.I.2006.German.1080p.BluRay.x264-ABC")


def test_leading_article_is_dropped():
    """"The.Transporter.The.Mission" failed on the leading "The"."""
    result = variants("The.Transporter.The.Mission.2005.1080p.BluRay")
    assert any(v.startswith("transporter") for v in result)


def test_umlauts_in_both_spellings():
    result = variants("Über den Dächern 2019")
    assert "ueber den daechern" in result or "uber den dachern" in result


def test_technical_markers_are_cut_off():
    result = variants("Voll.Abgezockt.2013.German.DL.1080p.BluRay.x264-ENCOUNTER")
    assert "voll abgezockt" in result
    assert not any("1080p" in v for v in result)


def test_part_number_becomes_a_digit():
    assert any("2" in v for v in variants("Kill Bill Part 2 2004"))


# -------------------------------------------------------- degenerate input
def test_transliteration_ruin_is_rejected():
    """"Хроники Нарнии 1" normalises down to exactly "1" — and "1" sits
    entirely inside "crank 1", giving a 100 percent match with a completely
    different film."""
    assert variants("Хроники Нарнии 1") == set()


def test_too_short_variants_are_rejected():
    assert variants("42") == set()
    assert variants("X") == set()


# ------------------------------------------------------------------- years
def test_years_finds_only_plausible_ones():
    assert years("Crank.I.2006.1080p") == [2006]
    assert years("Film.1899.mkv") == []
    assert years("Film.2099.mkv") == []
    # 1080 is a resolution, not a year
    assert 1080 not in years("Film.2010.1080p.BluRay")


# ------------------------------------------------------------------ match
def test_crank_is_found_through_its_alternate_title():
    candidates = build_candidates([item("Crank", 2006, 1, alternates=["Crank 1"])])
    found, confidence, reason = match("Crank.I.2006.German.1080p.BluRay.x264-ABC",
                                      candidates)
    assert found is not None, reason
    assert found["id"] == 1
    assert confidence >= 0.85


def test_saw_two_beats_saw_three():
    """A practically perfect match wins even against a close runner-up: "Saw II"
    matches at 100 percent while "Saw III" reaches 92 — the margin is small but
    the match is beyond doubt."""
    candidates = build_candidates([item("Saw II", 2005, 1), item("Saw III", 2006, 2)])
    found, _, reason = match("Saw.II.2005.German.1080p.BluRay.x264-ABC", candidates)
    assert found is not None, reason
    assert found["id"] == 1


def test_a_single_shared_word_is_not_enough():
    """"Saw" sits entirely inside "Saw II" but is a different film."""
    candidates = build_candidates([item("Saw II", 2005, 1)])
    found, _, _ = match("Saw.2004.German.1080p.BluRay.x264-ABC", candidates)
    assert found is None


def test_a_wrong_year_is_never_even_compared():
    candidates = build_candidates([item("Halloween", 1978, 1)])
    found, _, reason = match("Halloween.2018.German.1080p.BluRay.x264-ABC", candidates)
    assert found is None
    assert "year" in reason


def test_year_tolerance_is_honoured():
    candidates = build_candidates([item("Some Film", 2019, 1)])
    assert match("Some.Film.2020.1080p.BluRay-ABC", candidates, 1)[0] is not None
    assert match("Some.Film.2022.1080p.BluRay-ABC", candidates, 1)[0] is None


def test_without_a_year_nothing_is_matched():
    candidates = build_candidates([item("Crank", 2006, 1, alternates=["Crank 1"])])
    found, _, reason = match("Crank.German.1080p.BluRay.x264-ABC", candidates)
    assert found is None
    assert "year" in reason


def test_localised_title_through_alternates():
    candidates = build_candidates([item("Due Date", 2010, 1, alternates=["Stichtag"])])
    found, _, reason = match("Stichtag.2010.German.DL.1080p.BluRay.x264-ABC", candidates)
    assert found is not None, reason
    assert found["id"] == 1


def test_empty_candidate_list_does_not_crash():
    assert match("Anything.2020.1080p", [])[0] is None


def test_unusable_name_does_not_crash():
    candidates = build_candidates([item("Crank", 2006, 1)])
    assert match("", candidates)[0] is None
    assert match("...", candidates)[0] is None


@pytest.mark.parametrize("name", [
    "Der.Unsichtbare.2020.German.DL.1080p.BluRay.x264-ABC",
    "Der Unsichtbare (2020) 1080p",
])
def test_localised_title_in_several_spellings(name):
    candidates = build_candidates([item("The Invisible Man", 2020, 1,
                                        alternates=["Der Unsichtbare"])])
    assert match(name, candidates)[0] is not None
