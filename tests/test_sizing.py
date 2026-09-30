"""Tests for the size estimates and the storage outlook.

The shapes below are the ones Radarr 6 and Sonarr 4 actually send, trimmed to
the fields that matter: a film carries its file inline, a series carries only
statistics and its episode files are a request of their own. The quality
definitions are the services' untouched defaults, which say nothing — that is
the case the module has to get right first.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app import profiles, sizing
from app.api import core, jobs, library

GB = sizing.GB
MB = sizing.MB
NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)


def definition(name, minimum=0, preferred=1990, maximum=2000):
    return {"quality": {"name": name}, "minSize": minimum,
            "preferredSize": preferred, "maxSize": maximum}


RADARR_DEFAULTS = [definition(n, m) for n, m in (
    ("HDTV-720p", 12), ("WEBDL-720p", 12), ("Bluray-720p", 12),
    ("WEBDL-1080p", 21), ("WEBRip-1080p", 21), ("Bluray-1080p", 21),
    ("Remux-1080p", 21), ("WEBDL-2160p", 40), ("Bluray-2160p", 40),
    ("Remux-2160p", 40), ("SDTV", 0), ("DVD", 0))]
SONARR_DEFAULTS = [definition(n, 0, 990, 1000) for n in (
    "HDTV-720p", "WEBDL-1080p", "Bluray-1080p", "Bluray-1080p Remux",
    "WEBDL-2160p")]


def movie_file(quality, mb_per_min, minutes, runtime_text=None):
    return {"size": int(mb_per_min * minutes * MB),
            "quality": {"quality": {"name": quality}},
            "mediaInfo": {"runTime": runtime_text} if runtime_text else {}}


def movie(movie_id, *, quality=None, rate=60.0, runtime=100, profile=1,
          monitored=True, root="/movies"):
    item = {"id": movie_id, "title": f"Film {movie_id}", "year": 2000 + movie_id,
            "runtime": runtime, "qualityProfileId": profile,
            "monitored": monitored, "hasFile": quality is not None,
            "rootFolderPath": root, "path": f"{root}/Film {movie_id}"}
    if quality:
        item["movieFile"] = movie_file(quality, rate, runtime or 100)
        item["sizeOnDisk"] = item["movieFile"]["size"]
    return item


def reference() -> dict[str, sizing.Rate]:
    return sizing.rates({}, {})


# ---------------------------------------------------------------------------
# What the service says, and when it says nothing
# ---------------------------------------------------------------------------
def test_the_default_sizes_count_as_not_set():
    """Taken at its word, a two hour WEB-DL at Radarr's default is 233 GB."""
    for raw, kind in ((RADARR_DEFAULTS, "radarr"), (SONARR_DEFAULTS, "sonarr")):
        defs = sizing.definitions(raw, kind)
        assert all(d.preferred_mb is None and d.max_mb is None
                   for d in defs.values())
    assert sizing.definitions(RADARR_DEFAULTS, "radarr")["webdl-1080p"].min_mb == 21


def test_a_size_somebody_set_is_used():
    raw = [definition("WEBDL-1080p", 10, 60, 120)]
    defs = sizing.definitions(raw, "radarr")
    assert defs["webdl-1080p"].preferred_mb == 60
    assert defs["webdl-1080p"].max_mb == 120
    rate = sizing.rates(defs, {})["webdl-1080p"]
    assert rate.basis == "service"
    assert rate.typical == 60


def test_the_two_services_name_a_remux_differently_and_mean_the_same():
    defs = sizing.definitions(SONARR_DEFAULTS, "sonarr")
    assert "remux-1080p" in defs
    assert "sdtv" not in str(sorted(sizing.definitions(RADARR_DEFAULTS, "radarr")))


def test_with_nothing_measured_the_reference_is_used():
    rate = reference()["webdl-1080p"]
    assert rate.basis == "reference"
    assert rate.typical == sizing.REFERENCE["webdl-1080p"]
    assert rate.low < rate.typical < rate.high


def test_files_at_a_quality_replace_the_reference():
    samples = {"bluray-1080p": [50.0, 55.0, 60.0, 65.0, 70.0]}
    rate = sizing.rates({}, samples)["bluray-1080p"]
    assert rate.basis == "measured"
    assert rate.samples == 5
    assert rate.typical == 60.0
    assert (rate.low, rate.high) == (55.0, 65.0)


def test_a_handful_of_files_is_not_a_measurement():
    rate = sizing.rates({}, {"bluray-1080p": [500.0] * 4})["bluray-1080p"]
    assert rate.basis == "reference"


