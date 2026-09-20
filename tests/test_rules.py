"""Tests for the rules themselves — especially the ones that delete.

``leftover_files`` is the only rule that removes data irreversibly. Every guard
it has is tested here, because the cost of one of them silently breaking is a
deleted film.
"""
from __future__ import annotations

import os
import time

import pytest

from app import compat, rules
from app import settings as S
from app.rules import (
    check_api_changes,
    check_below_profile,
    check_detached_folder,
    check_disk_space,
    check_downloader_disk_space,
    check_downloader_paused,
    check_grab_loop,
    check_leftover_files,
    check_manual_import,
    check_missing_audio_language,
    check_not_an_upgrade,
    check_profile_violation,
    check_stalled,
    check_unmatched_files,
    check_unpack_failed,
    check_wrong_title,
    check_wrong_year,
)


class FakeArr:
    def __init__(self, kind="radarr", name="Radarr", deprecated=None):
        self.kind = kind
        self.name = name
        # The real client fills this in from response headers. The fake has to
        # carry it too, or a rule that reads it looks fine here and throws in
        # production — which is exactly what the empty-context test is for.
        self.observed = compat.Observed(version="5.0.0.1",
                                        deprecated=dict(deprecated or {}))


class FakeStore:
    """Just enough store for check_stalled."""

    def __init__(self, minutes=0.0):
        self.minutes = minutes
        self.pruned = None

    def check_progress(self, key, bytes_left):
        return self.minutes

    def prune_progress(self, active):
        self.pruned = active


def config(**overrides):
    cfg = S.defaults()
    cfg["cleanup_paths"] = S.cleanup_paths(cfg)
    cfg.update(overrides)
    return cfg


def queue_entry(title, year=2018, item_id=1, state="downloading",
                size=10 * 1024 ** 3, left=0, messages=(), profile_id=1,
                item_title="The Film", **movie):
    return {
        "id": 99, "title": title, "size": size, "sizeleft": left,
        "trackedDownloadState": state,
        "statusMessages": [{"messages": list(messages)}] if messages else [],
        "quality": {"quality": {"resolution": 1080, "source": "bluray",
                                "modifier": "none"}},
        "movie": {"id": item_id, "title": item_title, "year": year,
                  "qualityProfileId": profile_id, **movie},
    }


# ===========================================================================
# Queue
# ===========================================================================
def test_wrong_year_flags_a_different_film():
    ctx = {"queue": [queue_entry("Halloween.2018.1080p", year=1978)]}
    found = check_wrong_year(FakeArr(), ctx, config())
    assert len(found) == 1
    assert found[0].severity == "error"
    assert "2018" in found[0].describe("en")


def test_wrong_year_ignores_a_release_without_a_year():
    ctx = {"queue": [queue_entry("Halloween.1080p.BluRay", year=1978)]}
    assert check_wrong_year(FakeArr(), ctx, config()) == []


def test_wrong_year_honours_the_tolerance():
    ctx = {"queue": [queue_entry("The.Film.2019.1080p", year=2018)]}
    assert check_wrong_year(FakeArr(), ctx, config(year_tolerance=1)) == []
    assert len(check_wrong_year(FakeArr(), ctx, config(year_tolerance=0))) == 1


def test_wrong_year_leaves_a_title_that_is_itself_a_year_alone():
    # "1917" states no release year, so there is nothing to contradict. The
    # naive reading compared 1917 with 2019 and blocklisted a good release.
    ctx = {"queue": [queue_entry("1917.German.DL.1080p.BluRay.x264-GROUP",
                                 year=2019, item_title="1917")]}
    assert check_wrong_year(FakeArr(), ctx, config()) == []


def test_wrong_year_accepts_the_premiere_year_the_service_holds():
    ctx = {"queue": [queue_entry("The.Witch.2015.1080p.BluRay-GROUP", year=2016,
                                 item_title="The Witch", secondaryYear=2015)]}
    assert check_wrong_year(FakeArr(), ctx, config(year_tolerance=0)) == []


