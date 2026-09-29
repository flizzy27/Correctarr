"""Adversarial cases for the profile builder and the detection algorithms.

Each test here names a release, a title or a service answer that a first
version got wrong. Most of them are ordinary: a WEB-DL, a film with an accent
in its name, a library where every indexer sits at the default priority.
"""
from __future__ import annotations

import re

import pytest

from app import indexers, matching, packs, profiles, rules, scoring
from app import settings as S
from app.storage import Store


def config(**over):
    cfg = S.defaults()
    cfg["cleanup_paths"] = S.cleanup_paths(cfg)
    cfg.update(over)
    return cfg


class Radarr:
    kind, name, service_id = "radarr", "Radarr", 1


class Sonarr:
    kind, name, service_id = "sonarr", "Sonarr", 2


# ===========================================================================
# Profiles: the language markers
# ===========================================================================
@pytest.mark.parametrize("name", [
    "Movie.2020.1080p.WEB-DL.x264-GRP",
    "Movie.2020.1080p.WEB.DL.DDP5.1.H.264-GRP",
    "Movie.2020.1080p.AMZN.WEB DL.x264-GRP",
    "Show.S01E01.1080p.WEB-DL.DDP5.1-GRP",
])
def test_a_web_dl_is_not_read_as_a_german_dual_language_release(name):
    """"DL" after "WEB" is the source, not German plus original audio. Read
    as German, every English WEB-DL collected the language bonus, and a
    profile with German made compulsory grabbed them as German."""
    assert not re.search(profiles.LANGUAGE_PATTERNS["de"], name), name


@pytest.mark.parametrize("name", [
    "Movie.2020.German.DL.1080p.WEB-DL.x264-GRP",
    "Movie.2020.GERMAN.DL.1080p.WEB.H264-GRP",
    "Zodiac.DC.2007.1080p.BluRay.AC3.DL.x264-HDC",
    "Movie.2020.German.EAC3.DL.1080p.WEB-DL.x264-GRP",
    "Movie.2020.GER.DL.1080p.BluRay.x264-GRP",
])
def test_a_german_dual_language_release_next_to_a_web_dl_is_still_german(name):
    assert re.search(profiles.LANGUAGE_PATTERNS["de"], name), name


@pytest.mark.parametrize("code,name", [
    ("es", "Movie.2020.1080p.Latino.WEB-DL.x264-GRP"),
    ("es", "Movie.2020.SPA.1080p.BluRay.x264-GRP"),
    ("pt", "Movie.2020.1080p.PT-BR.WEB-DL.x264-GRP"),
    ("pt", "Movie.2020.1080p.PTBR.WEB-DL.x264-GRP"),
    ("fr", "Movie.2020.MULTi.VFF.1080p.BluRay.x264-GRP"),
])
def test_the_common_spellings_of_the_other_offered_languages_are_seen(code, name):
    assert re.search(profiles.LANGUAGE_PATTERNS[code], name), name


def test_french_subtitles_are_not_french_audio():
    assert not re.search(profiles.LANGUAGE_PATTERNS["fr"],
                         "Movie.2020.VOSTFR.1080p.WEB-DL.x264-GRP")


def test_every_language_pattern_still_compiles_with_the_strictest_engine():
    for code, pattern in profiles.LANGUAGE_PATTERNS.items():
        re.compile(pattern), code
    re.compile(profiles.COLLECTION_PATTERN)


# ===========================================================================
# Profiles: "any language" is not "the original language"
# ===========================================================================
def test_any_language_is_chosen_even_when_the_original_comes_first():
    languages = [{"id": -2, "name": "Original"}, {"id": -1, "name": "Any"},
                 {"id": 4, "name": "German"}]
    assert profiles.any_language(languages) == {"id": -1, "name": "Any"}


def test_the_original_language_is_never_taken_for_any():
    """Radarr's "Original" means the film's own language. A German library of
    American films set to it refuses every German dub it was built for.
    Sonarr offers no "Any" at all, because its profiles have no language."""
    languages = [{"id": -2, "name": "Original"}, {"id": 4, "name": "German"}]
    assert profiles.any_language(languages) is None