def test_what_is_measured_at_one_quality_moves_the_others_at_that_resolution():
    """This library's WEB-DLs weigh twice the reference, so its WEBRips are
    not likely to weigh the reference either."""
    samples = {"webdl-1080p": [sizing.REFERENCE["webdl-1080p"] * 2] * 6}
    table = sizing.rates({}, samples)
    assert table["webrip-1080p"].basis == "scaled"
    assert table["webrip-1080p"].typical == pytest.approx(
        sizing.REFERENCE["webrip-1080p"] * 2)
    assert table["webdl-2160p"].basis == "reference"


def test_nothing_outside_the_services_own_window_can_arrive():
    defs = sizing.definitions([definition("Bluray-1080p", 30, 1990, 50)], "radarr")
    rate = sizing.rates(defs, {"bluray-1080p": [10.0, 20.0, 60.0, 80.0, 90.0]})[
        "bluray-1080p"]
    assert rate.low >= 30
    assert rate.high <= 50


# ---------------------------------------------------------------------------
# One title
# ---------------------------------------------------------------------------
def test_a_film_is_its_minutes_times_what_a_minute_weighs():
    table = reference()
    each = sizing.estimate(120, ["webdl-1080p", "bluray-1080p"], table)
    assert each.typical == pytest.approx(
        sizing.REFERENCE["bluray-1080p"] * 120 / 1024)
    # Low is the lowest rung, when nothing better is on offer.
    assert each.low == pytest.approx(table["webdl-1080p"].low * 120 / 1024)
    assert each.quality == "bluray-1080p"
    assert each.capped is False


def test_a_size_limit_caps_the_estimate_and_says_so():
    each = sizing.estimate(120, ["bluray-1080p"], reference(),
                           {"bluray-1080p": (0.0, 5.0)})
    assert each.typical == 5.0
    assert each.high == 5.0
    assert each.capped is True


def test_a_size_floor_raises_the_estimate():
    each = sizing.estimate(45, ["webdl-720p"], reference(),
                           {"webdl-720p": (2.0, 0.0)})
    assert each.low == 2.0
    assert each.typical == 2.0


def test_nothing_to_go_on_is_no_estimate():
    assert sizing.estimate(120, [], reference()) is None
    assert sizing.estimate(0, ["webdl-1080p"], reference()) is None
    assert sizing.estimate(120, ["dvd-480p"], reference()) is None


def test_what_a_profile_already_holds_beats_its_ladder():
    """A profile that allows remux and holds none: the ladder says 25 GB a
    film, the files say six."""
    titles = [sizing.Title(id=n, title="", year=None, profile_id=1, minutes=100,
                           runtime_known=True, units=1, files=1,
                           size=int(55 * 100 * MB), monitored=True, root="/m")
              for n in range(6)]
    own = sizing.profile_rate(titles)
    assert own.basis == "profile"
    assert own.typical == pytest.approx(55)
    each = sizing.estimate(100, ["bluray-1080p", "remux-1080p"], reference(),
                           own=own)
    assert each.typical == pytest.approx(55 * 100 / 1024)
    assert sizing.profile_rate(titles[:4]) is None


def test_the_limits_of_a_wish_are_per_resolution():
    wish = profiles.Wish(resolutions=("1080p", "2160p"), max_gb=80,
                         size_limits=(("1080p", 0, 15),)).tidy()
    keys = profiles.ladder_keys(wish)
    limits = sizing.wish_limits(wish, keys)
    assert limits["bluray-1080p"] == (0, 15)
    assert limits["bluray-2160p"] == (0, 80)


# ---------------------------------------------------------------------------
# An existing profile
# ---------------------------------------------------------------------------
PROFILE = {
    "id": 1, "name": "HD", "cutoff": 1000, "upgradeAllowed": True,
    "minFormatScore": 0,
    "items": [
        {"quality": {"id": 1, "name": "SDTV"}, "items": [], "allowed": True},
        {"quality": {"id": 4, "name": "HDTV-720p"}, "items": [], "allowed": False},
        {"id": 1000, "name": "WEB 1080p", "allowed": True, "items": [
            {"quality": {"id": 15, "name": "WEBRip-1080p"}, "items": [], "allowed": True},
            {"quality": {"id": 3, "name": "WEBDL-1080p"}, "items": [], "allowed": True}]},
        {"quality": {"id": 7, "name": "Bluray-1080p"}, "items": [], "allowed": True},
    ],
    "formatItems": [{"format": 50, "name": "Too big", "score": -10000},
                    {"format": 51, "name": "Nice", "score": 100},
                    {"format": 52, "name": "Slightly big", "score": -10}],
}
FORMATS = [
    {"id": 50, "name": "Too big", "specifications": [
        {"implementation": "SizeSpecification", "negate": False, "fields": [
            {"name": "min", "value": 20}, {"name": "max", "value": 2000}]}]},
    {"id": 51, "name": "Nice", "specifications": []},
    {"id": 52, "name": "Slightly big", "specifications": [
        {"implementation": "SizeSpecification", "negate": False, "fields": [
            {"name": "min", "value": 10}, {"name": "max", "value": 2000}]}]},
    {"id": 53, "name": "Big 2160p", "specifications": [
        {"implementation": "ResolutionSpecification", "negate": False,
         "fields": [{"name": "value", "value": 2160}]},
        {"implementation": "SizeSpecification", "negate": False, "fields": [
            {"name": "min", "value": 60}, {"name": "max", "value": 2000}]}]},
]


