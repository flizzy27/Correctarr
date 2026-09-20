"""Tests for the custom format reimplementation.

Scoring decides whether a waiting download gets blocklisted. A mistake here
throws away a good release.
"""
from __future__ import annotations

import regex

from app.scoring import CHECKABLE, format_matches, release_from_entry, score


def spec(implementation, fields, negate=False, required=False):
    return {"implementation": implementation, "negate": negate, "required": required,
            "fields": [{"name": k, "value": v} for k, v in fields.items()]}


def entry(title, gb=10.0, year=2018, resolution=1080, source="bluray"):
    return {"title": title, "size": int(gb * 1024 ** 3),
            "quality": {"quality": {"resolution": resolution, "source": source,
                                    "modifier": "none"}},
            "movie": {"year": year}, "languages": [{"id": 1, "name": "English"}]}


# --------------------------------------------- the reason this module exists
def test_variable_lookbehind_is_supported():
    """Radarr runs on .NET. The published profile patterns use variable width
    lookbehind throughout. The built-in ``re`` refuses those — measured, 523 of
    814 patterns failed. ``regex`` handles them."""
    pattern = r"(?<=^|[\s.-])YIFY\b"
    assert regex.search(pattern, "Film.2020.1080p-YIFY", regex.IGNORECASE)

    import re as builtin
    try:
        builtin.compile(pattern)
        accepted = True
    except builtin.error:
        accepted = False
    assert not accepted, ("The built-in re suddenly accepts variable lookbehind — "
                          "the dependency on regex could be revisited.")


def test_3d_is_detected_on_the_raw_name():
    """Radarr swallows markers placed before the year, so the raw release name
    is what gets checked here."""
    custom_format = {"specifications": [
        spec("ReleaseTitleSpecification", {"value": r"\b(3D|HOU|HSBS|SBS)\b"})]}
    name = "Black.Panther.3D.HOU.2018.German.DTS.DL.1080p.BluRay.x264-LeetHD"
    assert format_matches(custom_format, release_from_entry(entry(name)))


def test_language_is_not_checked():
    """LanguageSpecification is deliberately excluded: before the import the
    language comes from the file name, and the German ".DL." marker is not
    something the parser knows. Without that exclusion good releases are thrown
    away in bulk."""
    assert "LanguageSpecification" not in CHECKABLE


# ------------------------------------------------------------ combining logic
def test_an_uncheckable_spec_makes_the_format_uncheckable():
    """Better not to score at all than to score wrongly."""
    custom_format = {"specifications": [
        spec("ReleaseTitleSpecification", {"value": "1080p"}),
        spec("LanguageSpecification", {"value": 1}),
    ]}
    assert format_matches(custom_format, release_from_entry(entry("F.2018.1080p"))) is False


def test_required_conditions_must_all_match():
    custom_format = {"specifications": [
        spec("ReleaseTitleSpecification", {"value": "German"}, required=True),
        spec("ReleaseTitleSpecification", {"value": "1080p"}),
    ]}
    assert format_matches(custom_format, release_from_entry(entry("F.2018.German.1080p")))
    assert not format_matches(custom_format, release_from_entry(entry("F.2018.English.1080p")))


def test_at_least_one_optional_must_match():
    custom_format = {"specifications": [
        spec("ReleaseTitleSpecification", {"value": "xvid"}),
        spec("ReleaseTitleSpecification", {"value": "divx"}),
    ]}
    assert format_matches(custom_format, release_from_entry(entry("F.2018.XviD")))
    assert not format_matches(custom_format, release_from_entry(entry("F.2018.x264")))


def test_negate_inverts():
    custom_format = {"specifications": [
        spec("ReleaseTitleSpecification", {"value": "German"}, negate=True)]}
    assert format_matches(custom_format, release_from_entry(entry("F.2018.English")))
    assert not format_matches(custom_format, release_from_entry(entry("F.2018.German")))


def test_a_format_without_conditions_never_matches():
    assert not format_matches({"specifications": []}, release_from_entry(entry("X.2018")))


def test_an_invalid_pattern_does_not_raise():
    """A broken pattern must not abort the whole run."""
    custom_format = {"specifications": [
        spec("ReleaseTitleSpecification", {"value": "([unterminated"})]}
    assert format_matches(custom_format, release_from_entry(entry("F.2018"))) is False


# ------------------------------------------------------ individual conditions
def test_size_follows_radarr_semantics():
    """Radarr: greater than min, less than or equal to max."""
    custom_format = {"specifications": [spec("SizeSpecification", {"min": 5, "max": 15})]}
    assert format_matches(custom_format, release_from_entry(entry("F.2018", gb=10)))
    assert not format_matches(custom_format, release_from_entry(entry("F.2018", gb=5)))
    assert format_matches(custom_format, release_from_entry(entry("F.2018", gb=15)))
    assert not format_matches(custom_format, release_from_entry(entry("F.2018", gb=20)))


def test_resolution():
    custom_format = {"specifications": [spec("ResolutionSpecification", {"value": "1080"})]}
    assert format_matches(custom_format, release_from_entry(entry("F.2018", resolution=1080)))
    assert not format_matches(custom_format, release_from_entry(entry("F.2018", resolution=2160)))


def test_year_range():
    custom_format = {"specifications": [spec("YearSpecification", {"min": 2010, "max": 2020})]}
    assert format_matches(custom_format, release_from_entry(entry("F.2018", year=2018)))
    assert not format_matches(custom_format, release_from_entry(entry("F.2005", year=2005)))


def test_release_group_is_recognised():
    release = release_from_entry(entry("Film.2018.1080p.BluRay.x264-LeetHD"))
    assert release["group"] == "LeetHD"


# ------------------------------------------------------------------- scoring
def test_score_sums_up_and_names_the_hits():
    profile = {"formatItems": [
        {"name": "3D", "score": -999999},
        {"name": "No Effect", "score": 0},
        {"name": "Good Group", "score": 150},
    ]}
    formats = {
        "3D": {"specifications": [spec("ReleaseTitleSpecification", {"value": r"\b3D\b"})]},
        "No Effect": {"specifications": [spec("ReleaseTitleSpecification", {"value": "."})]},
        "Good Group": {"specifications": [spec("ReleaseGroupSpecification", {"value": "LeetHD"})]},
    }
    total, hits = score(entry("Film.3D.2018.1080p.BluRay.x264-LeetHD"), profile, formats)
    assert total == -999999 + 150
    assert any("3D" in h for h in hits)
    # Formats worth zero are never even checked.
    assert not any("No Effect" in h for h in hits)


def test_score_without_hits_is_zero():
    profile = {"formatItems": [{"name": "3D", "score": -999999}]}
    formats = {"3D": {"specifications": [
        spec("ReleaseTitleSpecification", {"value": r"\b3D\b"})]}}
    total, hits = score(entry("Film.2018.1080p.BluRay-ABC"), profile, formats)
    assert total == 0
    assert hits == []
