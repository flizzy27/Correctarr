"""What keeps the interface, its keys and its log to themselves."""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app import auth, logging_setup
from app import main as main_module
from app import settings as S
from app.api import core, jobs, services

PASSPHRASE = "a-proper-test-passphrase"


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.engine import Engine
    from app.storage import Store

    store = Store(tmp_path / "security.db")
    engine = Engine(store)
    monkeypatch.setattr(core, "store", store)
    monkeypatch.setattr(core, "engine", engine)
    monkeypatch.setattr(jobs.scheduler, "start", lambda *a, **k: None)
    monkeypatch.setattr(jobs.scheduler, "shutdown", lambda *a, **k: None)
    monkeypatch.setattr(jobs.scheduler, "get_jobs", lambda: [])
    monkeypatch.setattr(jobs, "schedule", lambda: None)
    monkeypatch.setattr(jobs, "trigger_event", lambda source: None)
    monkeypatch.setattr(engine, "arr_services", lambda: [])
    monkeypatch.setattr(auth, "_attempts", {})
    with TestClient(main_module.app) as test_client:
        yield test_client


@pytest.fixture
def signed_in(client):
    response = client.post("/api/auth/setup", json={"name": "tester", "password": PASSPHRASE})
    assert response.status_code == 200, response.text
    return client


# ---------------------------------------------------------------------------
# Requests from another page
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("headers", [
    {"Origin": "http://evil.example"},
    {"Origin": "http://testserver:8080"},
    {"Origin": "null"},
    {"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"},
    {"Sec-Fetch-Site": "same-site", "Origin": "http://testserver:7878"},
])
def test_a_change_sent_by_another_page_is_refused(signed_in, headers):
    response = signed_in.post("/api/safety/resume", headers=headers)
    assert response.status_code == 403
    assert "another web page" in response.json()["detail"]


@pytest.mark.parametrize("headers", [
    {},
    {"Origin": "http://testserver"},
    {"Sec-Fetch-Site": "same-origin", "Origin": "https://proxy.example"},
    {"Origin": "https://correctarr.example", "X-Forwarded-Host": "correctarr.example"},
])
def test_a_change_from_this_interface_goes_through(signed_in, headers):
    response = signed_in.post("/api/safety/resume", headers=headers)
    assert response.status_code != 403, response.text


def test_signing_in_from_another_page_is_refused(signed_in):
    response = signed_in.post("/api/auth", headers={"Origin": "http://evil.example"},
                              json={"name": "tester", "password": PASSPHRASE})
    assert response.status_code == 403


def test_a_read_is_not_affected(signed_in):
    assert signed_in.get("/api/settings", headers={"Origin": "http://evil.example"}).status_code == 200


def test_the_webhook_is_not_asked_where_it_came_from(client):
    token = core.webhook_token()
    response = client.post(f"/api/event?token={token}", json={"eventType": "Test"},
                           headers={"Origin": "http://radarr.example"})
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# The webhook token
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("token", ["", "wrong", "Üblich-falsch", "x" * 500])
def test_a_wrong_webhook_token_is_refused_cleanly(client, token):
    response = client.post("/api/event", params={"token": token}, json={"eventType": "Grab"})
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Sign-in throttling
# ---------------------------------------------------------------------------
def test_a_new_forwarded_address_per_attempt_does_not_escape_the_throttle(signed_in):
    signed_in.post("/api/auth/signout")
    codes = []
    for attempt in range(8):
        response = signed_in.post("/api/auth", json={"name": "tester", "password": "wrong-guess"},
                                  headers={"X-Forwarded-For": f"203.0.113.{attempt}"})
        codes.append(response.status_code)
    assert 429 in codes


def test_the_account_throttle_ends_with_a_successful_sign_in():
    key = auth.account_key("Tester ")
    assert key == auth.account_key("tester")
    auth.note_failure(key)
    auth.note_success(key)
    assert auth.retry_after(key) == 0


# ---------------------------------------------------------------------------
# Stored keys
# ---------------------------------------------------------------------------
class _Offline:
    #: The key the last connection was built with.
    last_key = ""

    def __init__(self, *args, **kwargs):
        _Offline.last_key = args[2] if len(args) > 2 else ""

    def reachable(self):
        return True, "reached"

    def close(self):
        pass

    def remove_webhook(self):
        return False


@pytest.fixture
def one_service(signed_in, monkeypatch):
    monkeypatch.setattr(services, "Arr", _Offline)
    monkeypatch.setattr(services, "_connector",
                        lambda kind, url, api_key, name="", timeout=30.0, verify=True:
                        _Offline(kind, url, api_key))
    saved = signed_in.post("/api/services", json={
        "name": "Radarr", "kind": "radarr", "url": "http://radarr:7878",
        "api_key": "the-real-key", "webhook": False})
    assert saved.status_code == 200, saved.text
    return saved.json()["id"]