def test_the_ladder_of_a_profile_is_read_in_the_services_order():
    keys, cutoff = sizing.profile_keys(PROFILE)
    # SDTV has no rate and is left out; the members of a group keep the
    # usual order among themselves; the cutoff is the group's first member.
    assert keys == ["webrip-1080p", "webdl-1080p", "bluray-1080p"]
    assert cutoff == "webrip-1080p"


def test_a_cutoff_on_a_single_quality_is_that_quality():
    keys, cutoff = sizing.profile_keys({**PROFILE, "cutoff": 7})
    assert cutoff == "bluray-1080p"


def test_only_a_size_that_refuses_is_a_limit():
    """Ten points less decides between two releases; it keeps neither out."""
    keys, _ = sizing.profile_keys(PROFILE)
    limits = sizing.profile_limits(PROFILE, FORMATS, keys)
    assert limits["bluray-1080p"] == (0.0, 20.0)


def test_a_size_limit_for_one_resolution_stays_there():
    profile = {**PROFILE, "formatItems": [
        {"format": 53, "name": "Big 2160p", "score": -10000}]}
    limits = sizing.profile_limits(profile, FORMATS,
                                   ["bluray-1080p", "bluray-2160p"])
    assert limits["bluray-1080p"] == (0.0, 0.0)
    assert limits["bluray-2160p"] == (0.0, 60.0)


def test_a_profile_built_here_is_read_back_the_way_it_was_meant():
    wish = profiles.Wish(resolutions=("1080p", "2160p"), languages=("de",),
                         size_limits=(("2160p", 15, 60),)).tidy()
    plan = profiles.build(wish)
    formats, items = [], []
    for number, entry in enumerate(plan.formats, start=1):
        formats.append({"id": number, "name": entry.full_name, "specifications": [
            {"implementation": impl, "negate": negate,
             "fields": [{"name": k, "value": v} for k, v in values.items()]}
            for impl, values, negate, _required in entry.conditions]})
        items.append({"format": number, "name": entry.full_name,
                      "score": entry.score})
    profile = {"minFormatScore": plan.min_score, "formatItems": items}
    limits = sizing.profile_limits(profile, formats,
                                   ["bluray-1080p", "bluray-2160p"])
    assert limits == {"bluray-1080p": (0.0, 0.0), "bluray-2160p": (15.0, 60.0)}


# ---------------------------------------------------------------------------
# The library
# ---------------------------------------------------------------------------
def test_a_film_library_is_read_with_its_files():
    items = [movie(1, quality="Bluray-1080p", rate=60),
             movie(2, quality="WEBDL-1080p", rate=40, runtime=0),
             movie(3)]
    titles = sizing.titles("radarr", items)
    assert [t.files for t in titles] == [1, 1, 0]
    assert titles[0].file_qualities == [("bluray-1080p", titles[0].size)]
    # No runtime: counted at the library's usual length, and flagged.
    assert titles[1].runtime_known is False
    assert titles[1].minutes == 100
    assert titles[2].file_qualities is None


def test_the_length_of_a_file_is_its_own_where_it_is_known():
    """The metadata runtime and the file differ by the credits."""
    item = movie(1, runtime=100)
    item["movieFile"] = movie_file("Bluray-1080p", 60, 120, "2:00:00")
    measured = sizing.samples("radarr", [item])
    assert measured["bluray-1080p"] == [pytest.approx(60)]


def test_a_sample_or_an_extra_is_not_measured():
    item = movie(1, runtime=100)
    item["movieFile"] = movie_file("Bluray-1080p", 60, 2, "0:02:00")
    assert sizing.samples("radarr", [item]) == {}