def test_wrong_year_accepts_a_release_date_the_service_holds():
    # Cinema in late December, disc the following spring. Both years are real.
    ctx = {"queue": [queue_entry("Hidden.Figures.2017.1080p.BluRay-GROUP",
                                 year=2016, item_title="Hidden Figures",
                                 physicalRelease="2017-04-11T00:00:00Z")]}
    assert check_wrong_year(FakeArr(), ctx, config(year_tolerance=0)) == []


def test_wrong_year_carries_a_confidence_so_a_near_miss_can_be_treated_gently():
    close = {"queue": [queue_entry("The.Film.2015.1080p", year=2018)]}
    far = {"queue": [queue_entry("The.Film.1995.1080p", year=2018)]}
    gentle = check_wrong_year(FakeArr(), close, config())[0]
    obvious = check_wrong_year(FakeArr(), far, config())[0]
    assert gentle.data["confidence"] < 0.3
    assert obvious.data["confidence"] == 1.0


def test_wrong_title_says_nothing_without_the_item_list():
    """Without alternate titles there is no basis for a judgement, and every
    localised release would be a false positive."""
    ctx = {"queue": [queue_entry("Stichtag.2010.German")], "items": []}
    assert check_wrong_title(FakeArr(), ctx, config()) == []


def test_wrong_title_accepts_a_localised_title():
    ctx = {"queue": [queue_entry("Stichtag.2010.German.1080p", year=2010)],
           "items": [{"id": 1, "title": "Due Date", "originalTitle": "Due Date",
                      "alternateTitles": [{"title": "Stichtag"}]}]}
    assert check_wrong_title(FakeArr(), ctx, config()) == []


def test_wrong_title_flags_a_genuinely_different_name():
    ctx = {"queue": [queue_entry("Completely.Different.2010.1080p", year=2010)],
           "items": [{"id": 1, "title": "Due Date", "originalTitle": "Due Date",
                      "alternateTitles": []}]}
    assert len(check_wrong_title(FakeArr(), ctx, config())) == 1


def test_profile_violation_needs_a_blocking_score():
    profile = {"id": 1, "name": "HD", "formatItems": [
        {"name": "3D", "score": -999999}]}
    formats = [{"name": "3D", "specifications": [
        {"implementation": "ReleaseTitleSpecification", "negate": False,
         "required": False, "fields": [{"name": "value", "value": r"\b3D\b"}]}]}]
    ctx = {"queue": [queue_entry("The.Film.3D.2018.1080p")],
           "profiles": [profile], "formats": formats}
    found = check_profile_violation(FakeArr(), ctx, config())
    assert len(found) == 1
    assert "3D" in found[0].describe("en")

    ctx["queue"] = [queue_entry("The.Film.2018.1080p")]
    assert check_profile_violation(FakeArr(), ctx, config()) == []


def test_not_an_upgrade_needs_both_the_state_and_the_message():
    cfg = config()
    yes = {"queue": [queue_entry("X.2018", state="importPending",
                                 messages=["Not an upgrade for existing file"])]}
    assert len(check_not_an_upgrade(FakeArr(), yes, cfg)) == 1

    wrong_state = {"queue": [queue_entry("X.2018", state="downloading",
                                         messages=["Not an upgrade"])]}
    assert check_not_an_upgrade(FakeArr(), wrong_state, cfg) == []

    wrong_message = {"queue": [queue_entry("X.2018", state="importPending",
                                           messages=["Something else"])]}
    assert check_not_an_upgrade(FakeArr(), wrong_message, cfg) == []


def test_stalled_ignores_a_download_that_has_not_started():
    """The client works its queue in order, so almost every entry has zero
    bytes. That is normal and must never be reported."""
    store = FakeStore(minutes=999)
    ctx = {"queue": [queue_entry("X.2018", size=1000, left=1000)], "store": store}
    assert check_stalled(FakeArr(), ctx, config()) == []


def test_stalled_ignores_a_finished_download():
    store = FakeStore(minutes=999)
    ctx = {"queue": [queue_entry("X.2018", size=1000, left=0)], "store": store}
    assert check_stalled(FakeArr(), ctx, config()) == []


