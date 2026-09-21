"""Tests for the profile builder.

The important ones here are not about tidiness. A quality profile decides what
a service fetches, and the failure everybody runs into is a *download loop*:
the same release grabbed, imported, judged insufficient, and grabbed again, for
as long as nobody notices. These pin down the arithmetic that makes that
impossible.

The release names are real shapes, including the German ones, because the
whole point of the language rule is that the marker the services cannot read
is the one most German releases actually carry.
"""
from __future__ import annotations

import re

import pytest

from app import profiles


def wish(**over) -> profiles.Wish:
    base = {"name": "Test", "resolutions": ("1080p",), "audio": "gut",
            "codec": "any", "languages": ("de",)}
    base.update(over)
    return profiles.Wish(**base)


# ---------------------------------------------------------------------------
# The loop, and why this one cannot
# ---------------------------------------------------------------------------
def test_the_upgrade_target_never_rises_above_the_floor():
    """This single number is what decides whether a profile can loop.

    "Keep upgrading until the file scores this" set above the score a release
    had to clear means the service is permanently shopping — and it is obeyed
    forever the moment a file scores lower after import than its release scored
    before it, which is what happens when the name changes on the way in.
    """
    for answers in (wish(), wish(language_required=True),
                    wish(languages=("de", "en"), language_required=True),
                    wish(audio="sehr_gut", codec="x265", colour="dv",
                         min_gb=4, max_gb=40, language_required=True),
                    _everything()):
        plan = profiles.build(answers)
        assert plan.cutoff_score == plan.min_score
        assert not profiles.check(plan)


def test_a_target_above_the_floor_is_refused_outright():
    """Stated as a check as well, so it stays true if the builder changes."""
    plan = profiles.build(wish())
    plan.cutoff_score = plan.min_score + 1
    problems = dict(profiles.check(plan))
    assert "profiles.problem.cutoff_above_floor" in problems


def test_a_meaningful_step_is_required_before_anything_is_replaced():
    """Without one, two releases a few points apart take turns forever."""
    assert profiles.UPGRADE_STEP > 0


def test_a_floor_nothing_can_reach_is_refused():
    """Then nothing is ever grabbed and the service searches for good."""
    plan = profiles.build(wish(language_required=True))
    plan.min_score = 99999
    assert "profiles.problem.floor_too_high" in dict(profiles.check(plan))


def test_asking_for_no_resolution_at_all_is_refused():
    plan = profiles.build(wish())
    plan.resolutions = ()
    assert "profiles.problem.no_quality" in dict(profiles.check(plan))


def test_a_size_window_nothing_fits_through_is_refused():
    plan = profiles.build(wish(min_gb=30, max_gb=10))
    assert "profiles.problem.impossible_size" in dict(profiles.check(plan))


# ---------------------------------------------------------------------------
# The floor
# ---------------------------------------------------------------------------
def test_nothing_is_demanded_unless_it_is_asked_for():
    plan = profiles.build(wish(language_required=False))
    assert plan.min_score == 0


def test_a_compulsory_language_raises_the_floor_to_exactly_its_own_score():
    """Enough to demand it, never more. A floor above the thing it is there to
    require would refuse releases that carry it."""
    plan = profiles.build(wish(language_required=True))
    assert plan.min_score == profiles.WANTED_LANGUAGE
    german = next(f for f in plan.formats if f.name == "Language DE")
    assert german.score == plan.min_score


def _everything() -> profiles.Wish:
    """Every preference switched on at once — the hardest case for a refusal."""
    return profiles.Wish(
        name="Everything", resolutions=("720p", "1080p", "2160p"),
        sources=profiles.SOURCES, audio="nah", surround=True, codec="x265",
        languages=("de", "en", "fr"), language_required=True, colour="dv",
        edition="extended", streamers=profiles.STREAMERS, good_groups=True,
        prefer_repack=True, min_gb=2, max_gb=90)