def test_a_series_library_is_read_from_its_statistics_and_its_episodes():
    series = [
        {"id": 1, "title": "Show", "runtime": 45, "monitored": True,
         "qualityProfileId": 2, "path": "/tv/Show",
         "statistics": {"episodeFileCount": 2, "episodeCount": 3,
                        "sizeOnDisk": 2 * GB}},
        {"id": 2, "title": "Other", "runtime": 0, "monitored": True,
         "qualityProfileId": 2, "path": "/tv/Other",
         "statistics": {"episodeFileCount": 1, "episodeCount": 1,
                        "sizeOnDisk": GB}},
    ]
    files = {1: [{"seriesId": 1, "size": GB,
                  "quality": {"quality": {"name": "WEBDL-1080p"}},
                  "mediaInfo": {"runTime": "44:00"}}] * 2}
    titles = sizing.titles("sonarr", series, files)
    assert titles[0].units == 3
    assert titles[0].file_qualities == [("webdl-1080p", GB)] * 2
    # Not read: nothing is known about its files one by one.
    assert titles[1].file_qualities is None
    assert titles[1].runtime_known is False
    measured = sizing.samples("sonarr", series, files)
    assert measured["webdl-1080p"][0] == pytest.approx(1024 / 44)


def test_a_spread_of_series_is_read_rather_than_the_first_few():
    series = [{"id": n, "statistics": {"episodeFileCount": 1}} for n in range(1, 201)]
    series.append({"id": 999, "statistics": {"episodeFileCount": 0}})
    chosen = sizing.series_to_read(series, limit=10)
    assert len(chosen) == 10
    assert chosen[0] == 1 and chosen[-1] > 150
    assert 999 not in sizing.series_to_read(series)
    assert sizing.series_to_read(series[:5], limit=10) == [1, 2, 3, 4, 5]


@pytest.mark.parametrize("text,minutes", [
    ("1:49:23", 109 + 23 / 60), ("55:17", 55 + 17 / 60), ("", None),
    (None, None), ("abc", None)])
def test_a_runtime_is_read_the_way_media_analysis_writes_it(text, minutes):
    result = sizing.runtime_minutes(text)
    assert result == (pytest.approx(minutes) if minutes else None)


def _library(items, keys, cutoff=None, upgrade=True, **kwargs):
    return sizing.library(sizing.titles("radarr", items), keys, cutoff,
                          reference(), None, upgrade=upgrade, **kwargs)


def test_a_file_the_profile_would_keep_keeps_its_own_size():
    result = _library([movie(1, quality="Bluray-1080p", rate=50)],
                      ["webdl-1080p", "bluray-1080p"])
    assert result["counts"]["kept"] == 1
    assert result["expected"]["typical_gb"] == result["current_gb"]
    assert result["change"]["typical_gb"] == 0


def test_a_file_below_the_cutoff_is_replaced():
    result = _library([movie(1, quality="WEBDL-1080p", rate=40)],
                      ["webdl-1080p", "bluray-1080p"])
    assert result["counts"]["replaced"] == 1
    assert result["change"]["typical_gb"] > 0


def test_a_file_the_ladder_does_not_take_is_replaced_even_downwards():
    """The services rank a quality outside the ladder below all of it: moving
    a 2160p film to a 720p profile fetches the 720p."""
    result = _library([movie(1, quality="Bluray-2160p", rate=250)],
                      ["webdl-720p", "bluray-720p"])
    assert result["counts"]["replaced"] == 1
    assert result["change"]["typical_gb"] < 0


def test_nothing_is_replaced_when_upgrades_are_off():
    result = _library([movie(1, quality="WEBDL-1080p", rate=40)],
                      ["webdl-1080p", "bluray-1080p"], upgrade=False)
    assert result["counts"]["kept"] == 1


def test_a_stop_below_the_top_keeps_what_reached_it():
    result = _library([movie(1, quality="WEBDL-1080p", rate=40)],
                      ["webdl-1080p", "bluray-1080p"], cutoff="webdl-1080p")
    assert result["counts"]["kept"] == 1


def test_an_unmonitored_title_is_neither_fetched_nor_upgraded():
    result = _library([movie(1, monitored=False),
                       movie(2, quality="WEBDL-1080p", monitored=False)],
                      ["webdl-1080p", "bluray-1080p"])
    assert result["counts"]["skipped"] == 1
    assert result["counts"]["kept"] == 1
    assert result["change"]["typical_gb"] == 0


def test_a_missing_film_is_fetched_and_counted():
    result = _library([movie(1, runtime=120)], ["bluray-1080p"], detail=True)
    assert result["counts"]["fetched"] == 1
    assert result["expected"]["typical_gb"] == pytest.approx(
        sizing.REFERENCE["bluray-1080p"] * 120 / 1024, abs=0.1)
    assert result["rows"][0]["outcome"] == "fetched"