def test_stalled_reports_one_that_stopped_half_way():
    store = FakeStore(minutes=200)
    ctx = {"queue": [queue_entry("X.2018", size=1000, left=400)], "store": store}
    found = check_stalled(FakeArr(), ctx, config(stalled_minutes=120))
    assert len(found) == 1
    assert "60" in found[0].describe("en")        # 60 percent done


def test_stalled_prunes_entries_that_left_the_queue():
    store = FakeStore()
    check_stalled(FakeArr(), {"queue": [], "store": store}, config())
    assert store.pruned == set()


def test_grab_loop_counts_only_recent_grabs():
    now = time.time()
    recent = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 3600))
    old = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 60 * 3600))
    ctx = {"history": [{"eventType": "grabbed", "date": recent, "movieId": 1,
                        "sourceTitle": "X"} for _ in range(4)]
                      + [{"eventType": "grabbed", "date": old, "movieId": 2,
                          "sourceTitle": "Y"} for _ in range(9)],
           "items": [{"id": 1, "title": "The Film"}]}
    found = check_grab_loop(FakeArr(), ctx, config(loop_grabs=4, loop_hours=12))
    assert len(found) == 1
    assert found[0].title == "The Film"


# ===========================================================================
# Import
# ===========================================================================
def test_manual_import_only_when_year_and_title_both_fit():
    cfg = config()
    good = {"queue": [queue_entry("The.Film.2018.1080p", year=2018,
                                  state="importBlocked",
                                  messages=["One or more movies expected in this release were not imported or missing, manual import required"])]}
    assert len(check_manual_import(FakeArr(), good, cfg)) == 1

    bad_year = {"queue": [queue_entry("The.Film.1999.1080p", year=2018,
                                      state="importBlocked",
                                      messages=["manual import required"])]}
    assert check_manual_import(FakeArr(), bad_year, cfg) == []


def test_detached_folder_uses_the_configured_path(tmp_path):
    """Earlier the listing was fetched against a hard coded /downloads and
    checked against the configured path. The moment anyone changed it, that was
    a false alarm."""
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    cfg = config(path_downloads=str(downloads))

    # Files reported, folder empty here -> detached
    ctx = {"import_candidates": [{"relativePath": "a/b.mkv"}]}
    assert len(check_detached_folder(FakeArr(), ctx, cfg)) == 1

    # Folder has content -> fine
    (downloads / "something").mkdir()
    assert check_detached_folder(FakeArr(), ctx, cfg) == []


def test_detached_folder_says_nothing_when_the_path_is_missing():
    """Not mounted is not the same as detached, and guessing would be wrong."""
    ctx = {"import_candidates": [{"relativePath": "a/b.mkv"}]}
    assert check_detached_folder(FakeArr(), ctx, config(path_downloads="/nope")) == []


def _unpack_folder(tmp_path, name, video_mb=0, age_hours=0.0):
    folder = tmp_path / name
    folder.mkdir()
    if video_mb:
        (folder / "film.mkv").write_bytes(b"\0" * (video_mb * 1024 * 1024))
    if age_hours:
        old = time.time() - age_hours * 3600
        os.utime(folder, (old, old))
    return folder


def test_unpack_running_is_never_reported(tmp_path):
    """_UNPACK_ is what the folder is called WHILE unpacking. An early build
    reported every one of them and produced nothing but noise."""
    _unpack_folder(tmp_path, "_UNPACK_Film.2020", age_hours=10)
    cfg = config(path_downloads=str(tmp_path))
    cfg["cleanup_paths"] = [str(tmp_path)]
    ctx = {"import_candidates": [{"relativePath": "_UNPACK_Film.2020/film.mkv",
                                  "path": str(tmp_path / "_UNPACK_Film.2020/film.mkv")}],
           "downloader_names": {"Film.2020"}}          # the client still knows it
    assert check_unpack_failed(FakeArr(), ctx, cfg) == []


def test_unpack_with_a_finished_video_is_left_to_the_import_rule(tmp_path):
    _unpack_folder(tmp_path, "_UNPACK_Film.2020", video_mb=400, age_hours=10)
    cfg = config(path_downloads=str(tmp_path), trash_video_mb=300)
    cfg["cleanup_paths"] = [str(tmp_path)]
    ctx = {"import_candidates": [{"relativePath": "_UNPACK_Film.2020/film.mkv",
                                  "path": str(tmp_path / "_UNPACK_Film.2020/film.mkv")}],
           "downloader_names": set()}
    assert check_unpack_failed(FakeArr(), ctx, cfg) == []


