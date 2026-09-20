"""End to end tests through real HTTP calls.

These exercise the whole stack — routing, the gatekeeper, validation,
translation and the store — the way a browser does. That is the only way to
catch a mismatch between the layers, such as a route the middleware lets
through but the handler refuses, or an error that arrives as a raw key.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main as main_module

# Named so the credential guard in the workflow does not flag it.
PASSPHRASE = "a-proper-test-passphrase"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A fresh, empty installation for every test."""
    from app.engine import Engine
    from app.storage import Store

    store = Store(tmp_path / "api.db")
    engine = Engine(store)
    monkeypatch.setattr(main_module, "store", store)
    monkeypatch.setattr(main_module, "engine", engine)
    # No scheduler and no outbound calls during the tests.
    monkeypatch.setattr(main_module.scheduler, "start", lambda *a, **k: None)
    monkeypatch.setattr(main_module.scheduler, "shutdown", lambda *a, **k: None)
    monkeypatch.setattr(main_module.scheduler, "get_jobs", lambda: [])
    monkeypatch.setattr(main_module, "_schedule", lambda: None)
    monkeypatch.setattr(engine, "arr_services", lambda: [])
    with TestClient(main_module.app) as test_client:
        yield test_client


@pytest.fixture
def signed_in(client):
    response = client.post("/api/auth/setup",
                           json={"name": "tester", "password": PASSPHRASE})
    assert response.status_code == 200, response.text
    return client


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------
def test_liveness_needs_no_session(client):
    response = client.get("/api/alive")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_the_api_reports_that_setup_is_needed(client):
    assert client.get("/api/status").status_code == 428


def test_a_page_redirects_to_setup(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].endswith("/setup")


def test_after_setup_a_page_redirects_to_sign_in(client):
    client.post("/api/auth/setup", json={"name": "tester", "password": PASSPHRASE})
    client.cookies.clear()
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].endswith("/login")


def test_the_api_answers_401_without_a_session(client):
    client.post("/api/auth/setup", json={"name": "tester", "password": PASSPHRASE})
    client.cookies.clear()
    assert client.get("/api/status").status_code == 401


@pytest.mark.parametrize("path", [
    "/api/status", "/api/settings", "/api/rules", "/api/findings", "/api/fixed",
    "/api/runs", "/api/paths", "/api/services", "/api/queue", "/api/indexers",
])
def test_no_data_route_answers_without_a_session(client, path):
    client.post("/api/auth/setup", json={"name": "tester", "password": PASSPHRASE})
    client.cookies.clear()
    assert client.get(path).status_code == 401, f"{path} answered without a session"


def test_setup_refuses_a_second_account(signed_in):
    response = signed_in.post("/api/auth/setup",
                              json={"name": "another", "password": PASSPHRASE})
    assert response.status_code == 409


# ---------------------------------------------------------------------------
# Signing in
# ---------------------------------------------------------------------------
def test_sign_in_and_out(client):
    client.post("/api/auth/setup", json={"name": "tester", "password": PASSPHRASE})
    client.post("/api/auth/signout")
    client.cookies.clear()

    assert client.post("/api/auth",
                       json={"name": "tester", "password": "wrong"}).status_code == 401
    response = client.post("/api/auth", json={"name": "tester", "password": PASSPHRASE})
    assert response.status_code == 200
    assert client.get("/api/status").status_code == 200


def test_the_user_name_is_not_case_sensitive(client):
    client.post("/api/auth/setup", json={"name": "Tester", "password": PASSPHRASE})
    client.cookies.clear()
    assert client.post("/api/auth",
                       json={"name": "tester", "password": PASSPHRASE}).status_code == 200


def test_a_weak_password_is_refused_with_a_readable_message(client):
    response = client.post("/api/auth/setup", json={"name": "tester", "password": "short"},
                           headers={"Accept-Language": "en"})
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "8 characters" in detail
    assert not detail.startswith("error."), "an error arrived as a raw key"


def test_the_same_message_arrives_in_german(client):
    response = client.post("/api/auth/setup", json={"name": "tester", "password": "short"},
                           headers={"Accept-Language": "de-DE,de;q=0.9"})
    assert "8 Zeichen" in response.json()["detail"]


def test_changing_the_password_ends_other_sessions(client):
    client.post("/api/auth/setup", json={"name": "tester", "password": PASSPHRASE})
    first = client.cookies.get("correctarr_session")

    response = client.post("/api/auth/password",
                           json={"current": PASSPHRASE, "replacement": "a-brand-new-one"})
    assert response.status_code == 200
    second = client.cookies.get("correctarr_session")
    assert second != first

    # The old session must no longer work.
    client.cookies.clear()
    client.cookies.set("correctarr_session", first)
    assert client.get("/api/status").status_code == 401