def test_a_series_that_was_not_read_is_assumed_to_stay():
    series = [{"id": 1, "title": "Show", "runtime": 45, "monitored": True,
               "statistics": {"episodeFileCount": 10, "episodeCount": 12,
                              "sizeOnDisk": 20 * GB}}]
    titles = sizing.titles("sonarr", series)
    result = sizing.library(titles, ["webdl-1080p"], None, reference(), None,
                            upgrade=True)
    assert result["counts"]["fetched"] == 1       # two episodes missing
    assert result["expected"]["typical_gb"] == pytest.approx(
        20 + 2 * sizing.REFERENCE["webdl-1080p"] * 45 / 1024, abs=0.1)
    titles[0].units = 10
    result = sizing.library(titles, ["webdl-1080p"], None, reference(), None,
                            upgrade=True)
    assert result["counts"]["assumed_kept"] == 1


@pytest.mark.parametrize("change,free,verdict", [
    ({"typical_gb": 100, "high_gb": 150}, 200, "yes"),
    ({"typical_gb": 100, "high_gb": 250}, 200, "tight"),
    ({"typical_gb": 300, "high_gb": 400}, 200, "no"),
    ({"typical_gb": 1, "high_gb": 1}, None, "unknown")])
def test_whether_it_fits(change, free, verdict):
    assert sizing.fits(change, free) == verdict


# ---------------------------------------------------------------------------
# Disks
# ---------------------------------------------------------------------------
DISK_RADARR = [{"path": "/", "freeSpace": 100 * GB, "totalSpace": 200 * GB},
               {"path": "/movies", "freeSpace": 2500 * GB, "totalSpace": 8000 * GB}]
DISK_SONARR = [{"path": "/tv", "freeSpace": 2500 * GB - 55 * MB,
                "totalSpace": 8000 * GB}]


def test_a_root_folder_takes_its_total_from_the_disk_it_is_on():
    roots = sizing.root_folders([{"path": "/movies", "freeSpace": 2500 * GB}],
                                DISK_RADARR)
    assert roots[0]["total_gb"] == 8000
    assert roots[0]["free_gb"] == 2500


def test_two_services_on_one_pool_are_one_disk():
    """Seen as /movies from one container and /tv from the other, with free
    space a few megabytes apart — what was written between two requests."""
    radarr = sizing.root_folders([{"path": "/movies", "freeSpace": 2500 * GB}],
                                 DISK_RADARR)
    sonarr = sizing.root_folders([{"path": "/tv", "freeSpace": 2500 * GB - 55 * MB}],
                                 DISK_SONARR)
    assert sizing.same_disk(radarr[0], sonarr[0])
    assert len(sizing.disks(radarr + sonarr)) == 1
    other = sizing.root_folders([{"path": "/x", "freeSpace": 100 * GB}],
                                [{"path": "/x", "freeSpace": 100 * GB,
                                  "totalSpace": 8000 * GB}])
    assert not sizing.same_disk(radarr[0], other[0])


def test_free_space_counts_each_disk_once():
    roots = sizing.root_folders(
        [{"path": "/movies", "freeSpace": 2500 * GB},
         {"path": "/movies2", "freeSpace": 2500 * GB}],
        [{"path": "/movies", "freeSpace": 2500 * GB, "totalSpace": 8000 * GB},
         {"path": "/movies2", "freeSpace": 2500 * GB, "totalSpace": 8000 * GB}])
    titles = sizing.titles("radarr", [movie(1, root="/movies"),
                                      movie(2, root="/movies2")])
    assert sizing.free_space(roots, titles) == 2500


# ---------------------------------------------------------------------------
# How fast
# ---------------------------------------------------------------------------
def event(kind, days_ago, size_gb, **extra):
    return {"eventType": kind, "date": (NOW - timedelta(days=days_ago)).isoformat(),
            "data": {"size": str(int(size_gb * GB)), **extra.pop("data", {})},
            **extra}


def test_growth_is_imports_minus_what_they_replaced():
    history = [event("downloadFolderImported", d, 10,
                     data={"importedPath": f"/movies/F{d}/f.mkv"})
               for d in range(1, 61, 6)]
    history += [event("movieFileDeleted", 5, 4, movieId=1)]
    rows = sizing.growth(history, ["/movies"], now=NOW,
                         items=[movie(1, root="/movies")])
    row = rows["/movies"]
    assert row["imports"] == 10
    assert row["imported_gb"] == 100
    assert row["removed_gb"] == 4
    assert row["known"] is True
    assert row["gb_per_month"] == pytest.approx(96 / row["days"] * 30, abs=0.1)