def test_unpack_genuinely_failed_is_reported(tmp_path):
    _unpack_folder(tmp_path, "_UNPACK_Film.2020", age_hours=10)
    cfg = config(path_downloads=str(tmp_path), unpack_timeout_hours=2)
    cfg["cleanup_paths"] = [str(tmp_path)]
    ctx = {"import_candidates": [{"relativePath": "_UNPACK_Film.2020/film.rar",
                                  "path": str(tmp_path / "_UNPACK_Film.2020/film.rar")}],
           "downloader_names": set()}
    found = check_unpack_failed(FakeArr(), ctx, cfg)
    assert len(found) == 1
    assert found[0].data["videos"] == 0


def test_unpack_too_young_is_not_reported_yet(tmp_path):
    _unpack_folder(tmp_path, "_UNPACK_Film.2020", age_hours=0.1)
    cfg = config(path_downloads=str(tmp_path), unpack_timeout_hours=2)
    cfg["cleanup_paths"] = [str(tmp_path)]
    ctx = {"import_candidates": [{"relativePath": "_UNPACK_Film.2020/film.rar",
                                  "path": str(tmp_path / "_UNPACK_Film.2020/film.rar")}],
           "downloader_names": set()}
    assert check_unpack_failed(FakeArr(), ctx, cfg) == []


# ===========================================================================
# Cleanup — the only rule that deletes
# ===========================================================================
def _debris(tmp_path, name, files=(), age_hours=48.0):
    folder = tmp_path / name
    folder.mkdir()
    for file_name, size in files:
        (folder / file_name).write_bytes(b"\0" * size)
    old = time.time() - age_hours * 3600
    for path in list(folder.rglob("*")) + [folder]:
        os.utime(path, (old, old))
    return folder


def _cleanup_ctx(**overrides):
    ctx = {"downloader_names": set(), "downloader_reachable": True,
           "all_queues": []}
    ctx.update(overrides)
    return ctx


def test_cleanup_does_nothing_without_a_download_client(tmp_path):
    """The decisive guard. The client creates the folder when a download is
    queued, not when it starts — so age alone is dangerous."""
    _debris(tmp_path, "Junk", [("a.rar", 10)])
    cfg = config()
    cfg["cleanup_paths"] = [str(tmp_path)]

    for state in (None, False):
        found = check_leftover_files(
            FakeArr(), _cleanup_ctx(downloader_reachable=state), cfg)
        assert len(found) == 1
        assert found[0].severity == "info"
        assert found[0].data == {}            # nothing proposed for deletion


def test_cleanup_does_nothing_without_a_configured_path():
    cfg = config()
    cfg["cleanup_paths"] = []
    found = check_leftover_files(FakeArr(), _cleanup_ctx(), cfg)
    assert len(found) == 1
    assert found[0].data == {}


def test_cleanup_spares_a_folder_the_client_still_knows(tmp_path):
    _debris(tmp_path, "Film.2020.Group", [("a.rar", 10)])
    cfg = config()
    cfg["cleanup_paths"] = [str(tmp_path)]
    ctx = _cleanup_ctx(downloader_names={"Film.2020.Group"})
    assert check_leftover_files(FakeArr(), ctx, cfg) == []


def test_cleanup_spares_a_folder_an_arr_queue_still_names(tmp_path):
    _debris(tmp_path, "Film.2020.Group", [("a.rar", 10)])
    cfg = config()
    cfg["cleanup_paths"] = [str(tmp_path)]
    ctx = _cleanup_ctx(all_queues=[{"title": "Film.2020.Group"}])
    assert check_leftover_files(FakeArr(), ctx, cfg) == []


def test_cleanup_spares_a_folder_holding_a_usable_video(tmp_path):
    _debris(tmp_path, "Film.2020", [("film.mkv", 5 * 1024 * 1024)])
    cfg = config(trash_video_mb=1)
    cfg["cleanup_paths"] = [str(tmp_path)]
    assert check_leftover_files(FakeArr(), _cleanup_ctx(), cfg) == []