def test_what_is_unwanted_is_worth_less_than_every_bonus_together():
    """Otherwise a camera recording with the right language, the right sound
    and a well-regarded group on it scores its way over the floor and is
    grabbed on merit — which is the opposite of the point.

    A fixed figure stopped being enough the moment the builder learned a new
    preference, so it is worked out from the bonuses each profile hands out.
    """
    for answers in (wish(language_required=True), _everything()):
        plan = profiles.build(answers)
        bonuses = sum(f.score for f in plan.formats if f.score > 0)
        worst = min(f.score for f in plan.formats if f.score < 0)
        assert bonuses + worst < plan.min_score, (
            f"a refused release could still reach the floor: "
            f"{bonuses} + {worst} >= {plan.min_score}")


def test_a_refusal_that_could_be_outvoted_is_refused():
    plan = profiles.build(_everything())
    for entry in plan.formats:
        if entry.score < 0:
            entry.score = -10          # far too small to matter
    assert "profiles.problem.refusal_too_weak" in dict(profiles.check(plan))


def test_the_refusal_grows_with_the_number_of_preferences():
    """More things to like means a bigger number needed to say no."""
    plain = min(f.score for f in profiles.build(wish()).formats if f.score < 0)
    loaded = min(f.score for f in profiles.build(_everything()).formats
                 if f.score < 0)
    assert loaded < plain


# ---------------------------------------------------------------------------
# The patterns, against names that really exist
# ---------------------------------------------------------------------------
GERMAN = [
    # The one the services cannot read: German plus the original audio.
    "Zodiac.DC.2007.1080p.BluRay.AC3.DL.x264-HDC",
    "Der.Untergang.2004.German.1080p.BluRay.x264-GRP",
    "Movie.2020.German.DL.1080p.BluRay.x264",
    "Movie.2019.GerDub.1080p.WEB-DL",
    "Movie.2021.[DE+EN].1080p.WEBRip",
    "Film.2018.Deutsch.Synchro.1080p",
]
NOT_GERMAN = [
    "Some.Movie.2019.1080p.BluRay.x264-GRP",
    "Movie.2020.DLMux.ITA.1080p",              # Italian, not a German DL
    "Movie.2020.FRENCH.1080p.BluRay",
]


@pytest.mark.parametrize("name", GERMAN)
def test_a_german_release_is_recognised(name):
    assert re.search(profiles.LANGUAGE_PATTERNS["de"], name), name


@pytest.mark.parametrize("name", NOT_GERMAN)
def test_something_that_is_not_german_is_not(name):
    assert not re.search(profiles.LANGUAGE_PATTERNS["de"], name), name


@pytest.mark.parametrize("name,tier", [
    ("Movie.2020.1080p.TrueHD.Atmos.x264", "sehr_gut"),
    ("Movie.2020.1080p.DTS-HD.MA.x264", "sehr_gut"),
    ("Movie.2020.1080p.EAC3.x264", "gut"),
    ("Movie.2020.1080p.DTS.x264", "gut"),
    ("Movie.2020.1080p.AC3.x264", "ok"),
    ("Movie.2020.1080p.AAC2.0.x264", "nah"),
])
def test_sound_lands_in_the_right_tier(name, tier):
    """AC3 must not be read as EAC3, and AAC 2.0 must not be read as AAC."""
    hits = [name_ for name_, pattern in profiles.AUDIO_PATTERNS.items()
            if re.search(pattern, name)]
    assert tier in hits, f"{name} landed in {hits}"


@pytest.mark.parametrize("name", [
    "Movie.2018.3D.HSBS.1080p.BluRay",
    "Black.Panther.3D.HOU.2018.German.DTS.DL.1080p.BluRay.x264-LeetHD",
    "Meg.2018.HSBS.German.Dubbed.AC3.DL.1080p.BluRay.x264-miHD",
])
def test_two_pictures_are_seen(name):
    """The services cannot see most of these: a marker in front of the year is
    swallowed by their parser. A custom format reads the whole name."""
    assert re.search(profiles.THREE_D_PATTERN, name), name


@pytest.mark.parametrize("name", [
    "Movie.2026.HDCAM.1080p", "Movie.2026.TS.XviD", "Movie.2020.DVDSCR.x264",
    "Movie.2019.BR-DISK.1080p", "Movie.2019.1080p.UPSCALED.x265",
])
def test_rubbish_is_seen(name):
    assert re.search(profiles.RUBBISH_PATTERN, name), name