# ===========================================================================
# Profiles: a source left out stays out, even inside a group
# ===========================================================================
SCHEMA = [
    {"quality": {"id": 1, "name": "SDTV"}, "items": [], "allowed": False},
    {"quality": {"id": 9, "name": "HDTV-1080p"}, "items": [], "allowed": False},
    {"id": 1002, "name": "WEB 1080p", "allowed": False, "items": [
        {"quality": {"id": 3, "name": "WEBDL-1080p"}, "items": [], "allowed": False},
        {"quality": {"id": 15, "name": "WEBRip-1080p"}, "items": [], "allowed": False}]},
    {"quality": {"id": 7, "name": "Bluray-1080p"}, "items": [], "allowed": False},
]


def _allowed_quality_names(items):
    """What the service lets through: a group is allowed or not as a whole."""
    names = []
    for entry in items:
        if not entry["allowed"]:
            continue
        if entry.get("items"):
            names += [c["quality"]["name"] for c in entry["items"]]
        else:
            names.append(entry["quality"]["name"])
    return sorted(names)


def test_a_webrip_is_not_let_in_through_the_web_group_when_only_web_dl_was_asked_for():
    """Radarr and Sonarr judge a group by the group's own switch; what the
    members say is not read. A group half wanted has to be taken apart."""
    items, best = profiles.quality_items(SCHEMA, ("1080p",), False,
                                         ("webdl", "bluray"))
    assert _allowed_quality_names(items) == ["Bluray-1080p", "WEBDL-1080p"]
    assert best == 7


def test_every_quality_is_still_on_the_ladder_exactly_once_after_a_group_is_split():
    items, _best = profiles.quality_items(SCHEMA, ("1080p",), False, ("webdl",))
    seen = []
    for entry in items:
        if entry.get("items"):
            seen += [c["quality"]["id"] for c in entry["items"]]
        else:
            seen.append(entry["quality"]["id"])
    assert sorted(seen) == [1, 3, 7, 9, 15]


def test_the_cutoff_can_be_a_member_of_a_group_that_was_split():
    items, best = profiles.quality_items(SCHEMA, ("1080p",), False, ("webdl",))
    assert best == 3
    assert _allowed_quality_names(items) == ["WEBDL-1080p"]


def test_a_group_wanted_as_a_whole_stays_a_group():
    items, best = profiles.quality_items(SCHEMA, ("1080p",), False,
                                         ("webrip", "webdl"))
    group = next(i for i in items if i.get("name") == "WEB 1080p")
    assert group["allowed"] is True
    assert best == 1002


# ===========================================================================
# Profiles: box set words in a film's own title
# ===========================================================================
@pytest.mark.parametrize("name", [
    "The.Collection.2012.1080p.BluRay.x264-GRP",
    "Trilogy.of.Terror.1975.1080p.BluRay.x264-GRP",
    "The.Beatles.Anthology.1995.1080p.WEB-DL-GRP",
])
def test_a_film_whose_title_is_a_box_set_word_is_not_refused(name):
    """The word is followed by the year, which is where a title ends. Refused,
    the film could never be grabbed at all."""
    assert not re.search(profiles.COLLECTION_PATTERN, name), name


@pytest.mark.parametrize("name", [
    "Harry.Potter.Collection.German.DL.1080p.BluRay.x264-GRP",
    "Der.Herr.der.Ringe.Trilogie.Extended.German.DL.1080p-GRP",
    "The.Matrix.Trilogy.1999-2003.1080p.BluRay-GRP",
    "Transformers.2007-2018.COMPLETE.UHD.BluRay.2160p-GRP",
    "Saw.Teil.1-7.German.1080p-GRP",
])
def test_a_real_box_set_is_still_refused(name):
    assert re.search(profiles.COLLECTION_PATTERN, name), name


class FakeService:
    kind = "radarr"
    name = "Radarr"

    def __init__(self, kind="radarr", existing_profiles=()):
        self.kind = kind
        self._formats = []
        self._profiles = list(existing_profiles)
        self.saved_formats = []
        self.saved_profiles = []

    def custom_format_schema(self):
        return [{"implementation": "ReleaseTitleSpecification",
                 "implementationName": "Release Title",
                 "fields": [{"name": "value", "value": ""}]},
                {"implementation": "SizeSpecification",
                 "implementationName": "Size",
                 "fields": [{"name": "min", "value": 0},
                            {"name": "max", "value": 0}]}]

    def custom_formats(self):
        return list(self._formats)

    def quality_profile_schema(self):
        return {"items": SCHEMA}

    def profiles(self):
        return list(self._profiles)

    def languages(self):
        return [{"id": -1, "name": "Any"}]

    def save_custom_format(self, body, existing_id=None):
        saved = {**body, "id": existing_id or 100 + len(self._formats)}
        self._formats = [f for f in self._formats if f["name"] != body["name"]]
        self._formats.append(saved)
        self.saved_formats.append(body["name"])
        return saved

    def save_quality_profile(self, body, existing_id=None):
        self.saved_profiles.append((existing_id, body))
        return {**body, "id": existing_id or 7}