def test_a_renamed_episode_is_not_a_deleted_one():
    """Sonarr's 6 is "renamed" and Radarr's is "deleted"; by name only."""
    history = [event("downloadFolderImported", d, 2,
                     data={"importedPath": "/tv/S/e.mkv"}) for d in range(1, 30, 2)]
    history += [event("episodeFileRenamed", 3, 2, seriesId=1),
                {**event("x", 3, 2, seriesId=1), "eventType": 6}]
    row = sizing.growth(history, ["/tv"], now=NOW)["/tv"]
    assert row["removed_gb"] == 0


def test_a_short_history_says_nothing_about_months():
    history = [event("downloadFolderImported", d, 10) for d in range(1, 6)]
    row = sizing.growth(history, ["/movies"], now=NOW)["/movies"]
    assert row["known"] is False
    assert row["gb_per_month"] is None
    assert row["reason"] == "storage.reason.short_history"


def test_a_long_history_with_few_imports_says_nothing_either():
    history = [event("grabbed", 80, 0), event("downloadFolderImported", 2, 10)]
    row = sizing.growth(history, ["/movies"], now=NOW)["/movies"]
    assert row["reason"] == "storage.reason.few_imports"


def test_the_window_is_never_longer_than_asked():
    history = [event("downloadFolderImported", d, 1) for d in range(1, 300, 10)]
    row = sizing.growth(history, ["/movies"], now=NOW)["/movies"]
    assert row["days"] <= sizing.GROWTH_WINDOW_DAYS
    assert row["imports"] == 9


def test_several_root_folders_of_one_service_add_up():
    rows = [{"days": 60, "imports": 3, "imported_gb": 30, "removed_gb": 0},
            {"days": 60, "imports": 4, "imported_gb": 40, "removed_gb": 10}]
    combined = sizing.combined(rows)
    assert combined["imports"] == 7
    assert combined["gb_per_month"] == pytest.approx(30)


@pytest.mark.parametrize("free,pace,outlook,months", [
    (1000, 100, "months", 10.0), (1000, 0, "steady", None),
    (1000, -50, "shrinking", None), (1000, None, "unknown", None),
    (None, 100, "unknown", None)])
def test_the_outlook(free, pace, outlook, months):
    result = sizing.outlook(free, pace, NOW)
    assert result["outlook"] == outlook
    assert result["months_left"] == months
    if months:
        assert result["full_by"] == (NOW + timedelta(days=300)).date().isoformat()


# ---------------------------------------------------------------------------
# What is there
# ---------------------------------------------------------------------------
def test_the_library_is_broken_down_by_resolution_and_quality():
    items = [movie(n, quality="Bluray-1080p", rate=60 + n) for n in range(1, 13)]
    items += [movie(20, quality="Remux-2160p", rate=500), movie(21, quality="DVD"),
              movie(22)]
    result = sizing.breakdown(sizing.titles("radarr", items))
    assert result["titles"] == 15
    assert result["with_files"] == 14
    assert [r["resolution"] for r in result["by_resolution"]] == [
        "other", "1080p", "2160p"]
    assert {r["quality"]: r["files"] for r in result["by_quality"]} == {
        "other": 1, "bluray-1080p": 12, "remux-2160p": 1}
    assert len(result["largest"]) == 10
    assert result["largest"][0]["id"] == 20
    assert result["largest"][0]["quality"] == "remux-2160p"


# ---------------------------------------------------------------------------
# Through the interface
# ---------------------------------------------------------------------------
PASSPHRASE = "a-proper-test-passphrase"