def test_an_ordinary_release_is_none_of_those_things():
    name = "Der.Untergang.2004.German.DL.1080p.BluRay.x264-GRP"
    assert not re.search(profiles.THREE_D_PATTERN, name)
    assert not re.search(profiles.RUBBISH_PATTERN, name)


def test_every_pattern_compiles_with_the_strictest_engine():
    """Python's own engine is stricter than .NET's about inline flags, so a
    pattern it accepts works in the services too."""
    for code, pattern in profiles.LANGUAGE_PATTERNS.items():
        re.compile(pattern), code
    for tier, pattern in profiles.AUDIO_PATTERNS.items():
        re.compile(pattern), tier
    for pattern in (*profiles.CODEC_PATTERNS.values(), profiles.THREE_D_PATTERN,
                    profiles.RUBBISH_PATTERN, profiles.HDR_PATTERN):
        re.compile(pattern)


# ---------------------------------------------------------------------------
# Straightening out the answers
# ---------------------------------------------------------------------------
def test_nonsense_is_straightened_out_rather_than_carried():
    tidy = profiles.Wish(name="  x  ", resolutions=("4320p",), audio="excellent",
                         codec="vp9", languages=("de", "klingon"),
                         min_gb=-5, max_gb=99999).tidy()
    assert tidy.name == "x"
    assert tidy.resolutions == ("1080p",)          # nothing valid, so the default
    assert tidy.audio == "gut"
    assert tidy.codec == "any"
    assert tidy.languages == ("de",)
    assert tidy.min_gb == 0 and tidy.max_gb == 2000


def test_a_language_cannot_be_required_when_none_is_chosen():
    assert profiles.Wish(languages=(), language_required=True).tidy() \
        .language_required is False


def test_the_resolutions_keep_the_ladder_order():
    """Picked in any order, they still mean worst-to-best."""
    tidy = profiles.Wish(resolutions=("2160p", "720p")).tidy()
    assert tidy.resolutions == ("720p", "2160p")


# ---------------------------------------------------------------------------
# The ladder, built from what the service says it has
# ---------------------------------------------------------------------------
SCHEMA_ITEMS = [
    {"quality": {"id": 1, "name": "SDTV"}, "items": [], "allowed": False},
    {"quality": {"id": 4, "name": "HDTV-720p"}, "items": [], "allowed": False},
    {"id": 1000, "name": "WEB 1080p", "allowed": False, "items": [
        {"quality": {"id": 3, "name": "WEBDL-1080p"}, "items": []},
        {"quality": {"id": 15, "name": "WEBRip-1080p"}, "items": []}]},
    {"quality": {"id": 7, "name": "Bluray-1080p"}, "items": [], "allowed": False},
    {"quality": {"id": 30, "name": "Remux-1080p"}, "items": [], "allowed": False},
    {"quality": {"id": 19, "name": "Bluray-2160p"}, "items": [], "allowed": False},
]


def test_only_what_was_asked_for_is_switched_on():
    items, best = profiles.quality_items(SCHEMA_ITEMS, ("1080p",), False)
    on = [i.get("quality", {}).get("name") or i.get("name")
          for i in items if i["allowed"]]
    assert sorted(on) == ["Bluray-1080p", "WEB 1080p"]
    assert best is not None


def test_a_group_is_on_when_any_of_its_rungs_is():
    items, _best = profiles.quality_items(SCHEMA_ITEMS, ("1080p",), False)
    group = next(i for i in items if i.get("name") == "WEB 1080p")
    assert group["allowed"] is True
    assert [c["allowed"] for c in group["items"]] == [True, True]


def test_remux_stays_out_unless_it_is_asked_for():
    items, _ = profiles.quality_items(SCHEMA_ITEMS, ("1080p",), False)
    named = lambda rows, name: next(          # noqa: E731
        i for i in rows if (i.get("quality") or {}).get("name") == name)
    assert named(items, "Remux-1080p")["allowed"] is False

    items, _ = profiles.quality_items(SCHEMA_ITEMS, ("1080p",), True)
    assert named(items, "Remux-1080p")["allowed"] is True