def test_a_series_profile_does_not_refuse_a_complete_series_pack():
    """A season or complete-series pack is how Sonarr fetches a finished
    show; the box set refusal is about films, as the rule that watches for
    box sets is."""
    service = FakeService(kind="sonarr")
    profiles.apply_to(service, profiles.build(profiles.Wish(name="Test")))
    assert "Correctarr: Box set" not in service.saved_formats

    service = FakeService(kind="radarr")
    profiles.apply_to(service, profiles.build(profiles.Wish(name="Test")))
    assert "Correctarr: Box set" in service.saved_formats


# ===========================================================================
# Profiles: a profile somebody made by hand is theirs
# ===========================================================================
def test_a_hand_made_profile_with_the_same_name_is_not_overwritten():
    theirs = {"id": 4, "name": "HD-1080p", "formatItems": [
        {"format": 1, "name": "TRaSH: Something", "score": 500}]}
    service = FakeService(existing_profiles=[theirs])
    with pytest.raises(ValueError, match="profiles.problem.name_taken"):
        profiles.apply_to(service, profiles.build(profiles.Wish(name="HD-1080p")))
    assert service.saved_profiles == []
    assert service.saved_formats == []


def test_a_profile_this_program_wrote_is_updated_in_place():
    ours = {"id": 4, "name": "Test", "formatItems": [
        {"format": 1, "name": "Correctarr: Audio good", "score": 100},
        {"format": 2, "name": "TRaSH: Something", "score": 0}]}
    service = FakeService(existing_profiles=[ours])
    result = profiles.apply_to(service, profiles.build(profiles.Wish(name="Test")))
    assert result["updated"] is True
    assert service.saved_profiles[-1][0] == 4


# ===========================================================================
# Scoring: sources and modifiers arrive as numbers
# ===========================================================================
RADARR_SOURCES = [{"value": v, "name": n} for v, n in enumerate(
    ("UNKNOWN", "CAM", "TELESYNC", "TELECINE", "WORKPRINT", "DVD", "TV",
     "WEBDL", "WEBRIP", "BLURAY"))]
SONARR_SOURCES = [{"value": v, "name": n} for v, n in enumerate(
    ("Unknown", "Television", "TelevisionRaw", "Web", "WebRip", "DVD",
     "Bluray", "BlurayRaw"))]
RADARR_MODIFIERS = [{"value": v, "name": n} for v, n in enumerate(
    ("NONE", "REGIONAL", "SCREENER", "RAWHD", "BRDISK", "REMUX"))]


def select(implementation, value, options, negate=False, required=True):
    return {"implementation": implementation, "negate": negate,
            "required": required,
            "fields": [{"name": "value", "value": value,
                        "selectOptions": options}]}


def release(source, modifier="none", title="Movie.2020.1080p-GRP"):
    return {"title": title, "group": "GRP", "gb": 5.0, "resolution": "1080",
            "source": source, "modifier": modifier, "year": 2020}


def test_a_source_condition_is_compared_by_name_not_by_number():
    """The service stores the source as a number (7 is WEB-DL in Radarr) and
    reports the source of a release as a word ("webdl"). Compared as they
    were, no source condition ever held."""
    web = {"specifications": [select("SourceSpecification", 7, RADARR_SOURCES)]}
    assert scoring.format_matches(web, release("webdl"))
    assert not scoring.format_matches(web, release("bluray"))


def test_a_negated_source_is_not_true_for_everything():
    not_web = {"specifications": [
        select("SourceSpecification", 7, RADARR_SOURCES, negate=True)]}
    assert not scoring.format_matches(not_web, release("webdl"))
    assert scoring.format_matches(not_web, release("bluray"))


def test_sonarr_sources_are_read_from_their_own_list():
    """7 is WEB-DL in Radarr and a Blu-ray remux in Sonarr."""
    raw = {"specifications": [select("SourceSpecification", 7, SONARR_SOURCES)]}
    assert scoring.format_matches(raw, release("blurayRaw"))
    assert not scoring.format_matches(raw, release("web"))