class FakeArr:
    """A Radarr and a Sonarr that answer from fixed data and write nothing."""

    moved: list = []

    def __init__(self, kind, url, api_key, timeout=30.0, name="", service_id=None,
                 verify=True):
        self.kind, self.name, self.service_id = kind, name or kind, service_id

    def close(self):
        pass

    def items(self):
        if self.kind == "radarr":
            return [movie(n, quality="Bluray-1080p", rate=55) for n in range(1, 7)] + [
                movie(7, quality="WEBDL-1080p", rate=40), movie(8),
                movie(9, profile=2, quality="HDTV-720p", rate=20)]
        return [{"id": 1, "title": "Show", "runtime": 45, "monitored": True,
                 "qualityProfileId": 1, "path": "/tv/Show",
                 "statistics": {"episodeFileCount": 2, "episodeCount": 4,
                                "sizeOnDisk": 2 * GB}}]

    def files(self, ids):
        return [{"seriesId": 1, "size": GB,
                 "quality": {"quality": {"name": "WEBDL-1080p"}}}] * 2

    def quality_definitions(self):
        return RADARR_DEFAULTS if self.kind == "radarr" else SONARR_DEFAULTS

    def profiles(self):
        return [PROFILE, {**PROFILE, "id": 2, "name": "Loops",
                          "cutoffFormatScore": 50000, "formatItems": []}]

    def custom_formats(self):
        return FORMATS

    def root_folders(self):
        path = "/movies" if self.kind == "radarr" else "/tv"
        return [{"path": path, "freeSpace": 2500 * GB, "accessible": True}]

    def disk_space(self):
        return DISK_RADARR if self.kind == "radarr" else DISK_SONARR

    def history(self, page_size=200):
        return []

    def history_since(self, since):
        root = "/movies" if self.kind == "radarr" else "/tv"
        return [event("downloadFolderImported", d, 10,
                      data={"importedPath": f"{root}/x/f.mkv"})
                for d in range(1, 60, 5)]

    def set_quality_profile(self, ids, profile_id):
        FakeArr.moved.append((self.kind, list(ids), profile_id))


@pytest.fixture
def api(tmp_path, monkeypatch):
    from app.engine import Engine
    from app.storage import Store

    store = Store(tmp_path / "api.db")
    engine = Engine(store)
    monkeypatch.setattr(core, "store", store)
    monkeypatch.setattr(core, "engine", engine)
    monkeypatch.setattr(jobs.scheduler, "start", lambda *a, **k: None)
    monkeypatch.setattr(jobs.scheduler, "shutdown", lambda *a, **k: None)
    monkeypatch.setattr(jobs.scheduler, "get_jobs", lambda: [])
    monkeypatch.setattr(jobs, "schedule", lambda: None)
    monkeypatch.setattr(library, "Arr", FakeArr)
    library.forget_libraries()
    FakeArr.moved = []
    for kind in ("radarr", "sonarr"):
        store.save_service({"name": kind.capitalize(), "kind": kind,
                            "url": f"http://{kind}:1", "api_key": "k" * 32,
                            "enabled": True, "webhook": False})
    with TestClient(main_module.app) as client:
        response = client.post("/api/auth/setup",
                               json={"name": "tester", "password": PASSPHRASE})
        assert response.status_code == 200, response.text
        yield client
    library.forget_libraries()


def _service_id(api, kind):
    return next(s["id"] for s in api.get("/api/profiles/options").json()["services"]
                if s["kind"] == kind)


def test_the_presets_come_with_their_whole_wish_and_a_size(api):
    body = api.get("/api/profiles/presets?languages=de,en").json()
    assert body["basis"] == "reference"
    ids = [p["id"] for p in body["presets"]]
    assert ids == [p.id for p in profiles.PRESETS]
    first = body["presets"][0]
    assert first["name"] and first["description"]
    assert first["wish"]["languages"] == ["de", "en"]
    assert first["problems"] == []
    example = first["examples"][0]
    assert example["unit"] == "film" and example["minutes"] == 120
    assert example["basis"] == "reference"
    anime = next(p for p in body["presets"] if p["id"] == "anime_1080p")
    assert {e["unit"]: e["minutes"] for e in anime["examples"]} == {
        "episode": 24, "film": 120}


def test_the_presets_can_be_measured_against_a_library(api):
    radarr = _service_id(api, "radarr")
    body = api.get(f"/api/profiles/presets?service_id={radarr}").json()
    assert body["basis"] == "library"
    balanced = next(p for p in body["presets"] if p["id"] == "film_1080p")
    assert balanced["examples"][0]["basis"] == "measured"


def test_the_preview_estimates_only_when_asked(api):
    wish = {"name": "Test", "resolutions": ["1080p"], "sources": ["webdl", "bluray"]}
    assert api.post("/api/profiles/preview", json=wish).json()["estimate"] is None

    radarr = _service_id(api, "radarr")
    out = api.post("/api/profiles/preview",
                   json={**wish, "estimate_for": radarr}).json()
    estimate = out["estimate"]
    assert out["qualities"] == ["webdl-1080p", "bluray-1080p"]
    assert estimate["scope"] == {"profile_id": None, "titles": 9}
    assert estimate["counts"]["replaced"] == 2        # the WEB-DL and the 720p
    assert estimate["counts"]["fetched"] == 1
    assert estimate["fits"] == "yes"
    assert estimate["free_gb"] == 2500
    assert {q["quality"] for q in estimate["qualities"]} == {
        "webdl-1080p", "bluray-1080p"}

    only = api.post("/api/profiles/preview", json={
        **wish, "estimate_for": radarr, "estimate_profile": 2}).json()["estimate"]
    assert only["scope"]["titles"] == 1