def test_the_cutoff_is_a_quality_that_is_switched_on():
    """A cutoff pointing at something disabled can never be reached, and the
    service goes on searching for it."""
    items, best = profiles.quality_items(SCHEMA_ITEMS, ("1080p",), False)
    allowed_ids = set()
    for entry in items:
        if entry["allowed"]:
            if entry.get("quality"):
                allowed_ids.add(entry["quality"]["id"])
            else:
                allowed_ids.add(entry["id"])
                allowed_ids |= {c["quality"]["id"] for c in entry["items"]
                                if c["allowed"]}
    assert best in allowed_ids


def test_a_ladder_with_nothing_on_it_says_so():
    _items, best = profiles.quality_items(SCHEMA_ITEMS, ("2160p",), False)
    assert best is not None          # Bluray-2160p is in the schema
    _items, best = profiles.quality_items([], ("1080p",), False)
    assert best is None


# ---------------------------------------------------------------------------
# The bodies that are sent
# ---------------------------------------------------------------------------
FORMAT_SCHEMA = [
    {"implementation": "ReleaseTitleSpecification",
     "implementationName": "Release Title",
     "fields": [{"name": "value", "value": ""}]},
    {"implementation": "SizeSpecification", "implementationName": "Size",
     "fields": [{"name": "min", "value": 0}, {"name": "max", "value": 0}]},
]


def test_a_condition_is_filled_into_the_services_own_template():
    """Field names are not guessed. A version that renames one is then a
    non-event rather than a rejected profile."""
    built = profiles.specification(
        FORMAT_SCHEMA, "SizeSpecification", {"min": 4.0, "max": 25.0},
        negate=False, required=False)
    assert built["implementation"] == "SizeSpecification"
    assert {f["name"]: f["value"] for f in built["fields"]} == {"min": 4.0, "max": 25.0}


def test_a_condition_the_service_does_not_have_is_refused():
    with pytest.raises(ValueError, match="unknown_specification"):
        profiles.specification(FORMAT_SCHEMA, "MadeUpSpecification", {},
                               negate=False, required=False)


def test_every_format_the_builder_makes_can_be_built_for_the_service():
    plan = profiles.build(_everything())
    for entry in plan.formats:
        body = profiles.custom_format_body(FORMAT_SCHEMA, entry)
        assert body["name"].startswith(profiles.MARK + ":")
        assert body["specifications"]


def test_the_profile_body_carries_the_three_numbers():
    plan = profiles.build(wish(language_required=True, upgrade=False))
    body = profiles.profile_body(plan, SCHEMA_ITEMS, 7, [], None)
    assert body["minFormatScore"] == plan.min_score
    assert body["cutoffFormatScore"] == plan.cutoff_score
    assert body["minUpgradeFormatScore"] == profiles.UPGRADE_STEP
    assert body["upgradeAllowed"] is False
    assert body["cutoff"] == 7


def test_the_profile_does_not_demand_a_language_of_the_service():
    """Before the import the language is read out of the name, and the German
    ".DL." is not a marker the parser knows — so a profile that demands German
    throws away the German releases it was set up to find. Measured: see
    app/scoring.py."""
    plan = profiles.build(wish(languages=("de",), language_required=True))
    body = profiles.profile_body(
        plan, SCHEMA_ITEMS, 7, [],
        profiles.any_language([{"id": -1, "name": "Any"},
                               {"id": 4, "name": "German"}]))
    assert body["language"] == {"id": -1, "name": "Any"}


def test_everything_it_writes_is_named_after_this_program():
    """So it can be written again without leaving a second copy behind, and so
    a profile somebody made by hand is never touched."""
    plan = profiles.build(_everything())
    assert all(f.full_name.startswith("Correctarr: ") for f in plan.formats)