def test_a_source_number_without_its_list_is_not_guessed():
    bare = {"specifications": [{"implementation": "SourceSpecification",
                                "negate": False, "required": True,
                                "fields": [{"name": "value", "value": 7}]}]}
    assert scoring.format_matches(bare, release("webdl")) is False


def test_a_quality_modifier_is_compared_by_name():
    remux = {"specifications": [
        select("QualityModifierSpecification", 5, RADARR_MODIFIERS)]}
    assert scoring.format_matches(remux, release("bluray", "remux"))
    assert not scoring.format_matches(remux, release("bluray", "none"))


def test_a_release_without_a_group_does_not_match_a_group_by_its_title():
    """The service matches a group condition against the group and nothing
    else. Matched against the whole name, a film called *Ghost* belonged to
    the group of that name."""
    group = {"specifications": [
        {"implementation": "ReleaseGroupSpecification", "negate": False,
         "required": True,
         "fields": [{"name": "value", "value": r"(?<=^|[\s.-])Ghost\b"}]}]}
    entry = {"title": "Ghost.1990.German.DL.1080p.BluRay.x264",
             "quality": {"quality": {"resolution": 1080, "source": "bluray"}},
             "movie": {"year": 1990}}
    assert scoring.release_from_entry(entry)["group"] == ""
    assert not scoring.format_matches(group, scoring.release_from_entry(entry))


def test_web_dl_at_the_end_of_a_name_is_not_a_group():
    entry = {"title": "Movie.2020.1080p.WEB-DL", "quality": {}, "movie": {}}
    assert scoring.release_from_entry(entry)["group"] == ""


def test_a_file_extension_is_not_part_of_the_group():
    entry = {"title": "Movie.2020.1080p.BluRay.x264-GRP.mkv", "quality": {},
             "movie": {}}
    assert scoring.release_from_entry(entry)["group"] == "GRP"


def test_a_refusal_is_whatever_no_bonus_can_climb_back_over():
    """A profile built here refuses with a few thousand points, not with
    -999999. Its refusals are refusals all the same."""
    profile = {"minFormatScore": 0, "formatItems": [
        {"name": "Correctarr: Language DE", "score": 1000},
        {"name": "Correctarr: Audio good", "score": 100},
        {"name": "Correctarr: 3D", "score": -1101},
        {"name": "A small dislike", "score": -50},
        {"name": "Old style", "score": -999999}]}
    assert scoring.refused(profile) == {"Correctarr: 3D", "Old style"}


def test_below_profile_sees_a_refusal_from_a_profile_built_here():
    profile = {"id": 1, "name": "Built here", "minFormatScore": 0,
               "formatItems": [
                   {"name": "Correctarr: Language DE", "score": 1000},
                   {"name": "Correctarr: 3D", "score": -1001}]}
    formats = [{"name": "Correctarr: Language DE", "specifications": [
                   {"implementation": "ReleaseTitleSpecification",
                    "fields": [{"name": "value", "value": "German"}]}]},
               {"name": "Correctarr: 3D", "specifications": [
                   {"implementation": "ReleaseTitleSpecification",
                    "fields": [{"name": "value", "value": r"\b3D\b"}]}]}]
    item = {"id": 5, "title": "Film", "qualityProfileId": 1, "movieFile": {
        "sceneName": "Film.2020.3D.HSBS.German.1080p.BluRay-GRP",
        "quality": {"quality": {"resolution": 1080, "source": "bluray"}}}}
    found = rules.check_below_profile(
        Radarr(), {"profiles": [profile], "formats": formats, "items": [item]},
        config())
    assert len(found) == 1
    assert "3D" in found[0].params["reason"]


# ===========================================================================
# Matching
# ===========================================================================
def lib(*rows):
    return matching.build_candidates([
        {"id": n, "title": title, "year": year, **extra}
        for n, (title, year, extra) in enumerate(rows, start=1)])


def test_an_accent_in_the_title_is_not_a_gap_in_the_word():
    """"Amélie" came out as "am lie" — the accent was cut out before it was
    ever taken off the letter."""
    assert "amelie" in matching.variants("Amélie")
    found, _conf, _reason = matching.match(
        "Amelie.2001.German.1080p.BluRay.x264-GRP.mkv",
        lib(("Amélie", 2001, {}), ("Alien", 1979, {})))
    assert found and found["title"] == "Amélie"


