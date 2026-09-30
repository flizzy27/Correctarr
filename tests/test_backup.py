"""Settings, rules, services and notifications out to a file and back in.

The two things a backup must never do are carry a key somebody did not ask to
carry, and cost somebody a setting on the way back in. Both are tested here,
along with the round trip itself and a restore that only says what it would do.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import backup, notifications
from app import main as main_module
from app import settings as S
from app.api import core, jobs
from app.engine import Engine
from app.storage import Store

KINDS = ("radarr", "sonarr", "sabnzbd", "prowlarr")
PASSPHRASE = "a-proper-test-passphrase"


@pytest.fixture
def engine(tmp_path):
    store = Store(tmp_path / "backup.db")
    store.save_service({"name": "Radarr", "kind": "radarr", "url": "http://radarr:7878",
                        "api_key": "radarr-key-123", "enabled": True, "webhook": True})
    store.save_notification({"name": "Phone", "kind": "pushover",
                             "config": notifications.validate("pushover", {
                                 "app_token": "app-token-abc",
                                 "user_key": "user-key-def"}),
                             "min_severity": "error", "rules": ["stray_files"]})
    store.set("fast_seconds", 45)
    return Engine(store)


def exported(engine, secrets=False) -> dict:
    return backup.export(engine.store, engine.config(), engine.rule_settings(),
                         secrets=secrets, version="test")


def planned(engine, data):
    return backup.plan(engine.store, engine.config(), engine.rule_settings(), data, KINDS)


def fresh_engine(tmp_path, name="other.db") -> Engine:
    return Engine(Store(tmp_path / name))


# ------------------------------------------------------------ export
def test_a_backup_leaves_every_key_and_token_out_by_default(engine):
    text = str(exported(engine))
    for secret in ("radarr-key-123", "app-token-abc", "user-key-def"):
        assert secret not in text


def test_keys_are_only_in_it_when_asked_for(engine):
    data = exported(engine, secrets=True)
    assert data["secrets"] is True
    assert data["services"][0]["api_key"] == "radarr-key-123"
    assert data["notifications"][0]["config"]["app_token"] == "app-token-abc"


def test_a_backup_holds_every_setting_and_every_rule(engine):
    data = exported(engine)
    assert data["format"] == backup.FORMAT
    assert set(data["settings"]) == {f.key for f in S.FIELDS}
    assert data["settings"]["fast_seconds"] == 45
    assert set(data["rules"]) == set(engine.rule_settings())
    assert set(data["rules"]["leftover_files"]) == {
        "enabled", "action", "min_age_hours", "max_gb", "min_confidence"}


# ------------------------------------------------------------ round trips
def test_the_same_installation_restored_changes_nothing(engine):
    result = planned(engine, exported(engine))
    assert result.count == 0
    assert result.skipped == []


def test_a_full_backup_rebuilds_an_empty_installation(engine, tmp_path):
    data = exported(engine, secrets=True)
    target = fresh_engine(tmp_path)
    result = planned(target, data)
    backup.apply(target.store, result, target.rule_settings())

    assert target.config()["fast_seconds"] == 45
    service = target.store.services()[0]
    assert (service["name"], service["api_key"]) == ("Radarr", "radarr-key-123")
    connection = target.store.notifications()[0]
    assert connection["config"]["user_key"] == "user-key-def"
    assert connection["rules"] == ["stray_files"]
    # And a second restore of the same file has nothing left to do.
    assert planned(target, data).count == 0


def test_rules_travel_with_their_action_and_conditions(engine, tmp_path):
    rules = engine.rule_settings()
    rules["stalled"] = {**rules["stalled"], "action": "remove", "min_age_hours": 6.0,
                        "enabled": False}
    engine.store.set("rules", rules)
    target = fresh_engine(tmp_path)
    result = planned(target, exported(engine))
    backup.apply(target.store, result, target.rule_settings())

    restored = target.rule_settings()["stalled"]
    assert (restored["action"], restored["min_age_hours"], restored["enabled"]) == (
        "remove", 6.0, False)
    # Every other rule is exactly as it was.
    before = fresh_engine(tmp_path, "untouched.db").rule_settings()
    for name, entry in target.rule_settings().items():
        if name != "stalled":
            assert entry == before[name], name


# ------------------------------------------------------------ keys left out
def test_a_backup_without_keys_keeps_the_keys_already_stored(engine):
    data = exported(engine)
    data["services"][0]["url"] = "http://radarr:7878/radarr"
    result = planned(engine, data)
    backup.apply(engine.store, result, engine.rule_settings())

    service = engine.store.services()[0]
    assert service["url"] == "http://radarr:7878/radarr"
    assert service["api_key"] == "radarr-key-123"
    assert engine.store.notifications()[0]["config"]["app_token"] == "app-token-abc"


def test_a_service_moved_to_another_host_without_its_key_keeps_the_key_at_home(engine):
    """The stored key goes only to the address it was entered for — the same
    promise the service form keeps. A backup is a file anybody can edit."""
    data = exported(engine)
    data["services"][0]["url"] = "http://elsewhere.example:7878"
    result = planned(engine, data)
    backup.apply(engine.store, result, engine.rule_settings())

    skipped = {(section, name): key for section, name, key, _f in result.skipped}
    assert skipped[("services", "Radarr")] == "backup.skip_needs_key"
    service = engine.store.services()[0]
    assert (service["url"], service["api_key"]) == ("http://radarr:7878", "radarr-key-123")


def test_a_new_service_without_its_key_is_skipped_and_named(engine, tmp_path):
    target = fresh_engine(tmp_path)
    result = planned(target, exported(engine))
    skipped = {(section, name): key for section, name, key, _f in result.skipped}
    assert skipped[("services", "Radarr")] == "backup.skip_needs_key"
    assert skipped[("notifications", "Phone")] == "backup.skip_needs_key"
    # The settings and rules still go ahead.
    assert result.settings["fast_seconds"] == 45


# ------------------------------------------------------------ what is refused
def test_something_that_is_not_a_backup_is_refused(engine):
    with pytest.raises(ValueError, match="error.backup_unreadable"):
        planned(engine, {"settings": {}})
    with pytest.raises(ValueError, match="error.backup_unreadable"):
        planned(engine, ["not", "a", "backup"])


def test_a_backup_from_a_newer_format_is_refused(engine):
    data = exported(engine)
    data["format_version"] = backup.FORMAT_VERSION + 1
    with pytest.raises(ValueError, match="error.backup_too_new"):
        planned(engine, data)


def test_an_invalid_value_is_skipped_and_the_rest_goes_ahead(engine):
    data = exported(engine)
    data["settings"]["fast_seconds"] = 1          # below the minimum
    data["settings"]["deep_minutes"] = 120
    data["settings"]["from_a_newer_build"] = True
    data["rules"]["stalled"]["action"] = "delete"  # not something it may do
    data["rules"]["gone_rule"] = {"action": "report"}
    result = planned(engine, data)

    reasons = {(section, name): key for section, name, key, _f in result.skipped}
    assert reasons[("settings", "fast_seconds")] == "backup.skip_invalid"
    assert reasons[("settings", "from_a_newer_build")] == "backup.skip_unknown"
    assert reasons[("rules", "stalled")] == "backup.skip_invalid"
    assert reasons[("rules", "gone_rule")] == "backup.skip_unknown"
    assert result.settings == {"deep_minutes": 120}


def test_a_restore_never_deletes_what_the_backup_does_not_mention(engine):
    data = exported(engine)
    data["services"] = []
    data["notifications"] = []
    result = planned(engine, data)
    backup.apply(engine.store, result, engine.rule_settings())
    assert len(engine.store.services()) == 1
    assert len(engine.store.notifications()) == 1


def test_a_notification_filter_naming_an_unknown_rule_drops_only_that_rule(engine):
    data = exported(engine)
    data["notifications"][0]["rules"] = ["stray_files", "rule_from_the_future"]
    data["notifications"][0]["min_severity"] = "catastrophic"
    result = planned(engine, data)
    written = result.connections[0]
    assert written["rules"] == ["stray_files"]
    assert written["min_severity"] == "warning"


# ------------------------------------------------------------ through the API
@pytest.fixture
def client(engine, monkeypatch):
    monkeypatch.setattr(core, "store", engine.store)
    monkeypatch.setattr(core, "engine", engine)
    monkeypatch.setattr(jobs.scheduler, "start", lambda *a, **k: None)
    monkeypatch.setattr(jobs.scheduler, "shutdown", lambda *a, **k: None)
    monkeypatch.setattr(jobs, "schedule", lambda: None)
    monkeypatch.setattr(engine, "arr_services", lambda: [])
    with TestClient(main_module.app) as test_client:
        test_client.post("/api/auth/setup", json={"name": "tester", "password": PASSPHRASE})
        yield test_client


def test_the_download_is_a_file_without_keys(client):
    response = client.get("/api/backup")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert "radarr-key-123" not in response.text
    assert response.json()["format"] == backup.FORMAT


def test_a_restore_only_says_what_it_would_do_until_told_to(client, engine):
    data = client.get("/api/backup").json()
    data["settings"]["deep_minutes"] = 120
    data["settings"]["fast_seconds"] = 1

    preview = client.post("/api/backup/restore", json={"backup": data}).json()
    assert preview["applied"] is False
    assert preview["count"] == 1
    assert preview["changes"]["settings"] == [
        {"key": "deep_minutes", "from": 90, "to": 120}]
    assert preview["skipped"][0]["name"] == "fast_seconds"
    assert "fast_seconds" not in preview["skipped"][0]["reason"]
    assert engine.config()["deep_minutes"] == 90

    done = client.post("/api/backup/restore", json={"backup": data, "apply": True}).json()
    assert done["applied"] is True
    assert engine.config()["deep_minutes"] == 120


def test_a_file_that_is_not_a_backup_is_refused_in_words(client):
    response = client.post("/api/backup/restore", json={"backup": {"hello": 1}})
    assert response.status_code == 400
    assert "error." not in response.json()["detail"]


@pytest.mark.parametrize("method,path", [
    ("get", "/api/backup"), ("post", "/api/backup/restore")])
def test_backups_need_a_session(client, method, path):
    client.cookies.clear()
    response = getattr(client, method)(path, **({"json": {"backup": {}}}
                                                if method == "post" else {}))
    assert response.status_code == 401