# ---------------------------------------------------------------------------
# Where it came from
# ---------------------------------------------------------------------------
def test_the_ladder_follows_the_sources_as_well_as_the_resolutions():
    """A disc rip and a broadcast capture at 1080p are both "1080p" and are not
    the same thing, so asking for one without the other has to be possible."""
    items, _best = profiles.quality_items(
        SCHEMA_ITEMS, ("1080p",), False, ("bluray",))
    on = [i.get("quality", {}).get("name") or i.get("name")
          for i in items if i["allowed"]]
    assert on == ["Bluray-1080p"]


def test_asking_for_web_only_leaves_the_disc_out():
    items, _best = profiles.quality_items(
        SCHEMA_ITEMS, ("1080p",), False, ("webdl", "webrip"))
    on = [i.get("quality", {}).get("name") or i.get("name")
          for i in items if i["allowed"]]
    assert on == ["WEB 1080p"]


def test_no_source_at_all_is_refused():
    plan = profiles.build(wish())
    object.__setattr__(plan.wish, "sources", ())
    assert "profiles.problem.no_source" in dict(profiles.check(plan))


def test_a_later_source_in_the_list_is_worth_more():
    """The order is the preference: a disc beats a broadcast capture."""
    plan = profiles.build(wish(sources=("hdtv", "webdl", "bluray")))
    scores = {f.name: f.score for f in plan.formats}
    assert scores["Source hdtv"] < scores["Source webdl"] < scores["Source bluray"]


# ---------------------------------------------------------------------------
# Dolby Vision that plays green
# ---------------------------------------------------------------------------
def test_dolby_vision_without_a_fallback_is_refused_not_ranked_last():
    """It looks like the best release in the list right up until it plays."""
    plan = profiles.build(wish(colour="dv"))
    entry = next(f for f in plan.formats
                 if f.name == "Dolby Vision without fallback")
    assert entry.score < 0


def test_nothing_is_said_about_dolby_vision_when_it_was_not_asked_for():
    plan = profiles.build(wish(colour="sdr"))
    assert not any("Dolby" in f.name for f in plan.formats)


@pytest.mark.parametrize("name,refused", [
    ("Movie.2020.2160p.DV.WEB-DL-GRP", True),
    ("Movie.2020.2160p.DV.HDR10.WEB-DL-GRP", False),
    ("Movie.2020.2160p.DoVi.HYBRID.BluRay-GRP", False),
    ("Movie.2020.2160p.HDR.WEB-DL-GRP", False),
    ("Movie.2020.1080p.BluRay-GRP", False),
])
def test_only_dolby_vision_on_its_own_is_caught(name, refused):
    assert bool(re.search(profiles.DV_NO_FALLBACK, name)) is refused


# ---------------------------------------------------------------------------
# The rest of the new preferences
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name,hit", [
    ("A.2020.1080p.DDP5.1.WEB-DL", True),
    ("A.2020.1080p.TrueHD.7.1.BluRay", True),
    ("A.2020.1080p.AAC2.0.WEB-DL", False),
])
def test_surround_is_told_from_stereo(name, hit):
    assert bool(re.search(profiles.SURROUND_PATTERN, name)) is hit


@pytest.mark.parametrize("name,hit", [
    ("A.2020.1080p.HC.HDRip.x264", True),
    ("A.2020.1080p.KORSUB.WEB-DL", True),
    ("A.2020.1080p.WEB-DL.x264-GRP", False),
])
def test_burned_in_subtitles_are_seen(name, hit):
    assert bool(re.search(profiles.HARDCODED_SUBS, name)) is hit


@pytest.mark.parametrize("name,hit", [
    ("A.2020.1080p.WEB.x264-RARBG", True),
    ("A.2020.1080p.WEB.x264[OBFUSCATED]", True),
    ("A.2020.1080p.WEB.x264-GRP", False),
])
def test_a_scrambled_name_is_seen(name, hit):
    assert bool(re.search(profiles.RETAGGED, name)) is hit


def test_a_web_dl_is_not_a_webrip():
    """They are re-encodes of each other and people care about the difference."""
    assert re.search(profiles.SOURCE_PATTERNS["webdl"], "A.2020.1080p.WEB-DL")
    assert not re.search(profiles.SOURCE_PATTERNS["webdl"], "A.2020.1080p.WEBRip")
    assert re.search(profiles.SOURCE_PATTERNS["webrip"], "A.2020.1080p.WEBRip")


