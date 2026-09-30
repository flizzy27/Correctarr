"""The summary: what a day or a week came to, through the existing channels.

It is built from the report every channel already renders, so these tests
check both halves: that it says the right things, and that a channel given it
renders it without knowing it is anything special.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import digest, safety
from app import main as main_module
from app import settings as S
from app.api import core, jobs
from app.engine import Engine
from app.notifications.channels.pushover import Pushover
from app.notifications.channels.webhook import Webhook
from app.storage import Store

NOW = datetime(2026, 9, 30, 8, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "digest.db")


def row(store, at, *, rule="leftover_files", title="Some.Release", action=None,
        state="done", key="action.result.deleted", params=None):
    data = {"path": f"/downloads/{title}", "_msg": "finding.leftover_files", "_params": {}}
    if action:
        data["_action"] = {"state": state, "key": key, "params": params or {"mb": 10}}
    with store._conn() as c:
        cursor = c.execute(
            "INSERT INTO findings(at,service,rule,severity,title,description,action,data) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (at.isoformat(), "radarr", rule, "warning", title, "d", action,
             json.dumps(data)))
        return {"id": cursor.lastrowid, "at": at.isoformat(), "service": "radarr",
                "rule": rule, "severity": "warning", "title": title, "action": action,
                "data": json.dumps(data)}


def compose(store, waiting=(), paused=None, period="weekly", language="en"):
    return digest.compose(store, period=period, waiting=list(waiting), paused=paused,
                          language=language, now=NOW)


# ------------------------------------------------------------ what it says
def test_a_quiet_week_sends_nothing(store):
    assert compose(store) is None


def test_it_says_what_was_fixed_and_what_is_waiting(store):
    row(store, NOW - timedelta(days=2), title="Old.Debris", action="deleted 10 MB")
    waiting = [row(store, NOW - timedelta(hours=3), rule="stuck_in_queue",
                   title="A Film")]
    summary = compose(store, waiting=waiting)

    assert summary.headline() == "Correctarr this week: 1 fixed, 1 waiting for you"
    titles = [section.title("en") for section in summary.groups]
    assert titles == ["Waiting for you", "Fixed"]
    waiting_line = summary.groups[0].findings[0].title
    assert waiting_line.endswith(": A Film") and not waiting_line.startswith("rules.")


def test_only_the_period_counts(store):
    row(store, NOW - timedelta(days=2), action="deleted 10 MB")
    assert compose(store, period="daily") is None
    assert compose(store, period="weekly").fixed == 1


class Radarr:
    kind, name, service_id = "radarr", "Radarr", 1

    def __init__(self):
        self.removed = []

    def reachable(self):
        return True, "fake"

    def close(self):
        pass

    def remove_from_queue(self, entry_id, blocklist=True, search_again=True):
        self.removed.append(entry_id)


def test_a_fix_by_hand_this_week_of_an_older_finding_is_in_the_summary(store, monkeypatch):
    """Found ten days ago, dealt with today. The row keeps the day it was
    found, and the week that dealt with it has to say so all the same."""
    now = datetime.now(UTC)
    engine = Engine(store)
    service = Radarr()
    monkeypatch.setattr(engine, "arr_services", lambda: [service])
    data = {"release": "Old.Release", "_msg": "finding.stalled", "_params": {},
            "_entry_id": 7}
    with store._conn() as c:
        cursor = c.execute(
            "INSERT INTO findings(at,service,rule,severity,title,description,action,data) "
            "VALUES(?,?,?,?,?,?,?,?)",
            ((now - timedelta(days=10)).isoformat(), "radarr", "stalled", "warning",
             "Old.Release", "d", None, json.dumps(data)))
    engine.act_now(store.finding(cursor.lastrowid), "remove")
    assert service.removed == [7]

    summary = digest.compose(store, period="weekly", waiting=[], paused=None, now=now)
    assert summary is not None and summary.fixed == 1
    a_week_on = now + timedelta(days=8)
    assert digest.compose(store, period="weekly", waiting=[], paused=None,
                          now=a_week_on) is None


def test_dry_runs_are_not_fixes_and_failures_have_their_own_section(store):
    row(store, NOW - timedelta(hours=2), title="A", action="DRY RUN: would delete",
        state="dry")
    row(store, NOW - timedelta(hours=2), title="B", action="FAILED: could not",
        state="failed", key="action.result.delete_failed", params={"error": "x"})
    summary = compose(store)
    assert summary.fixed == 0
    assert [s.rule for s in summary.groups] == ["digest_failed"]


def test_a_failure_retried_is_one_line_and_none_once_it_worked(store):
    for hours in (5, 4, 3):
        row(store, NOW - timedelta(hours=hours), title="Stuck", action="FAILED: no",
            state="failed", key="action.result.delete_failed", params={"error": "x"})
    assert len(compose(store).groups[0].findings) == 1

    row(store, NOW - timedelta(hours=1), title="Stuck", action="deleted 10 MB")
    assert [s.rule for s in compose(store).groups] == ["digest_fixed"]


def test_space_freed_by_deleting_is_in_the_heading(store):
    row(store, NOW - timedelta(hours=2), action="deleted 2048 MB")
    store.note_act("p", "leftover_files", "delete", at=NOW - timedelta(hours=2),
                   state="done", freed_mb=2048)
    fixed = compose(store).groups[-1]
    assert fixed.title("en") == "Fixed, 2.0 GB freed"


def test_a_pause_is_the_first_thing_it_says(store):
    guard = safety.Guard(store)
    guard.pause("safety.too_many", {"count": 61, "limit": 60})
    summary = digest.compose(store, period="weekly", waiting=[],
                             paused=guard.paused(), language="en")
    first = summary.groups[0]
    assert first.rule == "digest_paused"
    assert "61" in first.findings[0].title
    assert first.findings[0].action == "still paused"
    assert summary.severity == "error"


def test_a_pause_from_before_its_history_was_kept_is_still_mentioned(store):
    paused = {"since": NOW.isoformat(), "reason": "safety.too_many",
              "params": {"count": 70, "limit": 60}}
    summary = compose(store, paused=paused)
    assert summary.groups[0].findings[0].action == "still paused"


def test_it_speaks_german_when_asked(store):
    row(store, NOW - timedelta(hours=2), action="deleted 10 MB")
    summary = compose(store, language="de")
    assert summary.headline().startswith("Correctarr diese Woche")
    assert summary.groups[0].title("de") == "Behoben"
    assert summary.groups[0].findings[0].action == "gelöscht, 10 MB frei geworden"


# ------------------------------------------------------------ the channels
def test_a_channel_renders_it_without_knowing_what_it_is(store):
    row(store, NOW - timedelta(hours=2), title="Old.Debris", action="deleted 10 MB")
    summary = compose(store)
    lines = summary.lines(per_group=4)
    assert lines[0].endswith("Fixed (1)")
    assert lines[1].startswith("• Old.Debris →")


def test_the_webhook_payload_carries_the_sections(store, monkeypatch):
    row(store, NOW - timedelta(hours=2), title="Old.Debris", action="deleted 10 MB")
    sent = {}

    class Response:
        status_code = 200
        text = ""

    class Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, method, url, content=None, headers=None):
            sent["body"] = json.loads(content)
            return Response()

    monkeypatch.setattr("app.notifications.base.httpx.Client", Client)
    channel = Webhook({"url": "https://example.invalid/hook", "include_findings": True},
                      name="hook")
    ok, _detail = channel.send(compose(store))
    assert ok
    assert sent["body"]["title"].startswith("Correctarr this week")
    assert sent["body"]["groups"][0]["title"] == "Fixed"
    assert sent["body"]["findings"][0]["title"] == "Old.Debris"


def test_one_broken_connection_does_not_stop_the_others(store, monkeypatch):
    row(store, NOW - timedelta(hours=2), action="deleted 10 MB")
    calls = []
    monkeypatch.setattr(Pushover, "deliver",
                        lambda self, report: (calls.append(self.name), (True, "sent"))[1])
    connections = [
        {"id": 1, "name": "broken", "kind": "nonsense", "enabled": True},
        {"id": 2, "name": "phone", "kind": "pushover", "enabled": True,
         "config": {"app_token": "a", "user_key": "u"}},
        {"id": 3, "name": "off", "kind": "pushover", "enabled": False, "config": {}},
    ]
    outcomes = digest.send(connections, compose(store))
    assert [(o["name"], o["ok"]) for o in outcomes] == [("broken", False), ("phone", True)]
    assert calls == ["phone"]


# ------------------------------------------------------------ when it goes out
def test_it_is_due_once_per_period(store):
    assert digest.due(store, "weekly", NOW)
    store.set(digest.SENT_KEY, (NOW - timedelta(days=1)).isoformat())
    assert not digest.due(store, "weekly", NOW)
    assert digest.due(store, "daily", NOW)
    assert not digest.due(store, "off", NOW)


def test_the_setting_is_off_by_default_and_checked():
    assert S.BY_KEY["digest"].default == "off"
    with pytest.raises(ValueError):
        S.validate("digest", "hourly")
    with pytest.raises(ValueError):
        S.validate("digest_hour", 24)
    with pytest.raises(ValueError):
        S.validate("digest_day", "someday")
    assert S.validate("digest_hour", "7") == 7


def test_the_schedule_follows_the_setting(tmp_path, monkeypatch):
    from apscheduler.schedulers.background import BackgroundScheduler

    store = Store(tmp_path / "schedule.db")
    monkeypatch.setattr(core, "store", store)
    monkeypatch.setattr(core, "engine", Engine(store))
    scheduler = BackgroundScheduler(timezone="Etc/UTC")
    monkeypatch.setattr(jobs, "scheduler", scheduler)

    jobs.schedule()
    assert scheduler.get_job("digest") is None

    store.set_many({"digest": "weekly", "digest_day": "friday", "digest_hour": 18})
    jobs.schedule()
    trigger = str(scheduler.get_job("digest").trigger)
    assert "day_of_week='fri'" in trigger and "hour='18'" in trigger

    store.set("digest", "off")
    jobs.schedule()
    assert scheduler.get_job("digest") is None


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


def test_the_preview_says_nothing_would_go_out_on_a_quiet_week(client):
    answer = client.get("/api/digest").json()
    assert answer["period"] == "off"
    assert answer["preview"] is None
    assert answer["connections"] == 0


def test_sending_without_a_connection_is_refused_in_words(client):
    response = client.post("/api/digest/send")
    assert response.status_code == 400
    assert "error." not in response.json()["detail"]
