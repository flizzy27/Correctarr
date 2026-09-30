"""The insights: what happened over days, read from the store alone.

Every number here has a way of being wrong that looks right — counting rows
instead of findings, a season pack as twenty actions, a finding that is still
open as resolved — and each of those has a test.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import insights, safety
from app import main as main_module
from app.api import core, jobs
from app.engine import Engine, identity
from app.storage import MIGRATIONS, SCHEMA_VERSION, Store

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "insights.db")


def row(store, at: datetime, rule="stuck_in_queue", title="Film", path="/x",
        action=None, data=None):
    """Write one findings row at a given moment."""
    payload = {"path": path, "_msg": f"finding.{rule}", "_params": {}, **(data or {})}
    with store._conn() as c:
        c.execute("INSERT INTO findings(at,service,rule,severity,title,description,"
                  "action,data) VALUES(?,?,?,?,?,?,?,?)",
                  (at.isoformat(), "radarr", rule, "warning", title, "d", action,
                   json.dumps(payload)))
    return {"service": "radarr", "rule": rule, "severity": "warning", "title": title,
            "data": json.dumps(payload)}


def seen(store, key: str, at: datetime) -> None:
    with store._conn() as c:
        c.execute("INSERT INTO seen(key,last_seen) VALUES(?,?) ON CONFLICT(key) "
                  "DO UPDATE SET last_seen=excluded.last_seen", (key, at.isoformat()))


def summary(store, days=7, current=frozenset()):
    return insights.summarise(store, days, set(current), now=NOW, zone=UTC)


# ------------------------------------------------------------ the window
def test_the_window_is_whole_days_ending_today():
    days, start, dates = insights.window(3, NOW, UTC)
    assert days == 3
    assert start == datetime(2026, 9, 28, tzinfo=UTC)
    assert dates == ["2026-09-28", "2026-09-29", "2026-09-30"]


def test_a_day_before_the_clocks_went_back_is_counted_on_its_own_date(store, monkeypatch):
    """Half past midnight on a summer night, looked at in November. Read with
    November's offset it was half past eleven the evening before."""
    monkeypatch.setenv("TZ", "Europe/Berlin")
    november = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
    store.note_act("t", "stuck_in_queue", "import", at=datetime(2026, 10, 20, 22, 30,
                                                                  tzinfo=UTC))
    out = insights.summarise(store, 30, set(), now=november)
    day = next(d for d in out["daily"] if d["automatic"])
    assert day["date"] == "2026-10-21"


@pytest.mark.parametrize("asked,given", [(0, 1), (-5, 1), (500, insights.MOST_DAYS)])
def test_the_window_is_kept_to_what_is_stored(asked, given):
    assert insights.window(asked, NOW, UTC)[0] == given


def test_every_day_is_there_even_when_nothing_happened(store):
    out = summary(store, days=7)
    assert [d["date"] for d in out["daily"]][-1] == "2026-09-30"
    assert len(out["daily"]) == 7
    assert all(d["findings"] == 0 and d["automatic"] == 0 for d in out["daily"])
    assert out["resolution"] == {"resolved": 0, "mean_hours": None, "median_hours": None}


# ------------------------------------------------------------ findings
def test_a_finding_written_down_twice_in_a_day_counts_once(store):
    row(store, NOW - timedelta(hours=3))
    row(store, NOW - timedelta(hours=1))
    row(store, NOW - timedelta(hours=1), title="Another film", path="/y")
    out = summary(store)
    assert out["daily"][-1]["findings"] == 2
    assert out["totals"]["findings"] == 2


def test_the_rules_that_find_most_come_first(store):
    for n in range(3):
        row(store, NOW - timedelta(hours=1), rule="stray_files", path=f"/s{n}")
    row(store, NOW - timedelta(hours=1), rule="disk_space", path="/d")
    out = summary(store)
    assert [r["rule"] for r in out["top_rules"]][:2] == ["stray_files", "disk_space"]
    assert out["top_rules"][0]["findings"] == 3
    assert out["top_rules"][0]["title"] != "rules.stray_files.title"


# ------------------------------------------------------------ actions
def test_actions_are_split_by_who_took_them_and_how_they_ended(store):
    at = NOW - timedelta(hours=2)
    store.note_act("a", "stalled", "remove", identity="r1|1", at=at, state="done")
    store.note_act("b", "stalled", "remove", identity="r2|2", at=at, state="failed")
    store.note_act("c", "cutoff_unmet", "search", by_hand=True, at=at, state="done")
    today = summary(store)["daily"][-1]
    assert (today["automatic"], today["by_hand"], today["failed"]) == (2, 1, 1)


def test_a_season_pack_thrown_out_is_one_action_not_one_per_episode(store):
    at = NOW - timedelta(hours=1)
    for episode in range(22):
        store.note_act(f"sonarr:1:item:5:episode:{episode}", "wrong_year",
                       "blocklist", identity="Show.S01.Pack|dl-1", at=at, state="done")
    assert summary(store)["totals"]["automatic"] == 1


def test_a_loop_marker_is_not_an_action(store):
    store.note_act("a", "wrong_year", safety.LOOP, at=NOW - timedelta(hours=1))
    assert summary(store)["totals"]["automatic"] == 0


def test_space_freed_counts_only_what_was_deleted(store):
    at = NOW - timedelta(hours=1)
    store.note_act("p1", "leftover_files", "delete", at=at, state="done", freed_mb=2048)
    store.note_act("p2", "downloader_stale_entry", "remove_entry", at=at,
                   state="done", freed_mb=1024)
    out = summary(store)
    assert out["daily"][-1]["freed_gb"] == 3.0
    assert out["totals"]["freed_gb"] == 3.0


