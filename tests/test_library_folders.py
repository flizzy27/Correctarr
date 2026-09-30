"""What lies in a title's folder besides the files the service holds for it.

The cases are the ones found in a live library before the rule was written: a
film's folder holding the archive set of another film and three half-unpacked
downloads, and nine folders still carrying the sample their release came with.
Everything the rule is meant to leave alone is here as well — subtitles, the
extras media servers read, a series whose file list did not arrive.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import rules
from app.rules import _local_roots, _stray_kind, check_stray_files


class FakeArr:
    def __init__(self, kind="radarr", name="Radarr"):
        self.kind = kind
        self.name = name


def write(path: Path, size: int = 10) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def movie(root: Path, folder: str, file: str | None, **extra) -> dict:
    item = {"id": extra.pop("id", 1), "title": folder, "path": (root / folder).as_posix(),
            "hasFile": file is not None, **extra}
    if file is not None:
        item["movieFile"] = {"relativePath": file}
    return item


def ctx_for(root: Path, *items: dict, files: list | None = None) -> dict:
    return {"items": list(items), "files": files or [],
            "root_folders": [{"path": root.as_posix()}]}


CFG = {"path_movies": "/movies", "path_series": "/series"}


# ------------------------------------------------------------ what counts
@pytest.mark.parametrize("relative,expected", [
    ("Other.Film.2013.part116.rar", "archive"),
    ("Other.Film.2013.r54", "archive"),
    ("Other.Film.2013.par2", "archive"),
    ("Other.Film.2013.001", "archive"),
    ("_UNPACK_Other.Film.2012/other.film.mkv", "leftover"),
    ("_FAILED_Box.Set.1-3/film.2.mkv", "leftover"),
    ("film.mkv.partial~", "leftover"),
    ("film.mkv.!qB", "leftover"),
    ("Sample/film-sample.mkv", "sample"),
    ("film.sample.mkv", "sample"),
    ("Another.Film.2019.mkv", "video"),
    ("Trilogy/Part.1/Part.1.mkv", "video"),
])
def test_what_takes_space_and_does_not_belong_is_named(relative, expected):
    assert _stray_kind(relative) == expected


@pytest.mark.parametrize("relative", [
    "film.de.srt", "film.idx", "film.sub", "movie.nfo", "poster.jpg",
    "Bester VPN.url", "film.sfv",
    "Trailers/film.mkv", "Behind The Scenes/making of.mkv",
    "Featurettes/one.mp4", "Film (2010)-trailer.mkv", "Film (2010)-deleted.mkv",
])
def test_what_belongs_or_costs_nothing_is_left_alone(relative):
    assert _stray_kind(relative) is None


def test_a_word_that_merely_contains_sample_is_not_a_sample():
    assert _stray_kind("The.Samples.Collector.2020.mkv") == "video"


# ------------------------------------------------------------ the rule
def test_a_folder_holding_another_films_debris_is_reported_with_its_size(tmp_path):
    folder = tmp_path / "A Film (1992)"
    write(folder / "A Film (1992).mkv", 5000)
    write(folder / "Other.Film.2013" / "Other.Film.2013.part001.rar", 3000)
    write(folder / "Other.Film.2013" / "Other.Film.2013.part002.rar", 3000)
    write(folder / "_UNPACK_Third.Film.2012" / "third.film.mkv", 2000)
    write(folder / "Fourth.Film.2014.mkv", 4000)

    found = check_stray_files(FakeArr(), ctx_for(
        tmp_path, movie(tmp_path, "A Film (1992)", "A Film (1992).mkv")), CFG)

    assert len(found) == 1
    finding = found[0]
    assert finding.rule == "stray_files"
    assert finding.severity == "warning"
    assert finding.message == "finding.stray_files"
    assert finding.params["archives"] == 2
    assert finding.params["leftovers"] == 1
    assert finding.params["videos"] == 1
    assert finding.data["counts"] == {"archive": 2, "leftover": 1, "video": 1}
    assert finding.data["examples"][0] == "Fourth.Film.2014.mkv"
    assert finding.data["mb"] == round(12000 / 1024 ** 2, 1)
    assert finding.data["path"] == (tmp_path / "A Film (1992)").as_posix()


def test_the_titles_own_file_is_never_reported(tmp_path):
    folder = tmp_path / "Film (2020)"
    write(folder / "Release.2020" / "release.2020.mkv")
    item = movie(tmp_path, "Film (2020)", "Release.2020/release.2020.mkv")
    assert check_stray_files(FakeArr(), ctx_for(tmp_path, item), CFG) == []


def test_a_folder_with_only_a_sample_left_says_so_quietly(tmp_path):
    folder = tmp_path / "Film (2020)"
    write(folder / "Film.mkv")
    write(folder / "Sample" / "film-sample.mkv", 2048)
    write(folder / "film.sfv")

    found = check_stray_files(FakeArr(), ctx_for(
        tmp_path, movie(tmp_path, "Film (2020)", "Film.mkv")), CFG)

    assert [f.message for f in found] == ["finding.stray_samples"]
    assert found[0].severity == "info"
    assert found[0].params["count"] == 1


def test_subtitles_artwork_and_extras_leave_a_folder_clean(tmp_path):
    folder = tmp_path / "Film (2020)"
    write(folder / "Film.mkv")
    write(folder / "Film.de.srt")
    write(folder / "poster.jpg")
    write(folder / "movie.nfo")
    write(folder / "Trailers" / "trailer.mkv")
    write(folder / "Film-featurette.mp4")
    item = movie(tmp_path, "Film (2020)", "Film.mkv")
    assert check_stray_files(FakeArr(), ctx_for(tmp_path, item), CFG) == []


def test_nothing_is_ever_deleted_or_offered(tmp_path):
    folder = tmp_path / "Film (2020)"
    write(folder / "Film.mkv")
    debris = write(folder / "leftover.part01.rar")
    check_stray_files(FakeArr(), ctx_for(
        tmp_path, movie(tmp_path, "Film (2020)", "Film.mkv")), CFG)

    assert debris.exists()
    rule = rules.BY_NAME["stray_files"]
    assert rule.actions == ("report",)
    assert not rule.modifies and not rule.deletes


def test_a_film_marked_as_having_a_file_that_did_not_arrive_is_not_judged(tmp_path):
    """No file data is no evidence. Read as "no file", the film's own video
    would be reported as a stranger in its own folder."""
    write(tmp_path / "Film (2020)" / "Film.mkv")
    item = {"id": 1, "title": "Film", "path": (tmp_path / "Film (2020)").as_posix(),
            "hasFile": True}
    assert check_stray_files(FakeArr(), ctx_for(tmp_path, item), CFG) == []


def test_a_film_without_a_file_but_a_video_in_its_folder_is_reported(tmp_path):
    """The service has lost track of a file — worth knowing about."""
    write(tmp_path / "Film (2020)" / "Film.mkv")
    item = movie(tmp_path, "Film (2020)", None)
    found = check_stray_files(FakeArr(), ctx_for(tmp_path, item), CFG)
    assert found[0].params["videos"] == 1


def test_every_episode_file_of_a_series_is_its_own(tmp_path):
    series = tmp_path / "Show (2011)"
    write(series / "Season 01" / "Show - S01E01.mkv")
    write(series / "Season 01" / "Show - S01E02.mkv")
    write(series / "Season 01" / "show.s01e02.sample.mkv")
    item = {"id": 7, "title": "Show", "path": series.as_posix(),
            "statistics": {"episodeFileCount": 2}}
    files = [{"seriesId": 7, "relativePath": "Season 01/Show - S01E01.mkv"},
             {"seriesId": 7, "relativePath": "Season 01/Show - S01E02.mkv"}]

    found = check_stray_files(FakeArr("sonarr", "Sonarr"),
                              ctx_for(tmp_path, item, files=files), CFG)

    assert [f.message for f in found] == ["finding.stray_samples"]


def test_a_series_whose_file_list_did_not_arrive_is_left_alone(tmp_path):
    """A failed request is not an empty folder. Taken as one, every episode of
    the series would have been reported."""
    write(tmp_path / "Show (2011)" / "Season 01" / "Show - S01E01.mkv")
    item = {"id": 7, "title": "Show", "path": (tmp_path / "Show (2011)").as_posix(),
            "statistics": {"episodeFileCount": 1}}
    found = check_stray_files(FakeArr("sonarr", "Sonarr"),
                              ctx_for(tmp_path, item), CFG)
    assert found == []


# ------------------------------------------------------------ where the library is
def test_a_root_the_service_names_differently_is_found_through_the_setting(tmp_path):
    """Measured live: Sonarr keeps its series under /tv, this container sees
    the same folder as /series."""
    local = tmp_path / "series"
    write(local / "Show (2011)" / "Season 01" / "Show - S01E01.mkv")
    write(local / "Show (2011)" / "leftover.rar")
    item = {"id": 7, "title": "Show", "path": "/nowhere-tv/Show (2011)",
            "statistics": {"episodeFileCount": 1}}
    files = [{"seriesId": 7, "relativePath": "Season 01/Show - S01E01.mkv"}]
    ctx = {"items": [item], "files": files, "root_folders": [{"path": "/nowhere-tv"}]}
    cfg = {"path_series": local.as_posix()}

    assert _local_roots(FakeArr("sonarr"), ctx, cfg) == {"/nowhere-tv": local.as_posix()}
    found = check_stray_files(FakeArr("sonarr", "Sonarr"), ctx, cfg)
    assert found[0].params["archives"] == 1
    # Reported under the service's own path, so that it means something there.
    assert found[0].data["path"] == "/nowhere-tv/Show (2011)"


def test_several_roots_are_not_guessed_at(tmp_path):
    ctx = {"items": [], "root_folders": [{"path": "/nowhere-a"}, {"path": "/nowhere-b"}]}
    assert _local_roots(FakeArr(), ctx, {"path_movies": tmp_path.as_posix()}) == {}


def test_a_library_that_cannot_be_seen_says_so_once(tmp_path):
    item = {"id": 1, "title": "Film", "path": "/nowhere/Film (2020)", "hasFile": True,
            "movieFile": {"relativePath": "Film.mkv"}}
    ctx = {"items": [item], "root_folders": [{"path": "/nowhere"}]}
    found = check_stray_files(FakeArr(), ctx, {"path_movies": "/also-nowhere"})
    assert [f.message for f in found] == ["finding.stray_files_hidden"]
    assert found[0].severity == "info"


def test_a_library_nobody_set_up_stays_quiet():
    item = {"id": 1, "title": "Film", "path": "/nowhere/Film (2020)", "hasFile": False}
    ctx = {"items": [item], "root_folders": [{"path": "/nowhere"}]}
    assert check_stray_files(FakeArr(), ctx, {"path_movies": ""}) == []