def test_an_apostrophe_does_not_split_a_word():
    found, _conf, reason = matching.match(
        "Oceans.Eleven.2001.German.DL.1080p.BluRay.x264-GRP.mkv",
        lib(("Ocean's Eleven", 2001, {}), ("Ocean's Twelve", 2004, {})))
    assert found and found["title"] == "Ocean's Eleven", reason


@pytest.mark.parametrize("file_name,title,year", [
    ("1917.2019.German.DL.1080p.BluRay.x264-GRP.mkv", "1917", 2019),
    ("2012.2009.German.DL.1080p.BluRay.x264-GRP.mkv", "2012", 2009),
    ("2001.A.Space.Odyssey.1968.German.1080p.BluRay-GRP.mkv",
     "2001: A Space Odyssey", 1968),
])
def test_a_title_that_is_a_number_can_be_matched(file_name, title, year):
    """The number at the front of the name was taken for the release year,
    so nothing was cut off and the whole name was compared with the title."""
    candidates = lib((title, year, {}), ("Crank 2: High Voltage", 2009, {}),
                     ("Saw", 2004, {}))
    found, _conf, reason = matching.match(file_name, candidates)
    assert found and found["title"] == title, reason


def test_a_number_left_over_from_a_foreign_script_is_still_rejected():
    """The guard for "Хроники Нарнии 1" stays: a number is only a title when
    the title is nothing but that number."""
    assert matching.variants("Хроники Нарнии 1") == set()


# ===========================================================================
# Box sets: the item's own title, however it is written
# ===========================================================================
@pytest.mark.parametrize("release,item", [
    ("The.Twilight.Saga.New.Moon.2009.German.DL.1080p.BluRay.x264-GRP",
     {"title": "The Twilight Saga: New Moon"}),
    ("Twilight.Saga.New.Moon.2009.German.DL.1080p.BluRay.x264-GRP",
     {"title": "The Twilight Saga: New Moon"}),
    ("The.Twilight.Saga.New.Moon.2009.German.DL.1080p.BluRay.x264-GRP",
     {"title": "New Moon - Biss zur Mittagsstunde",
      "originalTitle": "The Twilight Saga: New Moon"}),
    ("The.Collection.2012.German.DL.1080p.BluRay.x264-GRP",
     {"title": "The Collection"}),
    ("Star.Wars.Die.Saga.Geht.Weiter.2020.German.1080p-GRP",
     {"title": "Star Wars - Die Saga geht weiter",
      "alternateTitles": [{"title": "Star Wars: Die Saga geht weiter"}]}),
])
def test_a_film_with_a_box_set_word_in_its_own_title_is_not_a_box_set(release, item):
    """Colons, dashes, a dropped article, a localised title: the title was
    only taken out when the release spelled it exactly as the service did.
    Every release of *The Twilight Saga: New Moon* was a box set, and the
    default action blocklists a box set and searches again."""
    verdict = packs.judge(release, item)
    assert not verdict.is_pack, verdict


def test_a_complete_edition_of_one_film_is_not_a_box_set():
    verdict = packs.judge("Movie.2020.Remastered.Complete.Edition.1080p.BluRay-GRP",
                          {"title": "Movie"})
    assert not verdict.is_pack


def test_a_saga_box_set_is_still_one():
    verdict = packs.judge("The.Twilight.Saga.2008-2012.German.DL.1080p.BluRay-GRP",
                          {"title": "Twilight"})
    assert verdict.is_pack


# ===========================================================================
# Indexers: a ranking nobody can act on
# ===========================================================================
def _view(name, priority, rating):
    view = indexers.IndexerView(name=name, priority=priority, enabled=True)
    view.rating = rating
    return view


def test_indexers_at_the_same_priority_are_not_out_of_order():
    """Every indexer at the default priority is the most common setup there
    is. Sorted by an arbitrary tie-break, they were "out of place", and the
    suggestion was to set 25 to 25."""
    views = [_view(f"Indexer {n}", 25, rating)
             for n, rating in enumerate((10.0, 20.0, 90.0, 80.0, 50.0))]
    assert indexers.rank_deviation(views) == []


def test_a_suggestion_always_changes_the_priority():
    views = [_view("A", 1, 10.0), _view("B", 2, 30.0), _view("C", 3, 50.0),
             _view("D", 4, 95.0)]
    found = indexers.rank_deviation(views)
    assert found
    for row in found:
        assert row["suggested_priority"] != row["actual_priority"]