def test_cleanup_reports_archive_remnants(tmp_path):
    _debris(tmp_path, "Film.2020", [("a.rar", 10), ("a.par2", 10)])
    cfg = config()
    cfg["cleanup_paths"] = [str(tmp_path)]
    found = check_leftover_files(FakeArr(), _cleanup_ctx(), cfg)
    assert len(found) == 1
    assert found[0].data["files"] == 2


def test_cleanup_reports_an_empty_folder_sooner(tmp_path):
    _debris(tmp_path, "Empty", [], age_hours=3)
    cfg = config(trash_age_hours=24, trash_empty_hours=2)
    cfg["cleanup_paths"] = [str(tmp_path)]
    found = check_leftover_files(FakeArr(), _cleanup_ctx(), cfg)
    assert len(found) == 1


def test_cleanup_waits_the_full_time_for_a_folder_with_content(tmp_path):
    _debris(tmp_path, "Junk", [("a.rar", 10)], age_hours=3)
    cfg = config(trash_age_hours=24, trash_empty_hours=2)
    cfg["cleanup_paths"] = [str(tmp_path)]
    assert check_leftover_files(FakeArr(), _cleanup_ctx(), cfg) == []


def test_cleanup_always_reports_a_failed_folder(tmp_path):
    _debris(tmp_path, "_FAILED_Film.2020", [("film.mkv", 5 * 1024 * 1024)])
    cfg = config(trash_video_mb=1)
    cfg["cleanup_paths"] = [str(tmp_path)]
    found = check_leftover_files(FakeArr(), _cleanup_ctx(), cfg)
    assert len(found) == 1


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_cleanup_never_follows_a_symlink(tmp_path):
    """A symlink pointing at the library must never be walked, let alone
    deleted."""
    outside = tmp_path / "library"
    outside.mkdir()
    (outside / "precious.mkv").write_bytes(b"\0" * 100)
    area = tmp_path / "downloads"
    area.mkdir()
    (area / "link").symlink_to(outside, target_is_directory=True)
    old = time.time() - 48 * 3600
    os.utime(area / "link", (old, old), follow_symlinks=False)

    cfg = config()
    cfg["cleanup_paths"] = [str(area)]
    assert check_leftover_files(FakeArr(), _cleanup_ctx(), cfg) == []


def test_cleanup_only_looks_inside_the_configured_directory(tmp_path):
    inside = tmp_path / "downloads"
    inside.mkdir()
    _debris(inside, "Junk", [("a.rar", 10)])
    other = tmp_path / "elsewhere"
    other.mkdir()
    _debris(other, "AlsoJunk", [("a.rar", 10)])

    cfg = config()
    cfg["cleanup_paths"] = [str(inside)]
    found = check_leftover_files(FakeArr(), _cleanup_ctx(), cfg)
    assert [f.title for f in found] == ["Junk"]


# ===========================================================================
# Library
# ===========================================================================
def test_audio_language_rule_is_silent_until_configured():
    """There is no sensible default for which languages matter."""
    ctx = {"items": [{"id": 1, "title": "X", "movieFile": {
        "mediaInfo": {"audioLanguages": "English"}}}]}
    assert check_missing_audio_language(FakeArr(), ctx, config()) == []


def test_audio_language_rule_flags_a_missing_language():
    ctx = {"items": [{"id": 1, "title": "X", "movieFile": {
        "mediaInfo": {"audioLanguages": "English"}}}]}
    found = check_missing_audio_language(FakeArr(), ctx,
                                         config(audio_languages="German"))
    assert len(found) == 1


def test_audio_language_rule_accepts_a_match():
    ctx = {"items": [{"id": 1, "title": "X", "movieFile": {
        "mediaInfo": {"audioLanguages": "German / English"}}}]}
    assert check_missing_audio_language(
        FakeArr(), ctx, config(audio_languages="German, English")) == []


def test_audio_language_rule_says_nothing_without_media_info():
    ctx = {"items": [{"id": 1, "title": "X", "movieFile": {"mediaInfo": {}}}]}
    assert check_missing_audio_language(
        FakeArr(), ctx, config(audio_languages="German")) == []