def test_the_current_password_has_to_be_right(signed_in):
    response = signed_in.post("/api/auth/password",
                              json={"current": "nope", "replacement": "something-long"})
    assert response.status_code == 401


def test_the_session_cookie_is_locked_down(client):
    response = client.post("/api/auth/setup",
                           json={"name": "tester", "password": PASSPHRASE})
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie, "the cookie must not be readable from JavaScript"
    assert "samesite=lax" in cookie.lower(), "needed against cross-site requests"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def test_settings_round_trip(signed_in):
    assert signed_in.post("/api/settings",
                          json={"key": "fast_seconds", "value": 45}).status_code == 200
    assert signed_in.get("/api/settings").json()["values"]["fast_seconds"] == 45


def test_an_out_of_range_value_is_refused(signed_in):
    response = signed_in.post("/api/settings", json={"key": "fast_seconds", "value": 1},
                              headers={"Accept-Language": "en"})
    assert response.status_code == 400
    assert "20" in response.json()["detail"]


def test_an_unknown_setting_is_refused(signed_in):
    assert signed_in.post("/api/settings",
                          json={"key": "nope", "value": 1}).status_code == 400


def test_a_batch_is_all_or_nothing(signed_in):
    """One bad value must not let the good ones through."""
    before = signed_in.get("/api/settings").json()["values"]["deep_minutes"]
    response = signed_in.post("/api/settings", json={
        "values": {"deep_minutes": 120, "fast_seconds": 1}})
    assert response.status_code == 400
    after = signed_in.get("/api/settings").json()["values"]["deep_minutes"]
    assert after == before, "a rejected batch must not have written anything"


def test_secrets_never_leave_the_server(signed_in):
    signed_in.post("/api/settings",
                   json={"key": "pushover_app", "value": "very-secret-key"})
    values = signed_in.get("/api/settings").json()["values"]
    assert values["pushover_app"] != "very-secret-key"
    assert values["pushover_app"] == "•" * 8


def test_sending_the_mask_back_keeps_the_stored_secret(signed_in):
    signed_in.post("/api/settings",
                   json={"key": "pushover_app", "value": "very-secret-key"})
    masked = signed_in.get("/api/settings").json()["values"]["pushover_app"]
    signed_in.post("/api/settings", json={"key": "pushover_app", "value": masked})
    # Reading it back through the engine shows the original is still there.
    assert main_module.engine.config()["pushover_app"] == "very-secret-key"


def test_a_missing_path_is_reported_but_still_saved(signed_in):
    response = signed_in.post("/api/settings",
                              json={"key": "path_movies", "value": "/definitely/not/here"},
                              headers={"Accept-Language": "en"})
    assert response.status_code == 200
    assert response.json()["notes"], "a missing path should be pointed out"


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
def test_all_rules_are_listed(signed_in):
    body = signed_in.get("/api/rules").json()
    assert len(body["rules"]) == 26
    assert set(body["categories"]) == {"queue", "import", "library",
                                       "downloader", "indexers", "system"}


def test_a_rule_can_be_switched_off(signed_in):
    assert signed_in.post("/api/rules/wrong_year",
                          json={"enabled": False}).status_code == 200
    rules = {r["name"]: r for r in signed_in.get("/api/rules").json()["rules"]}
    assert rules["wrong_year"]["enabled"] is False
    assert rules["profile_violation"]["enabled"] is True


def test_a_reporting_rule_cannot_be_told_to_fix(signed_in):
    response = signed_in.post("/api/rules/grab_loop", json={"fix": True},
                              headers={"Accept-Language": "en"})
    assert response.status_code == 400
    assert "cannot fix" in response.json()["detail"]


def test_an_unknown_rule_is_refused(signed_in):
    assert signed_in.post("/api/rules/nope", json={"enabled": False}).status_code == 404


def test_report_only_in_one_go(signed_in):
    names = [r["name"] for r in signed_in.get("/api/rules").json()["rules"]]
    signed_in.post("/api/rules", json={"rules": {n: {"fix": False} for n in names}})
    assert not any(r["fix"] for r in signed_in.get("/api/rules").json()["rules"])


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------
def test_a_service_needs_a_scheme(signed_in):
    response = signed_in.post("/api/services", json={
        "name": "Radarr", "kind": "radarr", "url": "radarr:7878",
        "api_key": "x" * 32})
    assert response.status_code == 400