# ===========================================================================
# Library: audio languages as the service writes them
# ===========================================================================
def _film(languages, item_id=1):
    return {"id": item_id, "title": "Film", "movieFile": {
        "relativePath": "Film.mkv",
        "mediaInfo": {"audioLanguages": languages}}}


@pytest.mark.parametrize("found,wanted", [
    ("ger/eng", "German"),
    ("deu/eng", "German"),
    ("ger", "de"),
    ("German / English", "German"),
    ("eng/ger", "Deutsch"),
    ("ger/eng", "ger"),
    ("fre/eng", "French"),
])
def test_a_file_with_the_wanted_language_is_not_reported(found, wanted):
    """Radarr writes the tracks as three-letter codes, "ger/eng". The setting
    asks for "German, English". Compared as text, every German file in the
    library lacked German."""
    assert rules.check_missing_audio_language(
        Radarr(), {"items": [_film(found)]},
        config(audio_languages=wanted)) == []


@pytest.mark.parametrize("found,wanted", [
    ("eng", "German"),
    ("French", "en"),              # "en" is inside "french" as text
    ("spa/fre", "English"),
])
def test_a_file_without_the_wanted_language_is_reported(found, wanted):
    assert len(rules.check_missing_audio_language(
        Radarr(), {"items": [_film(found)]},
        config(audio_languages=wanted))) == 1


def test_no_file_is_called_damaged_when_the_service_reads_no_files_at_all():
    """Analysing video files can be switched off. Then no file has media
    information, and every one of them was reported as unreadable."""
    items = [{"id": n, "title": f"Film {n}", "movieFile": {"relativePath": "a.mkv"}}
             for n in range(1, 4)]
    assert rules.check_unreadable_file(Radarr(), {"items": items}, config()) == []


def test_one_file_without_media_information_among_read_ones_is_still_reported():
    items = [{"id": 1, "title": "Read", "movieFile": {
                 "relativePath": "a.mkv", "mediaInfo": {"audioLanguages": "eng"}}},
             {"id": 2, "title": "Unread", "movieFile": {"relativePath": "b.mkv"}}]
    found = rules.check_unreadable_file(Radarr(), {"items": items}, config())
    assert [f.title for f in found] == ["Unread"]


def test_an_undetermined_track_is_no_evidence_either_way():
    assert rules.check_missing_audio_language(
        Radarr(), {"items": [_film("und")]},
        config(audio_languages="German")) == []


# ===========================================================================
# Library: searches that have been answered
# ===========================================================================
@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "s.db")


def _settle(store, key):
    with store._conn() as c:
        c.execute("INSERT INTO attempts(key,tries,first_try,last_try) "
                  "VALUES(?,3,'2026-01-01T00:00:00+00:00',"
                  "'2026-01-03T00:00:00+00:00')", (key,))


def test_a_season_gap_searched_for_often_enough_is_not_searched_again(store):
    """The search is counted for every rule, and only two rules ever read the
    count back. A gap nobody has a copy of was searched for on every full
    pass, for good."""
    _settle(store, rules.searching_for(Sonarr(), "season_gaps", 3, 1))
    series = {"id": 3, "title": "Show", "monitored": True, "seasons": [
        {"seasonNumber": 1, "monitored": True,
         "statistics": {"episodeFileCount": 8, "episodeCount": 10}}]}
    found = rules.check_season_gaps(Sonarr(), {"items": [series], "store": store},
                                    config())
    assert len(found) == 1
    assert found[0].data.get("settled") is True


def test_an_incomplete_series_searched_for_often_enough_is_not_searched_again(store):
    _settle(store, rules.searching_for(Sonarr(), "series_incomplete", 3))
    series = {"id": 3, "title": "Show", "monitored": True, "ended": True,
              "statistics": {"episodeFileCount": 8, "episodeCount": 10}}
    found = rules.check_series_incomplete(
        Sonarr(), {"items": [series], "store": store}, config())
    assert found[0].data.get("settled") is True


def test_a_missing_audio_language_searched_for_often_enough_is_left_alone(store):
    _settle(store, rules.searching_for(Radarr(), "missing_audio_language", 1))
    found = rules.check_missing_audio_language(
        Radarr(), {"items": [_film("eng")], "store": store},
        config(audio_languages="German"))
    assert found[0].data.get("settled") is True
