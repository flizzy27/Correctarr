"""Failures measured on a live installation, one test each.

Every case here was found by reading five days of a real store and a real
Radarr and Sonarr history, not by reading the code. That is the point of
keeping them together: each one is a thing that looked right, passed every
test written from the inside, and was wrong in front of somebody's library.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app import policy, rules, years
from app import settings as S
from app.arr import Arr, ArrError, GoneError
from app.engine import Engine
from app.rules import Finding, Rule
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


def ago(hours: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


# ---------------------------------------------------------------------------
# Sonarr's file list takes one series at a time
# ---------------------------------------------------------------------------
def test_sonarr_files_are_asked_for_one_series_at_a_time(monkeypatch):
    """A comma separated list of series is answered with 400 — measured against
    a live Sonarr. The series library rules never saw one episode file."""
    arr = Arr("sonarr", "http://sonarr:8989", "key")
    asked = []

    def call(method, path, **kwargs):
        value = kwargs["params"]["seriesId"]
        asked.append(value)
        if isinstance(value, str) and "," in value:
            raise ArrError("400")
        return [{"seriesId": value, "relativePath": f"{value}.mkv"}]

    monkeypatch.setattr(arr, "_call", call)
    files = arr.files([1, 2, 3])
    assert asked == [1, 2, 3]
    assert len(files) == 3


def test_one_broken_series_does_not_hide_the_others(monkeypatch):
    arr = Arr("sonarr", "http://sonarr:8989", "key")

    def call(method, path, **kwargs):
        if kwargs["params"]["seriesId"] == 2:
            raise ArrError("broken")
        return [{"seriesId": kwargs["params"]["seriesId"]}]

    monkeypatch.setattr(arr, "_call", call)
    assert [f["seriesId"] for f in arr.files([1, 2, 3])] == [1, 3]


# ---------------------------------------------------------------------------
# An episode title is not a year
# ---------------------------------------------------------------------------
def test_an_episode_called_1912_is_not_a_film_from_1912():
    series = {"title": "The Vampire Diaries", "year": 2009, "seasons": [],
              "firstAired": "2009-09-10", "ended": True,
              "lastAired": "2017-03-10"}
    assert not years.judge(
        "The.Vampire.Diaries.S03E16.1912.German.DL.1080p.BluRay.x264-iNTENTiON",
        series).wrong


def test_a_different_film_of_the_same_name_is_still_caught():
    """Cargo (2009) is not Cargo (2017), and The Boogeyman (1980) is not The
    Boogeyman (2023). Both were caught correctly and must stay caught."""
    assert years.judge("Cargo.2009.German.DTS.1080p.BluRay.x264-VIAHD",
                       {"title": "Cargo", "year": 2017}).wrong
    assert years.judge(
        "The.Boogeyman.UNCUT.GERMAN.1980.DL.AC3.1080p.BluRay.x264-GOREHOUNDS",
        {"title": "The Boogeyman", "year": 2023}).wrong


# ---------------------------------------------------------------------------
# Not out yet is not missing
# ---------------------------------------------------------------------------
def test_a_film_that_is_not_out_yet_is_not_missing():
    """Avatar 5 (2031) and a handful of films still in cinemas were reported
    as missing and searched for six times each."""
    ctx = {"missing": [
        {"id": 66, "title": "Avatar 5", "year": 2031, "isAvailable": False},
        {"id": 7, "title": "Spider-Man: Brand New Day", "year": 2026,
         "isAvailable": False, "status": "inCinemas"},
        {"id": 9, "title": "Heat", "year": 1995, "isAvailable": True},
    ]}
    found = rules.check_missing_items(Radarr(), ctx, config())
    assert [f.title for f in found] == ["Heat"]


def test_an_episode_that_has_not_aired_is_not_missing():
    ctx = {"missing": [
        {"id": 1, "seriesId": 3, "seasonNumber": 4, "episodeNumber": 1,
         "airDateUtc": (datetime.now(UTC) + timedelta(days=9)).isoformat(),
         "series": {"id": 3, "title": "The Series"}},
        {"id": 2, "seriesId": 3, "seasonNumber": 3, "episodeNumber": 9,
         "airDateUtc": ago(24 * 30), "series": {"id": 3, "title": "The Series"}},
    ]}
    found = rules.check_missing_items(Sonarr(), ctx, config())
    assert len(found) == 1
    assert found[0].data["episode_ids"] == [2]


def test_a_title_that_says_nothing_about_availability_is_still_reported():
    """The benefit of the doubt goes to reporting: a gap nobody knows about
    is worse than a line too many."""
    found = rules.check_missing_items(
        Radarr(), {"missing": [{"id": 1, "title": "Old", "year": 1990}]}, config())
    assert len(found) == 1


# ---------------------------------------------------------------------------
# "Nothing better out there" needs separate occasions, not button presses
# ---------------------------------------------------------------------------
@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "s.db")


def test_presses_inside_the_gap_count_as_one(store):
    for _ in range(6):
        store.note_attempt("k", gap_hours=12)
    assert store.attempt("k")["tries"] == 1


def test_seventy_one_tries_in_forty_five_seconds_prove_nothing(store):
    """The row that was really in the store, and the verdict drawn from it."""
    with store._conn() as c:
        c.execute("INSERT INTO attempts(key,tries,first_try,last_try,settled) "
                  "VALUES('2:cutoff_unmet:11:s2',71,?,?,1)",
                  ("2026-09-21T19:48:05+00:00", "2026-09-21T19:48:50+00:00"))
    record = rules._settled({"store": store}, "2:cutoff_unmet:11:s2", config())
    assert record["settled"] == 0, "the verdict is taken back"
    assert store.attempt("2:cutoff_unmet:11:s2")["settled"] == 0


def test_three_searches_on_three_days_do_settle_it(store):
    with store._conn() as c:
        c.execute("INSERT INTO attempts(key,tries,first_try,last_try) "
                  "VALUES('k',3,?,?)", (ago(60), ago(1)))
    assert rules._settled({"store": store}, "k", config())["settled"] == 1


# ---------------------------------------------------------------------------
# Cutoff: which quality is on disk, and which one is wanted
# ---------------------------------------------------------------------------
def test_radarr_quality_is_read_from_the_movie_list():
    """Radarr sends the cutoff list without files, asked for or not."""
    ctx = {"profiles": [{"id": 1, "name": "HD", "cutoff": 7,
                         "items": [{"quality": {"id": 7, "name": "Bluray-1080p"}}]}],
           "below_cutoff": [{"id": 4, "title": "The Film", "qualityProfileId": 1}],
           "items": [{"id": 4, "movieFile": {
               "quality": {"quality": {"name": "HDTV-720p"}}}}]}
    said = rules.check_cutoff_unmet(Radarr(), ctx, config())[0].describe("en")
    assert "HDTV-720p" in said and "?" not in said


def test_a_group_cutoff_is_named_by_its_qualities():
    profile = {"id": 1, "name": "1080p Heimkino", "cutoff": 1001, "items": [
        {"id": 1001, "name": "1080p Heimkino", "allowed": True, "items": [
            {"quality": {"id": 3, "name": "WEBDL-1080p"}, "allowed": True},
            {"quality": {"id": 7, "name": "Bluray-1080p"}, "allowed": True},
            {"quality": {"id": 9, "name": "HDTV-1080p"}, "allowed": False}]}]}
    assert rules.cutoff_of(profile) == "WEBDL-1080p / Bluray-1080p"


# ---------------------------------------------------------------------------
# The box set that has no span says so without a question mark
# ---------------------------------------------------------------------------
def test_a_box_set_without_a_span_reads_cleanly():
    entry = {"id": 1, "title": "Twilight.Saga.Biss.in.alle.Ewigkeit.2008.2012."
                                "German.DTS.DL.1080p.BluRay.x264-HQX",
             "size": int(57.6 * 1024 ** 3),
             "movie": {"id": 250, "title": "Twilight", "year": 2008}}
    found = rules.check_collection_pack(Radarr(), {"queue": [entry]}, config())
    assert len(found) == 1
    assert "(?)" not in found[0].describe("en")
    assert "(?)" not in found[0].describe("de")


# ---------------------------------------------------------------------------
# A profile that loops
# ---------------------------------------------------------------------------
def looping_profile(**over):
    base = {"id": 9, "name": "1080p Quality HDR", "upgradeAllowed": True,
            "cutoff": 1001, "minFormatScore": 20000, "cutoffFormatScore": 540000,
            "minUpgradeFormatScore": 10000,
            "items": [{"id": 1001, "name": "1080p", "allowed": True, "items": [
                {"quality": {"id": 3, "name": "WEBDL-1080p"}, "allowed": True}]}],
            "formatItems": [{"name": "German", "score": 400000}]}
    base.update(over)
    return base


def grabs(n, score, series_id=1, episode_id=None):
    return [{"eventType": "grabbed", "seriesId": series_id,
             "episodeId": episode_id or 100 + i,
             "data": {"customFormatScore": str(score)}} for i in range(n)]


def test_a_target_no_grab_ever_reached_is_reported():
    """224 grabs, the best at 381600, a target of 520000."""
    ctx = {"profiles": [looping_profile()],
           "items": [{"id": 1, "qualityProfileId": 9}],
           "history": grabs(12, 381600)}
    found = rules.check_profile_loop(Sonarr(), ctx, config())
    assert found[0].data["problem"] == "unreachable"
    assert found[0].severity == "error"
    assert "381600" in found[0].describe("en")


def test_the_same_episode_fetched_again_and_again_is_reported():
    ctx = {"profiles": [looping_profile()],
           "items": [{"id": 1, "qualityProfileId": 9}],
           "history": grabs(4, 600000, episode_id=628)}
    found = rules.check_profile_loop(Sonarr(), ctx, config())
    assert found[0].data["problem"] == "repeating"


def test_a_profile_nobody_uses_cannot_loop():
    ctx = {"profiles": [looping_profile()],
           "items": [{"id": 1, "qualityProfileId": 17}], "history": []}
    assert rules.check_profile_loop(Sonarr(), ctx, config()) == []


def test_a_profile_upgrading_only_by_resolution_is_fine():
    ctx = {"profiles": [looping_profile(cutoffFormatScore=0)],
           "items": [{"id": 1, "qualityProfileId": 9}],
           "history": grabs(12, 381600)}
    assert rules.check_profile_loop(Sonarr(), ctx, config()) == []


def test_a_profile_set_back_by_another_program_is_not_fought_over(store):
    """Profilarr writes its own value back every hour. Correcting it again
    every hour would be a tug of war; saying where to change it is not."""
    store.note_attempt(rules.retune_key(Sonarr(), 9))
    ctx = {"profiles": [looping_profile()],
           "items": [{"id": 1, "qualityProfileId": 9}],
           "history": grabs(12, 381600), "store": store}
    found = rules.check_profile_loop(Sonarr(), ctx, config())
    assert found[0].data["problem"] == "set_back"
    verdict = policy.decide(policy.Policy(action="retune_profile"), found[0])
    assert verdict.act is False
    assert verdict.reason == "policy.managed_elsewhere"
    assert "Profilarr" in found[0].describe("en")


def test_a_cutoff_on_a_disallowed_quality_is_reported():
    profile = looping_profile(cutoffFormatScore=0, cutoff=31)
    ctx = {"profiles": [profile], "items": [{"id": 1, "qualityProfileId": 9}],
           "history": []}
    found = rules.check_profile_loop(Sonarr(), ctx, config())
    assert [f.data["problem"] for f in found] == ["cutoff_disabled"]


def test_a_floor_nothing_reaches_is_reported_and_left_to_a_person():
    profile = looping_profile(cutoffFormatScore=0, minFormatScore=500000)
    ctx = {"profiles": [profile], "items": [{"id": 1, "qualityProfileId": 9}],
           "history": []}
    found = rules.check_profile_loop(Sonarr(), ctx, config())
    assert found[0].data["problem"] == "floor"
    assert policy.decide(policy.Policy(action="retune_profile"),
                         found[0]).act is False


# ---------------------------------------------------------------------------
# The engine: retune, back off, and "gone" is not a failure
# ---------------------------------------------------------------------------
class FakeArr:
    def __init__(self, kind="sonarr"):
        self.kind, self.name, self.service_id = kind, kind.capitalize(), 2
        self.saved, self.calls, self.closed = [], 0, False
        self.profile = looping_profile()

    def reachable(self):
        return True, "fake"

    def close(self):
        self.closed = True

    def quality_profile(self, pid):
        return dict(self.profile)

    def save_quality_profile(self, body, existing_id=None):
        self.saved.append(body)
        self.profile = body
        return body

    def remove_from_queue(self, *a, **k):
        self.calls += 1
        raise self.error

    def __getattr__(self, name):              # anything else the run asks for
        return lambda *a, **k: []


@pytest.fixture
def engine(tmp_path, monkeypatch):
    made = Engine(Store(tmp_path / "e.db"))
    monkeypatch.setattr(made, "downloaders", lambda: [])
    monkeypatch.setattr(made, "indexer_managers", lambda: [])
    made.store.set("dry_run", False)
    return made


def use(monkeypatch, *rule_list):
    monkeypatch.setattr(rules, "ALL", tuple(rule_list))
    monkeypatch.setattr("app.engine.ALL", tuple(rule_list))


def test_retuning_sets_the_target_to_zero_and_remembers_it(engine, monkeypatch):
    service = FakeArr()
    finding = Finding(rule="profile_loop", severity="error", title="1080p",
                      message="finding.profile_loop_unreachable",
                      service="sonarr", data={"profile_id": 9, "target": 540000})
    use(monkeypatch, Rule("profile_loop", "system", lambda a, c, g: [finding],
                          actions=("report", "retune_profile"),
                          default_action="retune_profile"))
    monkeypatch.setattr(engine, "arr_services", lambda: [service])
    engine.run(deep=True)
    assert service.saved[0]["cutoffFormatScore"] == 0
    assert engine.store.attempt(rules.retune_key(service, 9))


def test_a_failed_action_is_not_retried_on_the_next_pass(engine, monkeypatch):
    """Ninety-six identical failures, one a minute, on one season pack."""
    service = FakeArr()
    service.error = ArrError("500 QualitySource")
    finding = Finding(rule="wrong_year", severity="error", title="X",
                      message="finding.not_an_upgrade", service="sonarr",
                      entry_id=5, data={"release": "X.S01E01"})
    use(monkeypatch, Rule("wrong_year", "queue", lambda a, c, g: [finding],
                          actions=("report", "remove"), default_action="remove"))
    monkeypatch.setattr(engine, "arr_services", lambda: [service])
    engine.run()
    engine.run()
    engine.run()
    assert service.calls == 1
    assert finding.data["_held"] == "policy.failed_recently"


def test_a_queue_entry_that_has_gone_is_not_a_failure(engine, monkeypatch):
    """It was imported, or removed, between being found and acted on."""
    service = FakeArr()
    service.error = GoneError("Sonarr does not know queue/1793426392")
    finding = Finding(rule="wrong_year", severity="error", title="X",
                      message="finding.not_an_upgrade", service="sonarr",
                      entry_id=5, data={"release": "X.S01E01"})
    use(monkeypatch, Rule("wrong_year", "queue", lambda a, c, g: [finding],
                          actions=("report", "remove"), default_action="remove"))
    monkeypatch.setattr(engine, "arr_services", lambda: [service])
    result = engine.run()
    assert finding.action is None
    assert finding.data["_held"] == "policy.already_gone"
    assert result["errors"] == []


def test_an_import_into_sonarr_uses_sonarrs_own_description(engine, monkeypatch):
    """Radarr calls the source "webdl", Sonarr expects "web". Handing Radarr's
    description to Sonarr is a 500 — ninety-six of them on one season pack."""
    sent = []

    class Importing(FakeArr):
        def import_candidates(self, download_id=None, folder=None):
            return [{"path": "/downloads/Show.S01/Show.S01E08.mkv",
                     "quality": {"quality": {"source": "web", "name": "WEBDL-1080p"}},
                     "languages": [{"id": 4, "name": "German"}],
                     "episodes": [{"id": 808}]}]

        def manual_import(self, files):
            sent.extend(files)

    service = Importing()
    finding = Finding(
        rule="unmatched_files", severity="warning", title="Show.S01",
        message="finding.unmatched_importable", service="sonarr",
        data={"todo": "import", "item_id": 21,
              "path": "/downloads/Show.S01/Show.S01E08.mkv",
              "quality": {"quality": {"source": "webdl"}}, "gb": 1.0})
    outcome = engine._import_match(service, finding, False, clean=False)
    assert outcome.state == "done"
    assert sent[0]["quality"]["quality"]["source"] == "web"
    assert sent[0]["episodeIds"] == [808]
    assert sent[0]["languages"] == [{"id": 4, "name": "German"}]
