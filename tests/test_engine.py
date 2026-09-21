"""Tests for the run itself.

The orchestration is where the worst bug in the previous build lived: ten of
twenty-six rules silently did nothing on an install without Radarr, because
"once per pass" had been expressed as "only on Radarr". These tests pin the
distinction down.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app import rules as rules_module
from app import settings as S
from app.arr import ArrError
from app.engine import Engine
from app.rules import Finding, Rule
from app.storage import Store


class FakeArr:
    """An Arr service that answers without a network."""

    def __init__(self, kind="radarr", name=None, reachable=True, service_id=1):
        self.kind = kind
        self.name = name or kind.capitalize()
        # The real connection carries the row it was built from, so an action
        # comes back to the instance that produced the finding.
        self.service_id = service_id
        self._reachable = reachable
        self.closed = False
        self.removed = []
        self.searched = []

    def reachable(self):
        return (True, "fake 1.0") if self._reachable else (False, "unreachable")

    def close(self):
        self.closed = True

    # Everything the context gatherer may ask for
    def queue(self):
        return []

    def profiles(self):
        return []

    def custom_formats(self):
        return []

    def health(self):
        return []

    def items(self):
        return []

    def missing(self):
        return []

    def below_cutoff(self):
        return []

    def blocklist(self):
        return []

    def files(self, item_ids):
        return []

    def disk_space(self):
        return []

    def root_folders(self):
        return []

    def history(self, page_size=200):
        return []

    def forget_history(self):
        """The real connection memoises the history for the length of a run.
        Nothing to forget here, but the engine asks and has to be answered."""

    def import_candidates(self, download_id=None, folder=None):
        return []

    def remove_from_queue(self, entry_id, blocklist=True, search_again=True):
        self.removed.append((entry_id, blocklist, search_again))

    def search(self, item_ids):
        self.searched.append(item_ids)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    store = Store(tmp_path / "engine.db")
    made = Engine(store)
    monkeypatch.setattr(made, "downloaders", lambda: [])
    monkeypatch.setattr(made, "indexer_managers", lambda: [])
    return made


def use_rules(monkeypatch, *rules):
    monkeypatch.setattr(rules_module, "ALL", tuple(rules))
    monkeypatch.setattr("app.engine.ALL", tuple(rules))


def counting_rule(name, calls, **kwargs):
    def check(arr, ctx, cfg):
        calls.append(arr.name)
        return []
    return Rule(name, "queue", check, **kwargs)


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------
def test_a_service_rule_runs_once_per_service(engine, monkeypatch):
    calls = []
    use_rules(monkeypatch, counting_rule("per_service", calls, scope="service"))
    monkeypatch.setattr(engine, "arr_services",
                        lambda: [FakeArr("radarr", "Radarr"),
                                 FakeArr("sonarr", "Sonarr")])
    engine.run()
    assert calls == ["Radarr", "Sonarr"]


def test_a_once_rule_runs_exactly_once_with_two_services(engine, monkeypatch):
    calls = []
    use_rules(monkeypatch, counting_rule("once_only", calls, scope="once"))
    monkeypatch.setattr(engine, "arr_services",
                        lambda: [FakeArr("radarr", "Radarr A"),
                                 FakeArr("radarr", "Radarr B")])
    engine.run()
    assert calls == ["Radarr A"], "a once rule must not run per service"


def test_a_once_rule_still_runs_without_radarr(engine, monkeypatch):
    """The bug that started all of this: on a Sonarr-only install ten rules did
    nothing at all, with no sign of it anywhere."""
    calls = []
    use_rules(monkeypatch, counting_rule("once_only", calls, scope="once"))
    monkeypatch.setattr(engine, "arr_services", lambda: [FakeArr("sonarr", "Sonarr")])
    engine.run()
    assert calls == ["Sonarr"]


def test_only_kinds_is_respected(engine, monkeypatch):
    calls = []
    use_rules(monkeypatch,
              counting_rule("radarr_only", calls, only_kinds=("radarr",)))
    monkeypatch.setattr(engine, "arr_services",
                        lambda: [FakeArr("radarr", "Radarr"),
                                 FakeArr("sonarr", "Sonarr")])
    engine.run()
    assert calls == ["Radarr"]


def test_a_deep_rule_is_skipped_on_a_fast_pass(engine, monkeypatch):
    calls = []
    use_rules(monkeypatch, counting_rule("expensive", calls, deep=True))
    monkeypatch.setattr(engine, "arr_services", lambda: [FakeArr()])
    engine.run(deep=False)
    assert calls == []
    engine.run(deep=True)
    assert calls == ["Radarr"]


def test_a_disabled_rule_does_not_run(engine, monkeypatch):
    calls = []
    use_rules(monkeypatch, counting_rule("switchable", calls))
    monkeypatch.setattr(engine, "arr_services", lambda: [FakeArr()])
    engine.store.set("rules", {"switchable": {"enabled": False}})
    engine.run()
    assert calls == []


def test_an_unreachable_service_is_reported_and_skipped(engine, monkeypatch):
    calls = []
    use_rules(monkeypatch, counting_rule("per_service", calls))
    monkeypatch.setattr(engine, "arr_services",
                        lambda: [FakeArr("radarr", "Radarr", reachable=False),
                                 FakeArr("sonarr", "Sonarr")])
    result = engine.run()
    assert calls == ["Sonarr"]
    assert any("Radarr" in e for e in result["errors"])


def test_every_service_is_closed_even_when_a_rule_explodes(engine, monkeypatch):
    services = [FakeArr("radarr", "Radarr"), FakeArr("sonarr", "Sonarr")]

    def boom(arr, ctx, cfg):
        raise RuntimeError("kaboom")

    use_rules(monkeypatch, Rule("explodes", "queue", boom))
    monkeypatch.setattr(engine, "arr_services", lambda: services)
    result = engine.run()
    assert all(s.closed for s in services)
    assert any("kaboom" in e for e in result["errors"])


def test_one_broken_rule_does_not_stop_the_others(engine, monkeypatch):
    calls = []

    def boom(arr, ctx, cfg):
        raise ValueError("bad rule")

    use_rules(monkeypatch,
              Rule("explodes", "queue", boom),
              counting_rule("healthy", calls))
    monkeypatch.setattr(engine, "arr_services", lambda: [FakeArr()])
    result = engine.run()
    assert calls == ["Radarr"], "a broken rule must not take the run down"
    assert len(result["errors"]) == 1


# ---------------------------------------------------------------------------
# Fixing
# ---------------------------------------------------------------------------
def finding_rule(name, finding, *, acts=False,
                 action="blocklist_and_search", **kwargs):
    """A stand-in rule that always reports the same finding."""
    return Rule(name, "queue", lambda arr, ctx, cfg: [finding],
                actions=("report", action),
                default_action=action if acts else "report", **kwargs)


def a_finding(rule="wrong_year", **kwargs):
    base = {"rule": rule, "severity": "error", "title": "The Film",
            "message": "finding.not_an_upgrade", "service": "radarr",
            "entry_id": 42}
    base.update(kwargs)
    return Finding(**base)


def test_nothing_is_fixed_unless_the_rule_says_so(engine, monkeypatch):
    service = FakeArr()
    use_rules(monkeypatch, finding_rule("wrong_year", a_finding(),
                                        acts=False))
    monkeypatch.setattr(engine, "arr_services", lambda: [service])
    result = engine.run()
    assert result["fixed"] == 0
    assert service.removed == []


def test_a_finding_is_fixed_when_the_rule_says_so(engine, monkeypatch):
    service = FakeArr()
    use_rules(monkeypatch, finding_rule("wrong_year", a_finding(),
                                        acts=True))
    engine.store.set("dry_run", False)
    monkeypatch.setattr(engine, "arr_services", lambda: [service])
    result = engine.run()
    assert result["fixed"] == 1
    assert service.removed == [(42, True, True)]


def test_a_dry_run_changes_nothing(engine, monkeypatch):
    service = FakeArr()
    use_rules(monkeypatch, finding_rule("wrong_year", a_finding(),
                                        acts=True))
    monkeypatch.setattr(engine, "arr_services", lambda: [service])
    engine.store.set("dry_run", True)
    result = engine.run()
    assert service.removed == [], "a dry run must not touch anything"
    assert result["fixed"] == 0, "a dry run must not be counted as fixed"
    assert result["findings"][0]["action"].startswith("DRY RUN")


def test_a_failed_fix_is_recorded_but_not_counted(engine, monkeypatch):
    class Failing(FakeArr):
        def remove_from_queue(self, *args, **kwargs):
            raise ArrError("service said no")

    use_rules(monkeypatch, finding_rule("wrong_year", a_finding(),
                                        acts=True))
    engine.store.set("dry_run", False)
    monkeypatch.setattr(engine, "arr_services", lambda: [Failing()])
    result = engine.run()
    assert result["fixed"] == 0
    assert result["findings"][0]["action"].startswith("FAILED")
    assert result["errors"]


def test_a_finding_is_fixed_on_the_service_it_belongs_to(engine, monkeypatch):
    radarr, sonarr = FakeArr("radarr", "Radarr"), FakeArr("sonarr", "Sonarr")
    use_rules(monkeypatch,
              finding_rule("wrong_year", a_finding(service="sonarr"),
                           acts=True, scope="once"))
    engine.store.set("dry_run", False)
    monkeypatch.setattr(engine, "arr_services", lambda: [radarr, sonarr])
    engine.run()
    assert sonarr.removed == [(42, True, True)]
    assert radarr.removed == []


# ---------------------------------------------------------------------------
# Recording and deduplication
# ---------------------------------------------------------------------------
def test_a_repeated_finding_is_recorded_only_once(engine, monkeypatch):
    use_rules(monkeypatch, finding_rule("wrong_year", a_finding(),
                                        acts=False))
    monkeypatch.setattr(engine, "arr_services", lambda: [FakeArr()])
    engine.run()
    engine.run()
    assert len(engine.store.findings()) == 1, "a long-running problem must not "\
                                              "be recorded on every pass"


def test_a_recorded_finding_can_be_read_back_in_both_languages(engine, monkeypatch):
    use_rules(monkeypatch, finding_rule(
        "stalled", a_finding(rule="stalled", message="finding.stalled",
                             params={"minutes": 30, "percent": "42"}),
        acts=False))
    monkeypatch.setattr(engine, "arr_services", lambda: [FakeArr()])
    engine.run()

    from app import i18n
    row = engine.store.findings()[0]
    assert "30" in row["description"]
    import json
    data = json.loads(row["data"])
    german = i18n.t(data["_msg"], "de", **data["_params"])
    assert "30 Minuten" in german


def test_a_run_is_recorded_with_its_trigger(engine, monkeypatch):
    use_rules(monkeypatch)
    monkeypatch.setattr(engine, "arr_services", lambda: [FakeArr()])
    engine.run(trigger="manual")
    assert engine.store.runs()[0]["trigger"] == "manual"


def test_two_runs_cannot_overlap(engine, monkeypatch):
    monkeypatch.setattr(engine, "arr_services", lambda: [FakeArr()])
    engine.running = True
    assert engine.run() == {"skipped": "already running"}
    engine.running = False


# ---------------------------------------------------------------------------
# Deleting
# ---------------------------------------------------------------------------
def test_deleting_refuses_a_path_outside_the_configured_directories(engine, tmp_path):
    """Checked twice — on detection and immediately before deleting. Minutes
    can pass between the two, and the setting can change in between."""
    victim = tmp_path / "precious.mkv"
    victim.write_bytes(b"\0" * 10)
    cfg = S.defaults()
    cfg["cleanup_paths"] = [str(tmp_path / "downloads")]
    finding = Finding(rule="leftover_files", severity="warning", title="x",
                      message="finding.leftover_files",
                      data={"path": str(victim), "mb": 0})
    result = engine._delete_path(finding, cfg, is_dry=False)
    assert result.state == "failed"
    assert victim.exists(), "a file outside the configured area must survive"


def test_deleting_refuses_when_nothing_is_configured(engine, tmp_path):
    victim = tmp_path / "file.mkv"
    victim.write_bytes(b"\0")
    cfg = S.defaults()
    cfg["cleanup_paths"] = []
    assert engine._delete_path(
        Finding(rule="leftover_files", severity="warning", title="x",
                message="finding.leftover_files", data={"path": str(victim)}),
        cfg, is_dry=False).state == "failed"
    assert victim.exists()


def test_deleting_works_inside_the_configured_directory(engine, tmp_path):
    area = tmp_path / "downloads"
    area.mkdir()
    junk = area / "junk"
    junk.mkdir()
    (junk / "a.rar").write_bytes(b"\0")
    cfg = S.defaults()
    cfg["cleanup_paths"] = [str(area)]
    finding = Finding(rule="leftover_files", severity="warning", title="junk",
                      message="finding.leftover_files",
                      data={"path": str(junk), "mb": 1})
    result = engine._delete_path(finding, cfg, is_dry=False)
    assert result.state == "done"
    assert "deleted" in result.text("en")
    assert not junk.exists()


def test_a_dry_run_never_deletes(engine, tmp_path):
    area = tmp_path / "downloads"
    area.mkdir()
    junk = area / "junk"
    junk.mkdir()
    cfg = S.defaults()
    cfg["cleanup_paths"] = [str(area)]
    finding = Finding(rule="leftover_files", severity="warning", title="junk",
                      message="finding.leftover_files",
                      data={"path": str(junk), "mb": 1})
    assert engine._delete_path(finding, cfg, is_dry=True).text("en").startswith(
        "DRY RUN")
    assert junk.exists()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def test_an_unknown_stored_setting_is_ignored(engine):
    """A leftover key from an older build must not poison the configuration."""
    engine.store.set("a_setting_that_no_longer_exists", 123)
    cfg = engine.config()
    assert "a_setting_that_no_longer_exists" not in cfg
    assert cfg["fast_seconds"] == 60


def test_rule_settings_fall_back_to_the_defaults(engine):
    engine.store.set("rules", {"wrong_year": {"enabled": False}})
    cfg = engine.config()
    assert cfg["rules"]["wrong_year"]["enabled"] is False
    # the action keeps its default rather than disappearing
    assert cfg["rules"]["wrong_year"]["action"] == "blocklist_and_search"
    # and every other rule is untouched
    assert cfg["rules"]["profile_violation"]["enabled"] is True


def test_cleanup_paths_are_derived_into_the_configuration(engine):
    engine.store.set("path_downloads", "/downloads")
    engine.store.set("path_incomplete", "/incomplete")
    assert engine.config()["cleanup_paths"] == ["/downloads", "/incomplete"]


def test_the_lead_service_prefers_radarr(engine):
    sonarr, radarr = FakeArr("sonarr"), FakeArr("radarr")
    assert engine._lead_service([sonarr, radarr]) is radarr
    assert engine._lead_service([sonarr]) is sonarr
    assert engine._lead_service([]) is None


def test_a_fresh_install_changes_nothing_until_it_is_told_to(engine, monkeypatch):
    """The most important default in the program.

    Two rules delete by default, and somebody installing this for the first
    time has not agreed to that — they have not even seen what it would find.
    So a store with nothing in it runs every rule and touches nothing, until
    the switch is turned off deliberately.
    """
    service = FakeArr()
    use_rules(monkeypatch, finding_rule("wrong_year", a_finding(), acts=True))
    monkeypatch.setattr(engine, "arr_services", lambda: [service])

    assert engine.config()["dry_run"] is True
    result = engine.run()
    assert service.removed == []
    assert result["fixed"] == 0
    assert result["findings"][0]["action"].startswith("DRY RUN")


# ---------------------------------------------------------------------------
# Two services of the same kind
# ---------------------------------------------------------------------------
def test_an_action_goes_back_to_the_instance_that_found_it(engine, monkeypatch):
    """Queue ids are not comparable across instances.

    Entry 42 in one Radarr is a different download — or none at all — in the
    second. Acting on "the finding" against whichever service happened to come
    first either did nothing or removed something nobody had asked about.
    """
    first, second = FakeArr(name="Radarr 4K", service_id=1), FakeArr(service_id=2)
    # A fresh finding per call, the way a real check builds them.
    use_rules(monkeypatch, Rule(
        "wrong_year", "queue", lambda arr, ctx, cfg: [a_finding()],
        actions=("report", "blocklist_and_search"),
        default_action="blocklist_and_search"))
    engine.store.set("dry_run", False)
    monkeypatch.setattr(engine, "arr_services", lambda: [first, second])

    engine.run()
    assert first.removed == [(42, True, True)]
    assert second.removed == [(42, True, True)]
    # Each one acted on its own finding exactly once, not twice on one of them.
    assert len(first.removed) == 1 and len(second.removed) == 1


def test_a_finding_from_the_other_kind_still_lands_somewhere(engine, monkeypatch):
    """An orphaned file listed through Radarr can belong to a series.

    Those name a kind the instance that produced them does not have, and they
    have to fall back to a service of the kind they do name rather than being
    dropped.
    """
    radarr, sonarr = FakeArr("radarr", service_id=1), FakeArr("sonarr", service_id=2)
    use_rules(monkeypatch, finding_rule(
        "wrong_year", a_finding(service="sonarr"), acts=True, scope="once"))
    engine.store.set("dry_run", False)
    monkeypatch.setattr(engine, "arr_services", lambda: [radarr, sonarr])

    engine.run()
    assert sonarr.removed == [(42, True, True)]
    assert radarr.removed == []


def test_a_season_gap_is_searched_episode_by_episode(engine, monkeypatch):
    """Searching the series to fill two holes makes Sonarr query every indexer
    for every episode it already has, against whatever daily limit they impose."""
    class Sonarr(FakeArr):
        def __init__(self):
            super().__init__("sonarr")
            self.episode_searches = []

        def episodes(self, series_id):
            return [
                {"id": 1, "seasonNumber": 2, "monitored": True, "hasFile": True},
                {"id": 2, "seasonNumber": 2, "monitored": True, "hasFile": False},
                {"id": 3, "seasonNumber": 2, "monitored": True, "hasFile": False},
                {"id": 4, "seasonNumber": 2, "monitored": False, "hasFile": False},
                {"id": 5, "seasonNumber": 3, "monitored": True, "hasFile": False},
            ]

        def search_episodes(self, ids):
            self.episode_searches.append(list(ids))

    service = Sonarr()
    finding = a_finding(rule="season_gaps", service="sonarr", entry_id=None,
                        data={"item_id": 7, "season": 2})
    use_rules(monkeypatch, finding_rule("season_gaps", finding, acts=True,
                                        action="search"))
    engine.store.set("dry_run", False)
    monkeypatch.setattr(engine, "arr_services", lambda: [service])

    engine.run()
    assert service.episode_searches == [[2, 3]]
    assert service.searched == [], "the whole series must not be searched"


def test_a_series_with_no_season_named_is_searched_whole(engine, monkeypatch):
    """A series that ended incomplete is missing episodes across the board."""
    service = FakeArr("sonarr")
    finding = a_finding(rule="series_incomplete", service="sonarr", entry_id=None,
                        data={"item_id": 7})
    use_rules(monkeypatch, finding_rule("series_incomplete", finding, acts=True,
                                        action="search"))
    engine.store.set("dry_run", False)
    monkeypatch.setattr(engine, "arr_services", lambda: [service])

    engine.run()
    assert service.searched == [[7]]


# ---------------------------------------------------------------------------
# Acting on one finding because somebody pressed a button
# ---------------------------------------------------------------------------
def _recorded(engine, **over):
    """Write one finding into the store the way a run would."""
    from app.engine import _recordable
    finding = a_finding(**over)
    engine.store.record(_recordable(finding))
    return engine.store.findings(limit=1)[0]


def test_a_button_does_it_even_though_the_dry_run_is_on(engine, monkeypatch):
    """The dry run guards changes nobody asked for. A button is the opposite,
    and one that quietly does nothing because of a setting on another page is
    worse than no button at all."""
    service = FakeArr()
    monkeypatch.setattr(engine, "arr_services", lambda: [service])
    engine.store.set("dry_run", True)
    row = _recorded(engine, rule="wrong_year")

    answer = engine.act_now(row, "blocklist_and_search")
    assert answer["state"] == "done"
    assert service.removed == [(42, True, True)]


def test_a_queue_finding_can_still_be_acted_on_tomorrow(engine, monkeypatch):
    """The queue id is not a column, so unless it travels in the data a
    finding written down today has nothing to act on the next morning."""
    service = FakeArr()
    monkeypatch.setattr(engine, "arr_services", lambda: [service])
    row = _recorded(engine, rule="wrong_year", entry_id=99)
    assert '"_entry_id": 99' in row["data"]
    engine.act_now(row, "remove")
    assert service.removed == [(99, False, False)]


def test_an_action_the_rule_may_not_take_is_refused(engine, monkeypatch):
    monkeypatch.setattr(engine, "arr_services", lambda: [FakeArr()])
    row = _recorded(engine, rule="wrong_year")
    with pytest.raises(ValueError, match="action_not_allowed"):
        engine.act_now(row, "delete")


def test_report_only_is_not_something_to_do(engine, monkeypatch):
    monkeypatch.setattr(engine, "arr_services", lambda: [FakeArr()])
    row = _recorded(engine, rule="wrong_year")
    with pytest.raises(ValueError, match="nothing_to_do"):
        engine.act_now(row, "report")


def test_the_result_is_written_back_onto_the_finding(engine, monkeypatch):
    monkeypatch.setattr(engine, "arr_services", lambda: [FakeArr()])
    row = _recorded(engine, rule="wrong_year")
    engine.act_now(row, "blocklist")
    again = engine.store.finding(row["id"])
    assert again["action"] == "blocklisted"
    assert '"_by_hand": true' in again["data"]


def test_a_reason_for_holding_back_is_cleared_once_it_is_done(engine, monkeypatch):
    """It said "not acted on: too young". It has now been acted on."""
    monkeypatch.setattr(engine, "arr_services", lambda: [FakeArr()])
    row = _recorded(engine, rule="wrong_year",
                    data={"_held": "policy.too_young", "_held_params": {}})
    engine.act_now(row, "blocklist")
    assert "_held" not in engine.store.finding(row["id"])["data"]


def test_the_suggestion_is_what_the_rule_would_do_on_its_own(engine):
    from app.rules import BY_NAME
    suggested = engine.suggested_action(BY_NAME["wrong_year"], a_finding())
    assert suggested == "blocklist_and_search"


def test_a_reporting_rule_still_offers_its_first_real_action(engine):
    """A rule left on "report only" has something worth offering; it is just
    not going to do it unasked."""
    from app.rules import BY_NAME
    assert engine.suggested_action(BY_NAME["missing_items"], a_finding()) == "search"
    assert engine.suggested_action(BY_NAME["grab_loop"], a_finding()) == "report"


# ---------------------------------------------------------------------------
# What the search found
# ---------------------------------------------------------------------------
def test_a_search_reports_what_it_grabbed(engine, monkeypatch):
    """"A search was started" is not the answer anybody wants. Whether there
    is anything out there is, and the service knows within seconds."""
    import time as clock

    class Searching(FakeArr):
        def __init__(self):
            super().__init__()

        def history(self, page_size=200):
            return [{"eventType": "grabbed", "movieId": 7,
                     "date": datetime.now(UTC).isoformat(),
                     "sourceTitle": "The.Film.2019.2160p.BluRay-GRP",
                     "data": {"indexer": "Somewhere"}}]

    service = Searching()
    monkeypatch.setattr(engine, "arr_services", lambda: [service])
    monkeypatch.setattr(clock, "sleep", lambda _seconds: None)
    row = _recorded(engine, rule="missing_items", entry_id=None,
                    data={"item_id": 7})

    answer = engine.act_now(row, "search")
    assert answer["found"]["state"] == "grabbed"
    assert "The.Film" in answer["found"]["releases"][0]["release"]
    assert answer["found"]["releases"][0]["indexer"] == "Somewhere"


def test_a_search_that_finds_nothing_says_so(engine, monkeypatch):
    import time as clock

    service = FakeArr()
    monkeypatch.setattr(engine, "arr_services", lambda: [service])
    monkeypatch.setattr(clock, "sleep", lambda _seconds: None)
    monkeypatch.setattr(engine, "SEARCH_PATIENCE", 0.0)
    row = _recorded(engine, rule="missing_items", entry_id=None,
                    data={"item_id": 7})

    answer = engine.act_now(row, "search")
    assert answer["found"]["state"] == "nothing"
    assert answer["found"]["releases"] == []


def test_a_grab_from_before_the_button_does_not_count(engine, monkeypatch):
    """Otherwise every search reports success on whatever was grabbed last."""
    import time as clock
    from datetime import timedelta

    class Stale(FakeArr):
        def __init__(self):
            super().__init__()

        def history(self, page_size=200):
            return [{"eventType": "grabbed", "movieId": 7,
                     "date": (datetime.now(UTC) - timedelta(hours=3)).isoformat(),
                     "sourceTitle": "Something.Older", "data": {}}]

    monkeypatch.setattr(engine, "arr_services", lambda: [Stale()])
    monkeypatch.setattr(clock, "sleep", lambda _seconds: None)
    monkeypatch.setattr(engine, "SEARCH_PATIENCE", 0.0)
    row = _recorded(engine, rule="missing_items", entry_id=None,
                    data={"item_id": 7})
    assert engine.act_now(row, "search")["found"]["state"] == "nothing"


# ---------------------------------------------------------------------------
# Noticing that there is simply nothing better out there
# ---------------------------------------------------------------------------
def test_a_search_is_counted(engine, monkeypatch):
    """Whether it helped is not recorded, and does not have to be: the next
    full pass answers that for free."""
    service = FakeArr()
    monkeypatch.setattr(engine, "arr_services", lambda: [service])
    row = _recorded(engine, rule="missing_items", entry_id=None,
                    data={"item_id": 7})
    engine.act_now(row, "search")

    from app.rules import searching_for
    assert engine.store.attempt(searching_for(service, "missing_items", 7))


def test_a_settled_finding_is_not_acted_on_by_itself(engine, monkeypatch):
    """Searching again on every pass from here asks a question that has been
    answered, at the cost of an indexer query each time."""
    from app import policy
    verdict = policy.decide(
        policy.Policy(action="search"),
        a_finding(data={"item_id": 7, "settled": True, "tries": 3}))
    assert verdict.act is False
    assert verdict.reason == "policy.nothing_better"


def test_a_settled_finding_can_still_be_acted_on_by_hand(engine, monkeypatch):
    """The judgement stops it happening automatically. It does not take the
    decision away from the person in front of it."""
    service = FakeArr()
    monkeypatch.setattr(engine, "arr_services", lambda: [service])
    row = _recorded(engine, rule="missing_items", entry_id=None,
                    data={"item_id": 7, "settled": True, "tries": 5})
    assert engine.act_now(row, "search")["state"] == "done"
    assert service.searched == [[7]]


# ---------------------------------------------------------------------------
# All of them at once
# ---------------------------------------------------------------------------
def test_a_batch_opens_the_services_once(engine, monkeypatch):
    """Forty findings one call at a time meant contacting every service forty
    times over — and on an unreachable one, forty timeouts in a row."""
    service = FakeArr()
    probes = []

    class Counting(FakeArr):
        def reachable(self):
            probes.append(1)
            return (True, "fake")

    counting = Counting()
    monkeypatch.setattr(engine, "arr_services", lambda: [counting])
    rows = [_recorded(engine, rule="wrong_year", entry_id=n) for n in (1, 2, 3)]

    answer = engine.act_many(rows, "blocklist")
    assert answer["done"] == 3
    assert len(probes) == 1, "the services were opened once, not once per row"
    assert counting.removed == [(1, True, False), (2, True, False),
                                (3, True, False)]
    del service


def test_a_batch_carries_on_past_one_that_fails(engine, monkeypatch):
    class Fussy(FakeArr):
        def remove_from_queue(self, entry_id, blocklist=True, search_again=True):
            if entry_id == 2:
                raise ArrError("no")
            self.removed.append((entry_id, blocklist, search_again))

    monkeypatch.setattr(engine, "arr_services", lambda: [Fussy()])
    rows = [_recorded(engine, rule="wrong_year", entry_id=n) for n in (1, 2, 3)]
    answer = engine.act_many(rows, "blocklist")
    assert answer["done"] == 2
    assert answer["failed"] == 1
    assert [r["state"] for r in answer["results"]] == ["done", "failed", "done"]


def test_a_batch_does_not_wait_for_every_search(engine, monkeypatch):
    """One search is worth twenty seconds of somebody's attention. Forty is
    not, and the answer arrives in the list on the next pass regardless."""
    waited = []

    class Watching(FakeArr):
        def history(self, page_size=200):
            waited.append(1)
            return []

    monkeypatch.setattr(engine, "arr_services", lambda: [Watching()])
    rows = [_recorded(engine, rule="missing_items", entry_id=None,
                      data={"item_id": n}) for n in (1, 2, 3)]
    engine.act_many(rows, "search")
    assert waited == [], "a batch must not wait on the history"