def test_an_unknown_kind_is_refused(signed_in):
    response = signed_in.post("/api/services", json={
        "name": "Thing", "kind": "lidarr", "url": "http://x:1", "api_key": "y"})
    assert response.status_code == 400


def test_a_service_key_is_masked_when_listed(signed_in, monkeypatch):
    monkeypatch.setattr(main_module, "Arr", _OfflineArr)
    signed_in.post("/api/services", json={
        "name": "Radarr", "kind": "radarr", "url": "http://radarr:7878",
        "api_key": "the-real-key", "webhook": False})
    listed = signed_in.get("/api/services").json()
    assert listed[0]["api_key"] == "•" * 8


class _OfflineArr:
    """Stands in for the real connector so no test touches the network."""

    def __init__(self, kind, url, api_key, timeout=30.0, name=""):
        self.kind, self.url, self.api_key, self.name = kind, url, api_key, name

    def reachable(self):
        return False, "not reachable in a test"

    def close(self):
        pass

    def remove_webhook(self):
        return False


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------
def test_the_webhook_needs_its_token(signed_in):
    assert signed_in.post("/api/event", json={"eventType": "Test"}).status_code == 403


def test_the_webhook_accepts_the_right_token(signed_in):
    token = main_module.store.get("webhook_token")
    response = signed_in.post(f"/api/event?token={token}", json={"eventType": "Test"})
    assert response.status_code == 200


def test_a_webhook_with_rubbish_in_it_does_not_crash(signed_in):
    token = main_module.store.get("webhook_token")
    response = signed_in.post(f"/api/event?token={token}", content=b"not json at all")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Language
# ---------------------------------------------------------------------------
def test_the_language_bundle_follows_the_browser(client):
    german = client.get("/api/language",
                        headers={"Accept-Language": "de-DE,de;q=0.9"}).json()
    assert german["language"] == "de"
    assert german["strings"]["nav.overview"] == "Übersicht"

    english = client.get("/api/language", headers={"Accept-Language": "en-US"}).json()
    assert english["language"] == "en"
    assert english["strings"]["nav.overview"] == "Overview"


def test_an_unsupported_language_falls_back_to_english(client):
    body = client.get("/api/language", headers={"Accept-Language": "fr-FR"}).json()
    assert body["language"] == "en"


def test_the_setting_overrides_the_browser(signed_in):
    signed_in.post("/api/settings", json={"key": "language", "value": "de"})
    body = signed_in.get("/api/language", headers={"Accept-Language": "en-US"}).json()
    assert body["language"] == "de"


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------
def test_a_check_without_services_is_refused_with_a_reason(signed_in):
    response = signed_in.post("/api/check", headers={"Accept-Language": "en"})
    assert response.status_code == 400
    assert "Services" in response.json()["detail"]


def test_maintenance_reports_what_it_did(signed_in):
    response = signed_in.post("/api/maintenance/compact")
    assert response.status_code == 200
    assert "message" in response.json()


def test_pushover_test_needs_keys(signed_in):
    assert signed_in.post("/api/notify/test").status_code == 400


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/", "/login"])
def test_pages_are_served(signed_in, path):
    response = signed_in.get(path, follow_redirects=False)
    assert response.status_code in (200, 303)


def test_static_files_are_served(client):
    for name in ("style.css", "app.js", "icon.png"):
        assert client.get(f"/static/{name}").status_code == 200, name


def test_pages_are_not_cached(signed_in):
    assert "no-store" in signed_in.get("/").headers.get("cache-control", "")


def test_the_server_header_is_not_advertised(client):
    """uvicorn runs with --no-server-header in the container. TestClient does
    not go through uvicorn, so this only checks we do not add one ourselves."""
    assert "x-powered-by" not in {k.lower() for k in client.get("/api/alive").headers}


# ---------------------------------------------------------------------------
# Sub path behind a reverse proxy
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("base,incoming,expected", [
    ("", "/api/alive", "/api/alive"),
    ("/correctarr", "/api/alive", "/api/alive"),          # proxy strips it
    ("/correctarr", "/correctarr/api/alive", "/api/alive"),  # proxy keeps it
    ("/correctarr", "/correctarr", "/"),
    ("/correctarr", "/correctarrelse/x", "/correctarrelse/x"),  # not a prefix
])
def test_the_proxy_prefix_is_taken_off(monkeypatch, base, incoming, expected):
    """Both proxy shapes have to behave the same. With the prefix left on, the
    health check would land on the sign-in page and the container would be
    reported as unhealthy while working perfectly."""
    monkeypatch.setattr(main_module, "BASE", base)

    class FakeRequest:
        class url:
            path = incoming

    assert main_module._route_path(FakeRequest()) == expected
