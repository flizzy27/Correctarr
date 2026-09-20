"""Tests for the run itself.

The orchestration is where the worst bug in the previous build lived: ten of
twenty-six rules silently did nothing on an install without Radarr, because
"once per pass" had been expressed as "only on Radarr". These tests pin the
distinction down.
"""
from __future__ import annotations

import pytest

from app import rules as rules_module
from app import settings as S
from app.arr import ArrError
from app.engine import Engine
from app.rules import Finding, Rule
from app.storage import Store


class FakeArr:
    """An Arr service that answers without a network."""

    def __init__(self, kind="radarr", name=None, reachable=True):
        self.kind = kind
        self.name = name or kind.capitalize()
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

    def disk_space(self):
        return []

    def root_folders(self):
        return []

    def history(self, page_size=200):
        return []

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
    result = engine._delete_path(finding, cfg, dry=False)
    assert "FAILED" in result
    assert victim.exists(), "a file outside the configured area must survive"


def test_deleting_refuses_when_nothing_is_configured(engine, tmp_path):
    victim = tmp_path / "file.mkv"
    victim.write_bytes(b"\0")
    cfg = S.defaults()
    cfg["cleanup_paths"] = []
    assert "FAILED" in engine._delete_path(
        Finding(rule="leftover_files", severity="warning", title="x",
                message="finding.leftover_files", data={"path": str(victim)}),
        cfg, dry=False)
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
    result = engine._delete_path(finding, cfg, dry=False)
    assert "deleted" in result
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
    assert engine._delete_path(finding, cfg, dry=True).startswith("DRY RUN")
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
