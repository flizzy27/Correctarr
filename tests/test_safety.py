"""The safety guards: nothing automatic may happen without limit.

The case these were written for was measured on a live installation. An
episode was flagged by the year check, removed, blocklisted and searched for;
the service grabbed another copy within seconds, the year check flagged that
one as well, and round it went — eight times in four minutes. Each round was
recorded as a failure, because the service answered that the queue entry did
not exist, while its own history showed the release being marked as failed at
the same second. The failure backoff did not stop it: every new grab is a new
release, and so a new finding.

Every test here is one way a loop could get through, and the guard that stops
it. Stubs throughout, never a live service.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app import i18n, notifications, policy, rules, safety
from app import settings as S
from app.arr import GoneError
from app.engine import Engine
from app.rules import Finding, Rule
from app.storage import Store


def ago(hours: float) -> datetime:
    return datetime.now(UTC) - timedelta(hours=hours)


def finding(rule="wrong_year", *, entry_id=None, service="sonarr", title="Show",
            **data) -> Finding:
    return Finding(rule=rule, severity="error", title=title,
                   message="finding.not_an_upgrade", service=service,
                   entry_id=entry_id, data=data)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "safety.db")


@pytest.fixture
def guard(store):
    return safety.Guard(store, safety.Limits())


# ---------------------------------------------------------------------------
# A service that grabs a replacement the moment one is thrown out
# ---------------------------------------------------------------------------
class Regrabbing:
    """Sonarr, as it behaved: every removal is followed by a new grab for the
    same episode, and the removal itself may be answered with "not found"."""

    def __init__(self, answer_gone=False):
        self.kind, self.name, self.service_id = "sonarr", "Sonarr", 2
        self.removed, self.answer_gone = [], answer_gone
        self.serial = 1
        self.entries = [self._grab()]

    def _grab(self) -> dict:
        self.serial += 1
        return {"id": 1000 + self.serial, "seriesId": 21, "episodeId": 808,
                "title": f"Show.S03E16.1912.German.DL.1080p-GRP{self.serial}"}

    def reachable(self):
        return True, "fake"

    def close(self):
        pass

    def queue(self):
        return list(self.entries)

    def remove_from_queue(self, entry_id, blocklist=True, search_again=True):
        self.removed.append(entry_id)
        # The replacement arrives straight away, as it did live.
        self.entries = [self._grab()]
        if self.answer_gone:
            raise GoneError(f"Sonarr does not know queue/{entry_id}")

    def __getattr__(self, name):              # anything else the run asks for
        return lambda *a, **k: []


def year_check(arr, ctx, cfg):
    return [finding(entry_id=entry["id"], title="Show S03E16",
                    release=entry["title"])
            for entry in ctx["queue"]]


@pytest.fixture
def engine(tmp_path, monkeypatch):
    made = Engine(Store(tmp_path / "e.db"))
    monkeypatch.setattr(made, "downloaders", lambda: [])
    monkeypatch.setattr(made, "indexer_managers", lambda: [])
    monkeypatch.setattr(made, "notification_targets", lambda: [])
    made.store.set("dry_run", False)
    return made


def use(monkeypatch, *rule_list):
    monkeypatch.setattr(rules, "ALL", tuple(rule_list))
    monkeypatch.setattr("app.engine.ALL", tuple(rule_list))


def year_rule(action="blocklist_and_search"):
    return Rule("wrong_year", "queue", year_check,
                actions=("report", "remove", "blocklist", "blocklist_and_search"),
                default_action=action)


def test_a_replacement_flagged_again_is_left_for_a_person(engine, monkeypatch):
    """Eight rounds in four minutes, live. Here: one, then it waits."""
    service = Regrabbing()
    use(monkeypatch, year_rule())
    monkeypatch.setattr(engine, "arr_services", lambda: [service])
    for _ in range(8):
        result = engine.run()
    assert len(service.removed) == 1
    held = result["findings"][0]["data"]
    assert held["_held"] == "policy.regrabbed"


def test_a_removal_answered_with_not_found_still_counts(engine, monkeypatch):
    """The live loop was recorded as a failure every single time."""
    service = Regrabbing(answer_gone=True)
    use(monkeypatch, year_rule())
    monkeypatch.setattr(engine, "arr_services", lambda: [service])
    for _ in range(8):
        engine.run()
    assert len(service.removed) == 1


def test_remove_without_blocklist_and_a_regrab_is_caught_too(engine, monkeypatch):
    service = Regrabbing()
    use(monkeypatch, year_rule("remove"))
    monkeypatch.setattr(engine, "arr_services", lambda: [service])
    for _ in range(5):
        engine.run()
    assert len(service.removed) == 1


def test_a_title_held_for_a_regrab_is_held_for_every_rule(guard, store):
    first = finding(entry_id=1, release="A", _subject="t")
    guard.note(first, "blocklist_and_search")
    assert guard.check(finding(entry_id=2, release="B", _subject="t"),
                       "blocklist").reason == "policy.regrabbed"
    other = finding("profile_violation", entry_id=2, release="B", _subject="t")
    assert guard.check(other, "blocklist").reason == "policy.regrabbed"


def test_the_same_release_found_again_is_not_a_regrab(guard):
    """Still in the queue on the next pass — the removal had not landed yet.
    That is for the per-title limit, not a sign of a new grab."""
    guard.note(finding(entry_id=1, release="A", _subject="t"), "remove")
    assert guard.check(finding(entry_id=1, release="A", _subject="t"),
                       "remove") is None


def test_a_different_rule_flagging_after_a_discard_is_not_a_regrab(guard):
    guard.note(finding("stalled", entry_id=1, release="A", _subject="t"), "remove")
    assert guard.check(finding(entry_id=2, release="B", _subject="t"),
                       "blocklist") is None


class SeasonPack(Regrabbing):
    """One download, listed as one queue entry per episode. Removing any of
    them removes the lot; the rest are then unknown to the service."""

    def __init__(self, episodes=22):
        super().__init__()
        self.pack = 1
        self.entries = self._pack(episodes)

    def _pack(self, episodes=22):
        return [{"id": 5000 + 100 * self.pack + n, "seriesId": 21,
                 "episodeId": 900 + n, "downloadId": f"PACK{self.pack}",
                 "title": f"Show.S02.German.DL.1080p-GRP{self.pack}"}
                for n in range(1, episodes + 1)]

    def remove_from_queue(self, entry_id, blocklist=True, search_again=True):
        self.removed.append(entry_id)
        if not any(e["id"] == entry_id for e in self.entries):
            raise GoneError(f"Sonarr does not know queue/{entry_id}")
        self.pack += 1
        self.entries = self._pack()


def test_a_season_pack_is_thrown_out_once_not_once_per_episode(engine, monkeypatch):
    service = SeasonPack()
    use(monkeypatch, year_rule("blocklist"))
    monkeypatch.setattr(engine, "arr_services", lambda: [service])
    result = engine.run()
    assert len(service.removed) == 1
    held = [f["data"].get("_held") for f in result["findings"]]
    assert held.count("policy.same_download") == 21
    # One action for the fuse, however many episodes it covered.
    assert engine.store.count_acts(ago(1)) == 1
    assert engine.guard().paused() is None


def test_a_replacement_pack_is_recognised_whichever_episode_comes_first(
        engine, monkeypatch):
    """The replacement lists its episodes in another order. Every episode
    of the first pack was covered by the one action, so every one of them is
    recognised when it turns up again."""
    service = SeasonPack()
    use(monkeypatch, year_rule("blocklist_and_search"))
    monkeypatch.setattr(engine, "arr_services", lambda: [service])
    engine.run()
    service.entries.reverse()
    for _ in range(5):
        result = engine.run()
    assert len(service.removed) == 1
    assert {f["data"].get("_held") for f in result["findings"]} == {"policy.regrabbed"}


# ---------------------------------------------------------------------------
# Per title
# ---------------------------------------------------------------------------
def test_a_title_is_acted_on_at_most_three_times_a_day_across_rules(guard, store):
    for rule in ("manual_import", "unmatched_files", "manual_import"):
        guard.note(finding(rule, _subject="radarr:1:item:7"), "import")
    hold = guard.check(finding("unreadable_file", _subject="radarr:1:item:7"),
                       "refresh")
    assert hold.reason == "policy.acted_too_often"
    assert hold.params == {"count": 3, "hours": "24"}


def test_actions_outside_the_window_do_not_count(guard, store):
    for _ in range(3):
        store.note_act("radarr:1:item:7", "manual_import", "import", at=ago(25))
    assert guard.check(finding(_subject="radarr:1:item:7"), "import") is None


def test_the_limit_and_the_window_come_from_the_settings(store):
    limits = safety.Limits.from_config({**S.defaults(), "safety_title_limit": 1,
                                        "safety_title_hours": 2})
    guard = safety.Guard(store, limits)
    store.note_act("x", "manual_import", "import", at=ago(1))
    assert guard.check(finding(_subject="x"), "import").params["count"] == 1
    store.note_act("y", "manual_import", "import", at=ago(3))
    assert guard.check(finding(_subject="y"), "import") is None


class Stuck(Regrabbing):
    """An import the service accepts and then does not carry out."""

    def __init__(self):
        super().__init__()
        self.kind, self.imported = "radarr", []

    def import_candidates(self, download_id=None, folder=None):
        return [{"path": "/downloads/x.mkv", "movie": {"id": 7}}]

    def manual_import(self, files):
        self.imported.append(files)


def stuck_import(engine, monkeypatch) -> tuple[Stuck, Finding]:
    service = Stuck()
    stuck = finding("manual_import", entry_id=5, service="radarr",
                    release="X.2020", downloadId="abc", item_id=7)
    use(monkeypatch, Rule("manual_import", "import", lambda a, c, g: [stuck],
                          actions=("report", "import"), default_action="import"))
    monkeypatch.setattr(engine, "arr_services", lambda: [service])
    return service, stuck


def test_an_import_that_does_not_take_is_not_repeated_every_pass(engine, monkeypatch):
    """Reported as done, the entry never leaves: without a limit it was
    imported again on every pass, once a minute."""
    service, stuck = stuck_import(engine, monkeypatch)
    for _ in range(10):
        engine.run()
    assert len(service.imported) == 3
    assert stuck.data["_held"] == "policy.acted_too_often"


def test_a_held_title_is_reported_even_though_it_is_not_new(engine, monkeypatch):
    """The finding held for going round in a circle is exactly the one that
    is not new any more. Being held is news of its own — once per reporting
    interval, not on every pass."""
    stuck_import(engine, monkeypatch)
    passes = [engine.run()["findings"][0] for _ in range(5)]
    assert [p["is_new"] for p in passes] == [True, False, False, True, False]
    assert passes[3]["data"]["_held"] == "policy.acted_too_often"


def test_a_button_press_is_never_blocked_but_is_counted(engine, monkeypatch):
    service = Regrabbing()
    engine.guard().pause("safety.too_many", {"count": 30, "limit": 25})
    for _ in range(3):
        engine.store.note_act("sonarr:2:item:21:episode:808", "wrong_year",
                              "blocklist", "A|1")
    row = {"id": 1, "rule": "wrong_year", "service": "sonarr", "title": "Show",
           "data": {"release": "A", "_entry_id": 1001, "_instance": 2,
                    "_subject": "sonarr:2:item:21:episode:808"}}
    answer = engine._act_one(row, "remove", [service], "en", verify=False)
    assert answer["state"] == "done"
    assert service.removed == [1001]
    rows = engine.store.acts_on("sonarr:2:item:21:episode:808", ago(1))
    assert [r["by_hand"] for r in rows][-1] == 1


def test_a_button_press_does_not_count_towards_the_fuse(guard, store):
    """Somebody working through forty findings with one button is not a
    runaway, and must not stop the automatic side for everybody."""
    for number in range(40):
        store.note_act(f"t{number}", "cutoff_unmet", "search", by_hand=True)
    assert guard.check(finding(_subject="new"), "search") is None
    assert guard.paused() is None


def test_a_dry_run_is_not_counted(engine, monkeypatch):
    service = Regrabbing()
    engine.store.set("dry_run", True)
    use(monkeypatch, year_rule())
    monkeypatch.setattr(engine, "arr_services", lambda: [service])
    for _ in range(5):
        engine.run()
    assert engine.store.count_acts(ago(1)) == 0
    assert service.removed == []


# ---------------------------------------------------------------------------
# What a finding is about
# ---------------------------------------------------------------------------
def test_a_queue_finding_is_about_the_title_in_its_queue_entry():
    """Most queue rules do not copy the item id into their findings."""
    found = finding(entry_id=5, service="radarr", release="X", _instance=1)
    assert safety.subject(found, {"id": 5, "movieId": 42}) == "radarr:1:item:42"


def test_two_episodes_of_one_series_are_two_titles():
    """A season of twenty-two episodes each needing an import is not one
    title acted on twenty-two times."""
    first = safety.subject(finding(entry_id=1, _instance=2),
                           {"seriesId": 21, "episodeId": 807})
    second = safety.subject(finding(entry_id=2, _instance=2),
                            {"seriesId": 21, "episodeId": 808})
    assert first != second
    assert first.startswith("sonarr:2:item:21:episode:")


def test_two_instances_of_the_same_kind_are_kept_apart():
    one = safety.subject(finding(service="radarr", item_id=7, _instance=1))
    two = safety.subject(finding(service="radarr", item_id=7, _instance=3))
    assert one != two


def test_without_an_item_the_profile_path_or_release_is_the_subject():
    assert safety.subject(finding("profile_loop", profile_id=9, _instance=2)) \
        == "sonarr:2:profile:9"
    assert safety.subject(finding("leftover_files", service="sabnzbd",
                                  path="/downloads/x")) == "sabnzbd:path:/downloads/x"
    assert safety.subject(finding(release="Some.Release")) \
        == "sonarr:release:Some.Release"
    assert safety.subject(finding("downloader_warning", service="sabnzbd",
                                  title="SAB: disk full")) \
        == "sabnzbd:downloader_warning:SAB: disk full"


def test_the_engine_stamps_the_subject_from_the_queue(engine, monkeypatch):
    service = Regrabbing()
    use(monkeypatch, year_rule("report"))
    monkeypatch.setattr(engine, "arr_services", lambda: [service])
    data = engine.run()["findings"][0]["data"]
    assert data["_subject"] == "sonarr:2:item:21:episode:808"


# ---------------------------------------------------------------------------
# The fuse
# ---------------------------------------------------------------------------
def test_the_fuse_trips_before_the_hourly_limit_is_exceeded(guard, store):
    for number in range(60):
        store.note_act(f"t{number}", "cutoff_unmet", "search")
    hold = guard.check(finding(_subject="t-new"), "search")
    assert hold.reason == "policy.fuse_paused" and hold.tripped
    assert guard.paused()["reason"] == "safety.too_many"
    assert guard.paused()["params"] == {"count": 60, "limit": 60}
    again = guard.check(finding(_subject="t-other"), "search")
    assert again.reason == "policy.fuse_paused" and not again.tripped


def test_throwing_things_away_has_a_lower_limit_of_its_own(guard, store):
    for number in range(30):
        store.note_act(f"t{number}", "not_an_upgrade", "blocklist")
    assert guard.check(finding(_subject="a"), "import") is None
    hold = guard.check(finding(_subject="b"), "blocklist")
    assert hold.tripped
    assert guard.paused()["reason"] == "safety.too_many_discards"


def test_actions_older_than_an_hour_do_not_trip_the_fuse(guard, store):
    for number in range(80):
        store.note_act(f"t{number}", "cutoff_unmet", "search", at=ago(1.5))
    assert guard.check(finding(_subject="new"), "search") is None


def test_the_fuse_survives_a_restart(tmp_path):
    path = tmp_path / "restart.db"
    safety.Guard(Store(path)).pause("safety.too_many", {"count": 26, "limit": 25})
    again = safety.Guard(Store(path))
    assert again.paused()["reason"] == "safety.too_many"
    assert again.check(finding(_subject="x"), "search").reason == "policy.fuse_paused"


def test_resuming_starts_the_count_afresh(guard, store):
    """Otherwise the actions that tripped it are still inside the hour, and
    the next one trips it again before the button is let go."""
    for number in range(60):
        store.note_act(f"t{number}", "cutoff_unmet", "search", at=ago(0.1))
    assert guard.check(finding(_subject="x"), "search").tripped
    guard.resume()
    assert guard.paused() is None
    assert guard.check(finding(_subject="y"), "search") is None


def test_a_tripped_fuse_stops_the_run_and_is_announced_once(engine, monkeypatch):
    service = Regrabbing()
    service.entries = [{"id": n, "seriesId": 100 + n, "episodeId": 500 + n,
                        "title": f"Other.S01E{n:02d}-GRP"} for n in range(1, 8)]
    engine.store.set("safety_discard_limit", 2)
    use(monkeypatch, year_rule("blocklist"))
    monkeypatch.setattr(engine, "arr_services", lambda: [service])
    sent = []
    monkeypatch.setattr(engine, "notification_targets",
                        lambda: [{"id": 1, "name": "phone", "kind": "ntfy"}])
    monkeypatch.setattr(notifications, "alert",
                        lambda targets, notice, **kw: sent.append(notice))
    result = engine.run()
    assert len(service.removed) == 2
    assert len(sent) == 1
    assert sent[0].title == i18n.t("safety.too_many_discards", "en",
                                   count=2, limit=2)
    held = [f["data"].get("_held") for f in result["findings"]]
    assert held.count("policy.fuse_paused") == 5
    engine.run()
    assert len(service.removed) == 2 and len(sent) == 1


def test_the_announcement_ignores_every_filter_and_cooldown(monkeypatch):
    """Somebody who only wants errors from the queue rules still has to hear
    that nothing is being fixed any more."""
    reports = []

    class Channel:
        def send(self, report):
            reports.append(report)
            return True, "sent"

    monkeypatch.setattr(notifications, "build", lambda connection: Channel())
    notifications.reset_cooldowns()
    notifications.note_sent(1)
    connection = {"id": 1, "name": "phone", "kind": "ntfy", "enabled": True,
                  "rules": ["wrong_year"], "categories": ["queue"],
                  "min_severity": "error", "fixed_only": True, "cooldown": 60}
    notice = Finding(rule=safety.NOTICE_RULE, severity="error",
                     title="26 automatic actions within an hour",
                     message="safety.too_many", service="correctarr")
    outcome = notifications.alert([connection], notice, language="de")
    assert outcome[0]["ok"]
    assert reports[0].headline() == i18n.t("safety.notify_title", "de")
    assert reports[0].groups[0].title("de") == i18n.t("safety.heading", "de")
    notifications.reset_cooldowns()


# ---------------------------------------------------------------------------
# Webhook storms
# ---------------------------------------------------------------------------
class FakeTimer:
    made: list = []

    def __init__(self, wait, function, args=()):
        self.wait, self.function, self.args = wait, function, args
        self.daemon = False
        FakeTimer.made.append(self)

    def start(self):
        pass

    def fire(self):
        self.function(*self.args)


class QuietEngine:
    """Just enough engine for the event path: a gap of eight seconds."""

    def __init__(self, answer=None):
        self.runs, self.answer = [], answer or {"found": 0}

    def config(self):
        return {"events_enabled": True, "event_debounce": 8}

    def run(self, deep=False, trigger=""):
        self.runs.append(trigger)
        return self.answer


@pytest.fixture
def events(monkeypatch):
    from app import main as main_module
    FakeTimer.made = []
    monkeypatch.setattr(main_module.threading, "Timer", FakeTimer)
    monkeypatch.setattr(main_module, "_last_event", 0.0)
    monkeypatch.setattr(main_module, "_event_pending", False)
    return main_module


def test_a_webhook_storm_is_one_pass_now_and_one_after_the_gap(events, monkeypatch):
    """Fifteen grab events two seconds apart, as a search wave sends them."""
    engine = QuietEngine()
    monkeypatch.setattr(events, "engine", engine)
    for _ in range(15):
        events._trigger_event("Sonarr/grab")
    assert len(FakeTimer.made) == 1 and FakeTimer.made[0].wait == 0
    FakeTimer.made[0].fire()
    assert engine.runs == ["Sonarr/grab"]

    for _ in range(15):
        events._trigger_event("Sonarr/grab")
    assert len(FakeTimer.made) == 2
    assert 0 < FakeTimer.made[1].wait <= 8


def test_an_event_during_a_running_pass_is_kept_for_one_more(events, monkeypatch):
    """The run refuses to start while another is going. The event is not
    lost — and it is not multiplied either."""
    engine = QuietEngine({"skipped": "already running"})
    monkeypatch.setattr(events, "engine", engine)
    events._trigger_event("Radarr/grab")
    FakeTimer.made[0].fire()
    for _ in range(5):
        events._trigger_event("Radarr/grab")
    assert len(FakeTimer.made) == 2


# ---------------------------------------------------------------------------
# The banner and the button
# ---------------------------------------------------------------------------
@pytest.fixture
def signed_in(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main as main_module
    store = Store(tmp_path / "api.db")
    made = Engine(store)
    monkeypatch.setattr(main_module, "store", store)
    monkeypatch.setattr(main_module, "engine", made)
    monkeypatch.setattr(main_module.scheduler, "start", lambda *a, **k: None)
    monkeypatch.setattr(main_module.scheduler, "shutdown", lambda *a, **k: None)
    monkeypatch.setattr(main_module.scheduler, "get_jobs", lambda: [])
    monkeypatch.setattr(main_module, "_schedule", lambda: None)
    monkeypatch.setattr(made, "arr_services", lambda: [])
    with TestClient(main_module.app) as client:
        answer = client.post("/api/auth/setup",
                             json={"name": "tester",
                                   "password": "a-proper-test-passphrase"})
        assert answer.status_code == 200, answer.text
        yield client, made


def test_the_status_says_why_actions_are_paused_and_the_button_resumes(signed_in):
    client, made = signed_in
    assert client.get("/api/status").json()["safety"] == {"paused": False}

    made.guard().pause("safety.too_many_discards", {"count": 11, "limit": 10})
    shown = client.get("/api/status", headers={"Accept-Language": "de"}).json()
    assert shown["safety"]["paused"] is True
    assert shown["safety"]["reason"] == i18n.t("safety.too_many_discards", "de",
                                               count=11, limit=10)

    answer = client.post("/api/safety/resume", json={})
    assert answer.status_code == 200
    assert answer.json()["safety"] == {"paused": False}
    assert made.guard().paused() is None


def test_resuming_needs_a_session(signed_in):
    client, made = signed_in
    made.guard().pause("safety.too_many", {"count": 26, "limit": 25})
    client.post("/api/auth/signout")
    assert client.post("/api/safety/resume", json={}).status_code == 401
    assert made.guard().paused() is not None


# ---------------------------------------------------------------------------
# Settings, texts and the interface
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("key", ["safety_title_limit", "safety_title_hours",
                                 "safety_hourly_limit", "safety_discard_limit"])
def test_no_limit_can_be_switched_off(key):
    field = S.BY_KEY[key]
    assert field.group == "safety"
    with pytest.raises(ValueError):
        field.validate(0)
    assert field.validate(field.default) == field.default


def test_the_defaults_are_the_documented_ones():
    limits = safety.Limits.from_config(S.defaults())
    assert limits == safety.Limits(per_title=3, window_hours=24, per_hour=60,
                                   discards_per_hour=30)


def test_an_unreadable_limit_falls_back_to_the_default_not_to_unlimited():
    limits = safety.Limits.from_config({"safety_title_limit": "lots",
                                        "safety_hourly_limit": 0,
                                        "safety_discard_limit": None})
    assert limits == safety.Limits()


def test_discards_are_what_makes_the_service_fetch_something_else():
    assert policy.DESTRUCTIVE <= policy.DISCARDS
    assert {"remove", "blocklist", "blocklist_and_search"} <= policy.DISCARDS
    assert not {"search", "import", "refresh", "retune_profile"} & policy.DISCARDS


@pytest.mark.parametrize("key", [
    "policy.acted_too_often", "policy.regrabbed", "policy.fuse_paused",
    "policy.same_download",
    "safety.heading", "safety.notify_title", "safety.too_many",
    "safety.too_many_discards", "safety.banner_title", "safety.banner_help",
    "safety.resume", "safety.resumed", "unit.actions",
    "settings_page.group_safety", "settings_page.group_safety_help",
])
def test_every_safety_text_reads_in_both_languages(key):
    for code in i18n.AVAILABLE:
        assert key in i18n.bundle(code), f"{code}: {key}"


def test_the_readme_describes_the_limits_with_their_defaults():
    from pathlib import Path
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(
        encoding="utf-8")
    assert "### Safety limits" in readme
    for key in ("safety_title_limit", "safety_title_hours",
                "safety_hourly_limit", "safety_discard_limit"):
        assert f"`{key}`" in readme, key