def test_a_preview_with_a_tight_cap_warns(api):
    radarr = _service_id(api, "radarr")
    out = api.post("/api/profiles/preview", json={
        "name": "Small", "resolutions": ["1080p"], "sources": ["bluray"],
        "size_limits": {"1080p": {"min_gb": 0, "max_gb": 2}},
        "estimate_for": radarr}).json()
    keys = [w["key"] for w in out["estimate"]["warnings"]]
    assert "storage.warning.capped" in keys
    assert all(w["text"] for w in out["estimate"]["warnings"])


def test_the_current_profiles_say_what_they_hold_and_whether_they_loop(api):
    radarr = _service_id(api, "radarr")
    body = api.get(f"/api/profiles/current?service_id={radarr}").json()
    rows = {row["id"]: row for row in body["profiles"]}
    assert rows[1]["titles"] == 8
    assert rows[1]["with_files"] == 7
    assert rows[1]["own_rate"]["basis"] == "profile"
    assert rows[1]["loop"]["risk"] == "none"
    assert rows[2]["loop"]["risk"] in ("warning", "error")
    assert rows[2]["loop"]["problems"][0]["text"]
    assert rows[1]["written_here"] is False
    assert body["free_gb"] == 2500


def test_an_unknown_service_is_refused(api):
    assert api.get("/api/profiles/current?service_id=999").status_code == 404


def test_the_storage_overview_joins_the_disks_and_their_pace(api):
    body = api.get("/api/storage").json()
    assert [s["kind"] for s in body["services"]] == ["radarr", "sonarr"]
    assert len(body["disks"]) == 1
    disk = body["disks"][0]
    assert {f["service"] for f in disk["folders"]} == {"Radarr", "Sonarr"}
    radarr, sonarr = body["services"]
    assert radarr["growth"]["known"] and sonarr["growth"]["known"]
    # Each rate is rounded to a tenth on its own, so their sum and the rate of
    # the disk can differ by a little more than one rounding step.
    assert disk["gb_per_month"] == pytest.approx(
        radarr["growth"]["gb_per_month"] + sonarr["growth"]["gb_per_month"],
        abs=0.5)
    assert disk["outlook"] == "months"
    assert disk["complete"] is True
    assert radarr["disks"] == sonarr["disks"] == [disk["id"]]
    assert radarr["library"]["by_quality"]
    assert sonarr["library"]["read"] == 1


def test_moving_titles_is_shown_before_it_is_done(api):
    radarr = _service_id(api, "radarr")
    preview = api.post("/api/profiles/assign", json={
        "service_id": radarr, "profile_id": 1, "titles": [9, 1, 12345]}).json()
    assert preview["dry_run"] is True
    assert preview["moving"] == 1                    # 1 is on it already
    assert preview["unknown"] == 1
    assert {r["id"]: r["outcome"] for r in preview["rows"]} == {
        1: "kept", 9: "replaced"}
    assert preview["summary"]
    assert FakeArr.moved == []

    done = api.post("/api/profiles/assign", json={
        "service_id": radarr, "profile_id": 1, "titles": [9, 1], "apply": True}).json()
    assert done["moved"] == 1
    assert FakeArr.moved == [("radarr", [9], 1)]


def test_the_titles_to_pick_from_say_which_profile_they_are_on(api):
    """What the page lists before it asks to move any of them."""
    radarr = _service_id(api, "radarr")
    body = api.get(f"/api/profiles/titles?service_id={radarr}").json()
    assert {p["id"] for p in body["profiles"]} == {1, 2}
    rows = {row["id"]: row for row in body["titles"]}
    assert len(rows) == 9
    assert rows[9]["profile_id"] == 2
    assert rows[8]["files"] == 0 and rows[8]["gb"] == 0
    assert rows[1]["gb"] > 0
    assert [r["title"] for r in body["titles"]] == sorted(r["title"] for r in body["titles"])
    assert api.get("/api/profiles/titles?service_id=999").status_code == 404


def test_moving_to_a_profile_that_does_not_exist_is_refused(api):
    radarr = _service_id(api, "radarr")
    response = api.post("/api/profiles/assign", json={
        "service_id": radarr, "profile_id": 77, "titles": [1]})
    assert response.status_code == 404
    response = api.post("/api/profiles/assign", json={
        "service_id": radarr, "profile_id": 1, "titles": []})
    assert response.status_code == 400