def test_below_profile_ignores_a_pure_size_shortfall():
    """The lower bounds in a profile say what should still be GRABBED. They are
    not a verdict on what is already there — measured, that would be 170 of 189
    findings."""
    profile = {"id": 1, "name": "HD", "formatItems": [
        {"name": "Under 5 GB", "score": -999999}]}
    formats = [{"name": "Under 5 GB", "specifications": [
        {"implementation": "SizeSpecification", "negate": False, "required": False,
         "fields": [{"name": "min", "value": 0}, {"name": "max", "value": 5}]}]}]
    ctx = {"items": [{"id": 1, "title": "X", "qualityProfileId": 1,
                      "movieFile": {"sceneName": "X.2018.1080p", "size": 2 * 1024 ** 3,
                                    "quality": {"quality": {"resolution": 1080}}}}],
           "profiles": [profile], "formats": formats}
    assert check_below_profile(FakeArr(), ctx, config()) == []


# ===========================================================================
# Download client and system
# ===========================================================================
def test_downloader_paused_is_reported():
    ctx = {"downloaders": [{"name": "SAB", "status": {"paused": True}, "waiting": 5}]}
    found = check_downloader_paused(FakeArr(), ctx, config())
    assert len(found) == 1
    assert "5" in found[0].describe("en")


def test_downloader_running_is_not_reported():
    ctx = {"downloaders": [{"name": "SAB", "status": {"paused": False}, "waiting": 5}]}
    assert check_downloader_paused(FakeArr(), ctx, config()) == []


def test_downloader_disk_space_escalates_below_half():
    ctx = {"downloaders": [{"name": "SAB", "status": {"diskspace1": "100"}}]}
    found = check_downloader_disk_space(FakeArr(), ctx, config(disk_threshold_gb=250))
    assert found[0].severity == "error"

    ctx = {"downloaders": [{"name": "SAB", "status": {"diskspace1": "200"}}]}
    found = check_downloader_disk_space(FakeArr(), ctx, config(disk_threshold_gb=250))
    assert found[0].severity == "warning"


def test_downloader_disk_space_survives_a_missing_number():
    ctx = {"downloaders": [{"name": "SAB", "status": {"diskspace1": None}}]}
    assert check_downloader_disk_space(FakeArr(), ctx, config()) == []


def test_disk_space_only_looks_at_root_folders():
    ctx = {"disk_space": [{"path": "/mnt/user/films", "freeSpace": 10 * 1024 ** 3,
                           "totalSpace": 100 * 1024 ** 3},
                          {"path": "/somewhere/else", "freeSpace": 1,
                           "totalSpace": 100 * 1024 ** 3}],
           "root_folders": [{"path": "/mnt/user/films"}]}
    found = check_disk_space(FakeArr(), ctx, config(disk_threshold_gb=250))
    assert [f.title for f in found] == ["/mnt/user/films"]


# ===========================================================================
# Unmatched files
# ===========================================================================
def test_unmatched_file_is_imported_when_it_is_an_improvement():
    item = {"id": 7, "title": "Crank", "year": 2006, "qualityProfileId": 1,
            "originalTitle": "Crank", "alternateTitles": [{"title": "Crank 1"}],
            "_kind": "radarr"}
    from app.matching import build_candidates
    ctx = {"import_candidates": [{
               "relativePath": "Crank.I.2006.German.1080p.BluRay-ABC/film.mkv",
               "path": "/downloads/Crank.I.2006.German.1080p.BluRay-ABC/film.mkv",
               "size": 8 * 1024 ** 3, "dateAdded": "2020-01-01T00:00:00Z",
               "rejections": [{"reason": "Unknown movie"}],
               "quality": {"quality": {"resolution": 1080}}}],
           "match_candidates": build_candidates([item]),
           "profiles": [{"id": 1, "name": "HD", "formatItems": []}],
           "formats": [], "history": [], "items": [item],
           "downloader_names": set()}
    found = check_unmatched_files(FakeArr(), ctx, config())
    assert len(found) == 1
    assert found[0].data["todo"] == "import"
    assert found[0].data["item_id"] == 7