def test_the_guard_records_what_a_deletion_gave_back():
    class Finding:
        rule = "leftover_files"
        service = "radarr"
        title = "x"
        entry_id = None
        data = {"path": "/downloads/x", "mb": 512.0}

    recorded = {}

    class FakeStore:
        def note_act(self, *args, **kwargs):
            recorded.update(kwargs)

    safety.Guard(FakeStore()).note(Finding(), "delete", state="done")
    assert recorded["freed_mb"] == 512.0
    recorded.clear()
    safety.Guard(FakeStore()).note(Finding(), "delete", state="failed")
    assert recorded["freed_mb"] == 0.0
    recorded.clear()
    safety.Guard(FakeStore()).note(Finding(), "blocklist", state="done")
    assert recorded["freed_mb"] == 0.0


# ------------------------------------------------------------ resolution
def test_a_finding_no_pass_sees_any_more_counts_as_resolved(store):
    written = row(store, NOW - timedelta(hours=10))
    key = identity(written)[0]
    seen(store, key, NOW - timedelta(hours=4))
    out = summary(store)
    assert out["resolution"]["resolved"] == 1
    assert out["resolution"]["mean_hours"] == 6.0


def test_a_finding_still_there_is_not_resolved(store):
    written = row(store, NOW - timedelta(hours=10))
    key = identity(written)[0]
    seen(store, key, NOW)
    assert summary(store, current={key})["resolution"]["resolved"] == 0


def test_a_finding_that_began_before_the_window_keeps_its_real_beginning(store):
    row(store, NOW - timedelta(days=4))
    written = row(store, NOW - timedelta(hours=1))
    seen(store, identity(written)[0], NOW - timedelta(hours=1))
    out = summary(store, days=3)
    assert out["resolution"]["mean_hours"] == 95.0


# ------------------------------------------------------------ pauses
def test_every_pause_is_kept_with_when_it_was_resumed(store):
    guard = safety.Guard(store)
    guard.pause("safety.too_many", {"count": 61, "limit": 60})
    guard.resume()
    guard.pause("safety.too_many_discards", {"count": 31, "limit": 30})
    pauses = insights.summarise(store, 1, set())["pauses"]
    assert [p["reason_key"] for p in pauses] == ["safety.too_many",
                                                 "safety.too_many_discards"]
    assert pauses[0]["resumed"] and pauses[1]["resumed"] is None
    assert "61" in pauses[0]["reason"]


# ------------------------------------------------------------ the store
def test_the_schema_is_at_version_six_with_the_new_columns(store):
    assert SCHEMA_VERSION == 6
    with store._conn() as c:
        columns = {r["name"] for r in c.execute("PRAGMA table_info(actions)")}
        tables = {r["name"] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"state", "freed_mb"} <= columns
    assert "pauses" in tables


def test_a_store_at_version_five_is_upgraded_and_keeps_its_actions(tmp_path):
    path = tmp_path / "old.db"
    connection = sqlite3.connect(path)
    for _number, sql in MIGRATIONS[:5]:
        connection.executescript(sql)
    connection.execute("INSERT INTO actions(at,subject,rule,action) "
                       "VALUES('2026-09-01T00:00:00+00:00','s','stalled','remove')")
    connection.execute("PRAGMA user_version=5")
    connection.commit()
    connection.close()

    upgraded = Store(path)
    assert upgraded.version() == 6
    kept = upgraded.acts_since(datetime(2026, 8, 1, tzinfo=UTC))
    assert kept[0]["state"] == "" and kept[0]["freed_mb"] == 0


def test_a_half_applied_upgrade_still_adds_the_second_column(tmp_path):
    """A migration interrupted after its first column must not lose the second
    one on the next start because the first is reported as already there."""
    path = tmp_path / "half.db"
    connection = sqlite3.connect(path)
    for _number, sql in MIGRATIONS[:5]:
        connection.executescript(sql)
    connection.execute("ALTER TABLE actions ADD COLUMN state TEXT NOT NULL DEFAULT ''")
    connection.execute("PRAGMA user_version=5")
    connection.commit()
    connection.close()

    upgraded = Store(path)
    with upgraded._conn() as c:
        columns = {r["name"] for r in c.execute("PRAGMA table_info(actions)")}
    assert "freed_mb" in columns


# ------------------------------------------------------------ through the API
@pytest.fixture
def client(tmp_path, monkeypatch):
    fresh = Store(tmp_path / "api.db")
    engine = Engine(fresh)
    monkeypatch.setattr(core, "store", fresh)
    monkeypatch.setattr(core, "engine", engine)
    monkeypatch.setattr(jobs.scheduler, "start", lambda *a, **k: None)
    monkeypatch.setattr(jobs.scheduler, "shutdown", lambda *a, **k: None)
    monkeypatch.setattr(jobs, "schedule", lambda: None)
    monkeypatch.setattr(engine, "arr_services", lambda: [])
    with TestClient(main_module.app) as test_client:
        test_client.post("/api/auth/setup",
                         json={"name": "tester", "password": "a-proper-test-passphrase"})
        yield test_client


def test_the_insights_answer_with_every_part(client):
    answer = client.get("/api/insights?days=14").json()
    assert answer["days"] == 14
    assert len(answer["daily"]) == 14
    assert set(answer) >= {"daily", "totals", "top_rules", "resolution", "pauses",
                           "waiting", "since", "until"}
    assert answer["waiting"] == {"total": 0, "dismissed": 0, "by_rule": {}}


def test_the_insights_need_a_session(client):
    client.cookies.clear()
    assert client.get("/api/insights").status_code == 401