def test_an_old_wish_with_the_remux_switch_still_loads():
    """The switch beside the resolutions became a source. A page cached from
    an older build still sends the switch."""
    tidy = profiles.Wish(sources=("bluray",), allow_remux=True).tidy()
    assert "remux" in tidy.sources


# ---------------------------------------------------------------------------
# Writing it into a service, from end to end
# ---------------------------------------------------------------------------
class FakeService:
    """A Radarr that records what it was told rather than storing it."""

    kind = "radarr"
    name = "Radarr"

    def __init__(self, existing_formats=(), existing_profiles=()):
        self._formats = list(existing_formats)
        self._profiles = list(existing_profiles)
        self.saved_formats = []
        self.saved_profiles = []
        self._next = 100

    def custom_format_schema(self):
        return FORMAT_SCHEMA

    def custom_formats(self):
        return list(self._formats)

    def quality_profile_schema(self):
        return {"items": SCHEMA_ITEMS}

    def profiles(self):
        return list(self._profiles)

    def languages(self):
        return [{"id": -1, "name": "Any"}, {"id": 4, "name": "German"}]

    def save_custom_format(self, body, existing_id=None):
        self.saved_formats.append((existing_id, body))
        self._next += 1
        saved = {**body, "id": existing_id or self._next}
        self._formats = [f for f in self._formats if f["name"] != body["name"]]
        self._formats.append(saved)
        return saved

    def save_quality_profile(self, body, existing_id=None):
        self.saved_profiles.append((existing_id, body))
        return {**body, "id": existing_id or 7}


def test_a_profile_and_its_formats_are_written():
    service = FakeService()
    plan = profiles.build(wish(language_required=True, min_gb=4, max_gb=25))
    result = profiles.apply_to(service, plan)

    assert result["updated"] is False
    assert result["formats"] == len(plan.formats)
    assert len(service.saved_formats) == len(plan.formats)
    _existing, body = service.saved_profiles[0]
    assert body["minFormatScore"] == body["cutoffFormatScore"]
    assert body["name"] == "Test"


def test_saving_twice_changes_the_same_profile():
    """Otherwise the second attempt leaves "Test (1)" behind, and the person
    now has two profiles that disagree."""
    service = FakeService()
    plan = profiles.build(wish())
    profiles.apply_to(service, plan)
    service._profiles = [{"id": 7, "name": "Test"}]

    again = profiles.apply_to(service, plan)
    assert again["updated"] is True
    assert service.saved_profiles[-1][0] == 7
    # And the formats were updated in place rather than added beside.
    assert all(existing for existing, _body in service.saved_formats[-len(plan.formats):])


def test_a_format_somebody_made_by_hand_is_left_alone():
    """It is theirs. Nothing here is allowed to quietly change what their
    other profiles do."""
    mine = {"id": 55, "name": "TRaSH: Something"}
    service = FakeService(existing_formats=[mine])
    plan = profiles.build(wish())
    profiles.apply_to(service, plan)

    assert all(body["name"].startswith("Correctarr:")
               for _existing, body in service.saved_formats)
    _existing, profile = service.saved_profiles[0]
    theirs = next(f for f in profile["formatItems"] if f["name"] == mine["name"])
    assert theirs["score"] == 0, "somebody else's format must be listed at zero"


def test_every_format_the_service_knows_is_listed_on_the_profile():
    """A profile that names some and not others is refused by the service."""
    service = FakeService(existing_formats=[{"id": 55, "name": "Other"}])
    plan = profiles.build(wish(colour="hdr"))
    profiles.apply_to(service, plan)
    _existing, profile = service.saved_profiles[0]
    assert {f["name"] for f in profile["formatItems"]} == {
        "Other", *(f.full_name for f in plan.formats)}


def test_a_profile_that_could_loop_is_never_written():
    service = FakeService()
    plan = profiles.build(wish())
    plan.cutoff_score = 50000
    with pytest.raises(ValueError, match="cutoff_above_floor"):
        profiles.apply_to(service, plan)
    assert service.saved_profiles == []
    assert service.saved_formats == []