def test_unmatched_file_gives_up_only_after_the_waiting_time():
    ctx = {"import_candidates": [{
               "relativePath": "Total.Nonsense.Name/film.mkv",
               "path": "/downloads/Total.Nonsense.Name/film.mkv",
               "size": 1, "dateAdded": "2020-01-01T00:00:00Z",
               "rejections": [{"reason": "Unknown movie"}]}],
           "match_candidates": [], "profiles": [], "formats": [],
           "history": [], "items": [], "downloader_names": set()}
    # No history entry means no id to search for, so nothing is proposed.
    found = check_unmatched_files(FakeArr(), ctx, config(give_up_hours=6))
    assert len(found) == 1
    assert found[0].data["todo"] is None

    off = check_unmatched_files(FakeArr(), ctx, config(give_up_hours=0))
    assert off[0].severity == "info"


def test_unmatched_file_keeps_the_service_of_the_matched_item():
    """A series belongs to Sonarr even when the folder was listed through
    Radarr. Without this the import lands on the wrong service."""
    item = {"id": 3, "title": "Some Show", "year": 2019, "qualityProfileId": 1,
            "originalTitle": "Some Show", "alternateTitles": [],
            "_kind": "sonarr"}
    from app.matching import build_candidates
    ctx = {"import_candidates": [{
               "relativePath": "Some.Show.2019.1080p-ABC/file.mkv",
               "path": "/downloads/Some.Show.2019.1080p-ABC/file.mkv",
               "size": 1, "dateAdded": "2020-01-01T00:00:00Z",
               "rejections": [{"reason": "Unknown series"}],
               "quality": {"quality": {"resolution": 1080}}}],
           "match_candidates": build_candidates([item]),
           "profiles": [{"id": 1, "name": "HD", "formatItems": []}],
           "formats": [], "history": [], "items": [item],
           "downloader_names": set()}
    found = check_unmatched_files(FakeArr(kind="radarr"), ctx, config())
    assert found[0].service == "sonarr"


# ===========================================================================
# General guarantees
# ===========================================================================
@pytest.mark.parametrize("rule", rules.ALL, ids=lambda r: r.name)
def test_every_rule_survives_an_empty_context(rule):
    """A missing key must never take the whole run down. The engine catches
    exceptions, but a rule that always throws is a silent hole."""
    ctx = {"queue": [], "profiles": [], "formats": [], "items": [],
           "missing": [], "disk_space": [], "root_folders": [], "history": [],
           "import_candidates": [], "downloaders": [], "indexer_state": [],
           "all_queues": [], "downloader_names": set(),
           "downloader_reachable": None, "match_candidates": None,
           "health": [], "store": FakeStore()}
    cfg = config()
    cfg["cleanup_paths"] = []
    result = rule.check(FakeArr(), ctx, cfg)
    assert isinstance(result, list)
    for finding in result:
        assert finding.describe("en")
        assert finding.describe("de")


# ---------------------------------------------------------------------------
# Field names that are on their way out
# ---------------------------------------------------------------------------
def test_both_spellings_of_the_remaining_size_are_read():
    from app.rules import size_left
    # What the services send today, and what they have announced as its
    # replacement. Reading only one of the two would mean the stalled rule
    # stops working on the day the rename lands.
    assert size_left({"sizeleft": 1500}) == 1500.0
    assert size_left({"sizeLeft": 1500}) == 1500.0
    assert size_left({}) == 0.0
    assert size_left({"sizeleft": None}) == 0.0
    assert size_left({"sizeleft": "not a number"}) == 0.0


# ---------------------------------------------------------------------------
# Advance warning about the services changing
# ---------------------------------------------------------------------------
def test_a_replaced_api_call_is_reported_before_it_breaks():
    arr = FakeArr(deprecated={"queue": 12})
    found = check_api_changes(arr, {}, config())
    assert len(found) == 1
    assert found[0].severity == "info"
    assert "queue" in found[0].describe("en")
    assert found[0].data["count"] == 12


def test_nothing_is_reported_while_everything_is_current():
    assert check_api_changes(FakeArr(), {}, config()) == []