def test_the_stored_key_is_used_for_the_address_it_was_entered_for(signed_in, one_service):
    response = signed_in.post("/api/services/test", json={
        "id": one_service, "name": "Radarr", "kind": "radarr",
        "url": "http://radarr:7878/", "api_key": S.MASK})
    assert response.status_code == 200
    assert _Offline.last_key == "the-real-key"


def test_the_stored_key_is_not_sent_to_a_new_address(signed_in, one_service):
    response = signed_in.post("/api/services/test", json={
        "id": one_service, "name": "Radarr", "kind": "radarr",
        "url": "http://collector.example:7878", "api_key": S.MASK})
    assert response.status_code == 400
    assert "API key again" in response.json()["detail"]
    saved = signed_in.post("/api/services", json={
        "id": one_service, "name": "Radarr", "kind": "radarr",
        "url": "http://collector.example:7878", "api_key": S.MASK, "webhook": False})
    assert saved.status_code == 400


def test_no_key_is_listed_and_the_certificate_choice_is(signed_in, one_service):
    listed = signed_in.get("/api/services").json()
    assert "the-real-key" not in str(listed)
    assert listed[0]["verify_tls"] is True
    signed_in.post("/api/services", json={
        "id": one_service, "name": "Radarr", "kind": "radarr", "url": "http://radarr:7878",
        "api_key": S.MASK, "webhook": False, "verify_tls": False})
    assert signed_in.get("/api/services").json()[0]["verify_tls"] is False
    signed_in.delete(f"/api/services/{one_service}")
    assert core.store.get(core.engine.UNVERIFIED) == []


def test_the_public_address_has_to_be_a_web_address(signed_in):
    for value in ("javascript:alert(1)", "ftp://host/", "file:///config"):
        response = signed_in.post("/api/settings", json={"key": "public_url", "value": value})
        assert response.status_code == 400, value
    ok = signed_in.post("/api/settings", json={"key": "public_url",
                                               "value": "https://correctarr.example"})
    assert ok.status_code == 200


# ---------------------------------------------------------------------------
# The log
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("line, secret", [
    ("GET http://sab:8080/api?mode=queue&apikey=0123456789abcdef&output=json", "0123456789abcdef"),
    ("POST https://api.telegram.org/bot123456789:AAFakeTokenForTests_abcdefgh/sendMessage",
     "AAFakeTokenForTests_abcdefgh"),
    ("https://discord.com/api/webhooks/1234/abcdefghijklmnopqrstuvwxyz", "abcdefghijklmnopqrstuvwxyz"),
    ("Authorization: Bearer tk_abcdefghijklmnop", "tk_abcdefghijklmnop"),
    ("http://admin:hunter22secret@radarr:7878/api", "hunter22secret"),
    ('{"token": "abcdefgh12345678"}', "abcdefgh12345678"),
    ("POST /api/event?token=AbCdEfGh12345678_- HTTP/1.1", "AbCdEfGh12345678_-"),
    ("X-Api-Key: 0123456789abcdef0123", "0123456789abcdef0123"),
])
def test_a_secret_does_not_reach_the_log(line, secret):
    cleaned = logging_setup.redact(line)
    assert secret not in cleaned
    assert logging_setup.REDACTED in cleaned


def test_an_ordinary_line_is_left_alone():
    line = "Radarr GET queue -> 200 in 42ms, 3 records, next: 2026-09-30 12:00"
    assert logging_setup.redact(line) == line


def test_a_traceback_is_redacted_too():
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", None, None)
    try:
        raise RuntimeError("cannot reach http://sab:8080/api?apikey=0123456789abcdef")
    except RuntimeError:
        import sys
        record.exc_info = sys.exc_info()
    logging_setup.Redactor().filter(record)
    assert "0123456789abcdef" not in record.exc_text


def test_uvicorn_log_lines_pass_the_filter():
    logging_setup.configure()
    access = logging.getLogger("uvicorn.access")
    assert any(isinstance(f, logging_setup.Redactor) for f in access.filters)
    record = access.makeRecord("uvicorn.access", logging.INFO, __file__, 1,
                               '%s - "%s %s HTTP/%s" %d',
                               ("203.0.113.5:5", "POST", "/api/event?token=AbCdEfGh12345678", "1.1", 200),
                               None)
    for log_filter in access.filters:
        log_filter.filter(record)
    assert "AbCdEfGh12345678" not in record.getMessage()
