"""Correctarr — web interface, schedule and API.

Work happens three ways:

  1. **Event** — Radarr and Sonarr report through a webhook the moment they grab
     something or need manual work. Reaction within seconds.
  2. **Fast** — a short interval for the queue rules.
  3. **Deep** — less often, additionally walking the library and the indexers.

Behind a reverse proxy
----------------------
``BASE_URL`` sets the sub path the interface is reachable under, for example
``/correctarr``. The interface itself uses relative URLs throughout and works
without that setting as long as the proxy maps to the root. uvicorn runs with
``--proxy-headers`` so ``X-Forwarded-Proto`` is honoured — without it the
service would treat an HTTPS connection as plain and set the session cookie
without ``Secure``.
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, i18n, logging_setup
from . import settings as S
from .arr import Arr, ArrError
from .engine import Engine
from .prowlarr import Prowlarr
from .rules import ALL, BY_NAME, CATEGORIES
from .sab import Sab
from .storage import Store

logging_setup.configure()
log = logging.getLogger("correctarr")

HERE = Path(__file__).parent
CONFIG_DIR = Path(os.getenv("CONFIG_DIR", "/config"))
VERSION = os.getenv("VERSION", "dev")
BUILT_AT = os.getenv("BUILT_AT", "unknown")
COMMIT = os.getenv("COMMIT", "unknown")
BASE = "/" + (os.getenv("BASE_URL", "").strip().strip("/"))
BASE = "" if BASE == "/" else BASE


def _adopt_previous_store() -> None:
    """Keep using a store written under the previous name.

    Anyone running an earlier build should keep their settings, services and
    history rather than starting from nothing.

    This has to happen **before** the store is constructed. Run later, the store
    would already have created the new file, the existence check would pass, and
    the adoption would be skipped silently — leaving an existing user in front
    of an empty setup screen. That is exactly what happened in testing.
    """
    new = CONFIG_DIR / "correctarr.db"
    old = CONFIG_DIR / "radarr-fixer.db"
    if new.exists() or not old.exists():
        return
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            source = Path(str(old) + suffix)
            if source.exists():
                source.replace(Path(str(new) + suffix))
        log.info("Adopted the existing store: %s -> %s", old.name, new.name)
    except OSError as e:
        log.warning("Could not adopt the previous store: %s", e)


_adopt_previous_store()

store = Store(CONFIG_DIR / "correctarr.db")
engine = Engine(store)
scheduler = BackgroundScheduler(timezone=os.getenv("TZ", "Etc/UTC"))

_event_lock = threading.Lock()
_last_event = 0.0

# Paths that have to work without a session.
OPEN_PATHS = {"/api/alive", "/api/auth", "/api/auth/state", "/api/auth/setup",
              "/api/language", "/login", "/setup", "/api/event"}


def webhook_token() -> str:
    """The secret in the webhook URL.

    Radarr calls in without a session, so the path has to be open. Open does not
    mean unprotected: without this token nobody triggers runs from outside.
    """
    token = store.get("webhook_token")
    if not token:
        token = secrets.token_urlsafe(24)
        store.set("webhook_token", token)
    return token


def _seed_services() -> None:
    """On the very first start, take services from the environment.

    Convenience for anyone deploying with compose. The truth lives in the
    database afterwards and is maintained through the interface.
    """
    if store.services():
        return
    pairs = (("radarr", "RADARR"), ("sonarr", "SONARR"),
             ("sabnzbd", "SAB"), ("prowlarr", "PROWLARR"))
    labels = {"sabnzbd": "SABnzbd", "prowlarr": "Prowlarr"}
    for kind, prefix in pairs:
        url = os.getenv(f"{prefix}_URL")
        api_key = os.getenv(f"{prefix}_API_KEY")
        if not (url and api_key):
            continue
        store.save_service({
            "name": labels.get(kind, kind.capitalize()), "kind": kind,
            "url": url, "api_key": api_key, "enabled": True,
            "webhook": kind in ("radarr", "sonarr")})
        log.info("Adopted the %s service from the environment", kind)


def _schedule() -> None:
    cfg = engine.config()
    scheduler.add_job(lambda: engine.run(deep=False), "interval",
                      seconds=max(20, int(cfg.get("fast_seconds", 60))),
                      id="fast", replace_existing=True, max_instances=1,
                      coalesce=True, misfire_grace_time=30)
    scheduler.add_job(lambda: engine.run(deep=True), "interval",
                      minutes=max(5, int(cfg.get("deep_minutes", 90))),
                      id="deep", replace_existing=True, max_instances=1,
                      coalesce=True, misfire_grace_time=300)
    log.info("Schedule: fast every %ss, deep every %s minutes",
             cfg.get("fast_seconds"), cfg.get("deep_minutes"))


def _trigger_event(source: str) -> None:
    """Start a fast run soon, at most every few seconds."""
    global _last_event
    cfg = engine.config()
    if not cfg.get("events_enabled", True):
        return
    with _event_lock:
        now = time.monotonic()
        if now - _last_event < float(cfg.get("event_debounce", 8)):
            return
        _last_event = now
    log.info("Event from %s — checking now", source)
    threading.Thread(target=lambda: engine.run(deep=False, trigger=source),
                     daemon=True).start()


@asynccontextmanager
async def lifespan(_: FastAPI):
    _seed_services()
    webhook_token()
    store.prune_sessions()
    _schedule()
    scheduler.start()
    log.info("Correctarr %s ready — %d service(s), auth %s%s",
             VERSION, len(store.services()), auth.mode(),
             f", sub path {BASE}" if BASE else "")
    if auth.mode() == "off":
        log.warning("Authentication is disabled (AUTH=off). The interface is "
                    "reachable by anyone who can reach the port.")
    missing = i18n.missing_keys()
    for code, keys in missing.items():
        if keys:
            log.warning("Locale %s is missing %d keys, e.g. %s",
                        code, len(keys), ", ".join(keys[:3]))
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Correctarr", version=VERSION, lifespan=lifespan,
              root_path=BASE, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------
def _is_https(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0]
    return forwarded.strip() == "https"


def _origin(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:60]
    return (request.client.host if request.client else "?")[:60]


def language_for(request: Request) -> str:
    return i18n.resolve(store.get("language", "auto"),
                        request.headers.get("accept-language"))


def current_user(request: Request) -> dict | None:
    if auth.mode() == "off":
        return {"id": 0, "name": "open"}
    token = request.cookies.get(auth.COOKIE)
    if not token:
        return None
    session = store.session(auth.digest(token))
    if not session:
        return None
    return store.user_by_id(session["user_id"])


def _is_set_up() -> bool:
    return auth.mode() == "off" or store.user_count() > 0


@app.middleware("http")
async def gatekeeper(request: Request, call_next):
    path = request.url.path
    if path.startswith("/static") or path in OPEN_PATHS:
        return await call_next(request)

    if not _is_set_up():
        if path.startswith("/api/"):
            return JSONResponse({"error": "not set up", "setup": True}, status_code=428)
        if path != "/setup":
            return RedirectResponse(f"{BASE}/setup", status_code=303)
        return await call_next(request)

    if current_user(request) is None:
        if path.startswith("/api/"):
            return JSONResponse({"error": "not signed in"}, status_code=401)
        return RedirectResponse(f"{BASE}/login", status_code=303)
    return await call_next(request)


def require_user(request: Request) -> dict:
    user = current_user(request)
    if user is None:
        raise HTTPException(401, "Not signed in")
    return user


def _fail(request: Request, status: int, key: str, **fields) -> HTTPException:
    """An error the interface can show in the user's own language."""
    return HTTPException(status, i18n.t(key, language_for(request), **fields))


def _fail_from_value_error(request: Request, error: ValueError) -> HTTPException:
    """Validation errors carry ``key|arg|arg`` so they can be translated."""
    parts = str(error).split("|")
    key = parts[0]
    language = language_for(request)
    if key.startswith("error.") and len(parts) > 1:
        label = i18n.t(f"settings.{parts[1]}.label", language)
        extra = parts[2] if len(parts) > 2 else ""
        return HTTPException(400, i18n.t(key, language, field=label, value=extra))
    return HTTPException(400, i18n.t(key, language))


def _set_cookie(response: Response, token: str, request: Request) -> None:
    response.set_cookie(
        auth.COOKIE, token, max_age=auth.SESSION_DAYS * 86400,
        httponly=True, samesite="lax", secure=_is_https(request),
        path=BASE or "/")


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
def _page(name: str) -> FileResponse:
    return FileResponse(HERE / "templates" / name,
                        headers={"Cache-Control": "no-store"})


@app.get("/")
def index():
    return _page("index.html")


@app.get("/login")
def login_page():
    if not _is_set_up():
        return RedirectResponse(f"{BASE}/setup", status_code=303)
    return _page("login.html")


@app.get("/setup")
def setup_page():
    if _is_set_up():
        return RedirectResponse(f"{BASE}/", status_code=303)
    return _page("setup.html")


# ---------------------------------------------------------------------------
# Liveness — deliberately without any outbound call
# ---------------------------------------------------------------------------
@app.get("/api/alive")
def alive():
    """For the container health check.

    Calls nothing outward. The previous check hung off the status endpoint, and
    that queries every configured service — a service that swallows packets
    instead of refusing them would have had the container reported as unhealthy
    while it was working perfectly.
    """
    return {"ok": True, "version": VERSION}


@app.get("/api/language")
def language(request: Request):
    """Strings for the interface, in the language for this request."""
    code = language_for(request)
    return {"language": code, "available": list(i18n.AVAILABLE),
            "names": i18n.LANGUAGE_NAMES, "strings": i18n.bundle(code)}


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
class Credentials(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


@app.get("/api/auth/state")
def auth_state(request: Request):
    user = current_user(request)
    return {"set_up": _is_set_up(), "mode": auth.mode(),
            "signed_in": user is not None,
            "user": user["name"] if user else None,
            "version": VERSION}


@app.post("/api/auth/setup")
def auth_setup(body: Credentials, request: Request, response: Response):
    """Create the first account. Afterwards this path is closed."""
    if _is_set_up():
        raise _fail(request, 409, "error.already_set_up")
    if (problem := auth.username_problem(body.name)):
        raise _fail(request, 400, problem)
    if (problem := auth.password_problem(body.password, body.name)):
        raise _fail(request, 400, problem, min=auth.MIN_PASSWORD_LENGTH)
    user_id = store.create_user(body.name, auth.hash_password(body.password))
    token, token_digest = auth.new_token()
    store.create_session(token_digest, user_id, auth.SESSION_DAYS, _origin(request))
    _set_cookie(response, token, request)
    log.info("First account created: %s", body.name)
    return {"ok": True, "user": body.name}


@app.post("/api/auth")
def sign_in(body: Credentials, request: Request, response: Response):
    origin = _origin(request)
    if (wait := auth.retry_after(origin)):
        raise _fail(request, 429, "error.too_many_attempts", seconds=wait)

    user = store.user_by_name(body.name)
    # The hash is computed even without a match, otherwise the response time
    # reveals whether the user name exists.
    stored_hash = user["hash"] if user else auth.hash_password("no-such-user")
    correct = auth.verify_password(body.password, stored_hash)

    if not (user and correct):
        auth.note_failure(origin)
        log.warning("Failed sign-in for %r from %s", body.name, origin)
        raise _fail(request, 401, "error.bad_credentials")

    auth.note_success(origin)
    if auth.needs_rehash(stored_hash):
        store.set_user_hash(user["id"], auth.hash_password(body.password))
        log.info("Upgraded the password hash for %s", user["name"])
    store.note_login(user["id"])
    token, token_digest = auth.new_token()
    store.create_session(token_digest, user["id"], auth.SESSION_DAYS, origin)
    _set_cookie(response, token, request)
    return {"ok": True, "user": user["name"]}


@app.post("/api/auth/signout")
def sign_out(request: Request, response: Response):
    token = request.cookies.get(auth.COOKIE)
    if token:
        store.end_session(auth.digest(token))
    response.delete_cookie(auth.COOKIE, path=BASE or "/")
    return {"ok": True}


class PasswordChange(BaseModel):
    current: str = Field(min_length=1, max_length=256)
    replacement: str = Field(min_length=1, max_length=256)


@app.post("/api/auth/password")
def change_password(body: PasswordChange, request: Request, response: Response,
                    user: dict = Depends(require_user)):
    if auth.mode() == "off":
        raise _fail(request, 400, "error.auth_disabled")
    full = store.user_by_id(user["id"])
    if not full or not auth.verify_password(body.current, full["hash"]):
        auth.note_failure(_origin(request))
        raise _fail(request, 401, "error.current_password_wrong")
    if (problem := auth.password_problem(body.replacement, full["name"])):
        raise _fail(request, 400, problem, min=auth.MIN_PASSWORD_LENGTH)
    store.set_user_hash(full["id"], auth.hash_password(body.replacement))
    # Every previous session stops being valid, including on other devices.
    # Someone changing their password usually wants exactly that.
    store.end_all_sessions(full["id"])
    token, token_digest = auth.new_token()
    store.create_session(token_digest, full["id"], auth.SESSION_DAYS, _origin(request))
    _set_cookie(response, token, request)
    log.info("Password changed for %s, all other sessions ended", full["name"])
    return {"ok": True, "message": i18n.t("message.password_changed",
                                          language_for(request))}


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------
@app.post("/api/event")
async def event(request: Request, token: str = ""):
    """Receive webhook calls from Radarr and Sonarr."""
    if not secrets.compare_digest(token, webhook_token()):
        log.warning("Webhook with a wrong token from %s", _origin(request))
        raise HTTPException(403, "Bad token")
    try:
        body = await request.json()
    except Exception:                                   # noqa: BLE001
        body = {}
    kind = str(body.get("eventType") or "").lower()
    source = str(body.get("instanceName") or "arr")[:40]
    log.info("Webhook: %s from %s", kind or "?", source)
    if kind == "test":
        return {"ok": True, "message": "Connection works"}
    if kind in ("grab", "manualinteractionrequired", "download", "healthissue"):
        _trigger_event(f"{source}/{kind}")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------
KINDS = ("radarr", "sonarr", "sabnzbd", "prowlarr")


class ServiceBody(BaseModel):
    id: int | None = None
    name: str = Field(min_length=1, max_length=64)
    kind: str = "radarr"
    url: str = Field(min_length=1, max_length=500)
    api_key: str = Field(default="", max_length=200)
    enabled: bool = True
    webhook: bool = True


def _connector(kind: str, url: str, api_key: str, name: str = ""):
    if kind == "sabnzbd":
        return Sab(url, api_key, name=name or "SABnzbd")
    if kind == "prowlarr":
        return Prowlarr(url, api_key, name=name or "Prowlarr")
    return Arr(kind, url, api_key, name=name or kind.capitalize())


def _keep_stored_key(body: ServiceBody) -> ServiceBody:
    """If the mask came back, keep the stored key."""
    if body.id and (not body.api_key or body.api_key == S.MASK):
        existing = store.service(body.id)
        if existing:
            body.api_key = existing["api_key"]
    return body


@app.get("/api/services")
def list_services(_: dict = Depends(require_user)):
    out = []
    for entry in store.services():
        if entry["enabled"]:
            connector = _connector(entry["kind"], entry["url"],
                                   entry["api_key"], entry["name"])
            try:
                ok, info = connector.reachable()
            finally:
                connector.close()
        else:
            ok, info = None, "disabled"
        out.append({**entry, "enabled": bool(entry["enabled"]),
                    "webhook": bool(entry["webhook"]),
                    # The key never leaves the server.
                    "api_key": S.MASK if entry["api_key"] else "",
                    "reachable": ok, "info": info})
    return out


@app.post("/api/services")
def save_service(body: ServiceBody, request: Request, _: dict = Depends(require_user)):
    if body.kind not in KINDS:
        raise _fail(request, 400, "error.unknown_kind", kinds=", ".join(KINDS))
    if not body.url.startswith(("http://", "https://")):
        raise _fail(request, 400, "error.url_scheme")
    body = _keep_stored_key(body)
    if not body.api_key:
        raise _fail(request, 400, "error.api_key_missing")

    service_id = store.save_service(body.model_dump())
    note = ""
    if body.webhook and body.enabled and body.kind in ("radarr", "sonarr"):
        target = (engine.config().get("public_url") or "").rstrip("/")
        if not target:
            note = i18n.t("message.webhook_needs_url", language_for(request))
        else:
            connector = Arr(body.kind, body.url, body.api_key, name=body.name)
            try:
                state = connector.set_webhook(
                    f"{target}{BASE}/api/event?token={webhook_token()}")
                note = i18n.t(f"message.webhook_{state}", language_for(request))
            except ArrError as e:
                note = i18n.t("message.webhook_failed", language_for(request), error=str(e))
            finally:
                connector.close()
    return {"ok": True, "id": service_id, "webhook": note}


@app.delete("/api/services/{service_id}")
def delete_service(service_id: int, request: Request, _: dict = Depends(require_user)):
    entry = store.service(service_id)
    if not entry:
        raise _fail(request, 404, "error.no_such_service")
    if entry["kind"] in ("radarr", "sonarr"):
        connector = Arr(entry["kind"], entry["url"], entry["api_key"], name=entry["name"])
        try:
            connector.remove_webhook()
        except ArrError as e:
            log.info("Could not remove the webhook: %s", e)
        finally:
            connector.close()
    store.delete_service(service_id)
    return {"ok": True}


@app.post("/api/services/test")
def test_service(body: ServiceBody, request: Request, _: dict = Depends(require_user)):
    if body.kind not in KINDS:
        raise _fail(request, 400, "error.unknown_kind", kinds=", ".join(KINDS))
    if not body.url.startswith(("http://", "https://")):
        raise _fail(request, 400, "error.url_scheme")
    body = _keep_stored_key(body)
    connector = _connector(body.kind, body.url, body.api_key, body.name)
    try:
        ok, info = connector.reachable()
    finally:
        connector.close()
    if not ok:
        raise HTTPException(502, info)
    return {"ok": True, "info": info}


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
@app.get("/api/status")
def status(_: dict = Depends(require_user)):
    services = []
    for entry in store.services(enabled_only=True):
        connector = _connector(entry["kind"], entry["url"],
                               entry["api_key"], entry["name"])
        try:
            ok, info = connector.reachable()
        finally:
            connector.close()
        services.append({"name": entry["name"], "kind": entry["kind"],
                         "url": entry["url"], "ok": ok, "info": info})
    recent = store.runs(1)
    jobs = {j.id: (j.next_run_time.isoformat() if j.next_run_time else None)
            for j in scheduler.get_jobs()}
    cfg = engine.config()
    return {
        "version": VERSION, "built_at": BUILT_AT, "commit": COMMIT,
        "base": BASE, "auth": auth.mode(),
        "services": services, "running": engine.running,
        "last_trigger": engine.last_trigger,
        "last_run": recent[0] if recent else None,
        "next_fast": jobs.get("fast"), "next_deep": jobs.get("deep"),
        "summary": store.summary(), "store": store.size(),
        "dry_run": bool(cfg.get("dry_run")),
        "theme": cfg.get("theme", "midnight"),
        "density": cfg.get("density", "normal"),
    }


@app.get("/api/paths")
def paths(request: Request, _: dict = Depends(require_user)):
    """Whether the configured paths are actually there, with plain words.

    The single most common setup mistake: the paths inside the container do not
    match the ones the Arr services see. Otherwise that only surfaces when a
    rule stays quiet although it should have found something.
    """
    cfg = engine.config()
    language = language_for(request)
    out = []
    for key, required in (("path_downloads", True), ("path_incomplete", False),
                          ("path_movies", False), ("path_series", False)):
        path = cfg.get(key) or ""
        entry: dict[str, Any] = {"key": key, "path": path, "required": required}
        if not path:
            entry |= {"state": "unset",
                      "note": i18n.t("path.unset_required" if required
                                     else "path.unset_optional", language)}
        elif not os.path.isdir(path):
            entry |= {"state": "missing",
                      "note": i18n.t("path.missing", language, path=path)}
        else:
            try:
                count = len(os.listdir(path))
                writable = os.access(path, os.W_OK)
                entry |= {"state": "ok", "entries": count, "writable": writable,
                          "note": i18n.t("path.ok", language, count=count)
                                  + ("" if writable else
                                     " — " + i18n.t("path.read_only", language))}
            except OSError as e:
                entry |= {"state": "unreadable", "note": str(e)}
        out.append(entry)
    return {"paths": out, "cleanup": cfg.get("cleanup_paths", [])}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
@app.get("/api/settings")
def read_settings(_: dict = Depends(require_user)):
    cfg = engine.config()
    values = {f.key: cfg.get(f.key, f.default) for f in S.FIELDS}
    return {"schema": S.describe(), "values": S.mask(values)}


@app.post("/api/settings")
def write_settings(request: Request, body: dict = Body(...),
                   _: dict = Depends(require_user)):
    """Accepts either ``{"key": …, "value": …}`` or several at once.

    Every value is validated against the schema before a single one is written —
    either all of them or none.
    """
    if "key" in body:
        incoming = {str(body["key"]): body.get("value")}
    elif isinstance(body.get("values"), dict):
        incoming = body["values"]
    else:
        raise _fail(request, 400, "error.bad_settings_body")

    current = engine.config()
    clean: dict[str, Any] = {}
    for key, value in incoming.items():
        if key not in S.BY_KEY:
            raise _fail(request, 400, "error.unknown_setting", key=key)
        try:
            clean[key] = S.validate(key, S.unmask(key, value, current.get(key)))
        except ValueError as e:
            raise _fail_from_value_error(request, e) from e

    store.set_many(clean)
    if any(S.BY_KEY[k].reschedules for k in clean):
        _schedule()

    language = language_for(request)
    notes = []
    for key, value in clean.items():
        if S.BY_KEY[key].kind == "path" and value and not os.path.isdir(value):
            notes.append(i18n.t("path.missing", language, path=value))
    updated = engine.config()
    return {"ok": True, "saved": sorted(clean),
            "values": S.mask({f.key: updated.get(f.key) for f in S.FIELDS}),
            "notes": notes}


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
@app.get("/api/rules")
def list_rules(_: dict = Depends(require_user)):
    cfg = engine.config()
    counts = store.summary()["per_rule"]
    return {"categories": list(CATEGORIES), "rules": [{
        "name": r.name, "category": r.category, "found": counts.get(r.name, 0),
        "scope": r.scope, "only_kinds": list(r.only_kinds), "deep": r.deep,
        "modifies": r.modifies, "deletes": r.deletes,
        "fix_by_default": r.fix_by_default,
        "enabled": (cfg["rules"].get(r.name) or {}).get("enabled", True),
        "fix": (cfg["rules"].get(r.name) or {}).get("fix", r.fix_by_default),
    } for r in ALL]}


class RuleToggle(BaseModel):
    enabled: bool | None = None
    fix: bool | None = None


@app.post("/api/rules/{name}")
def toggle_rule(name: str, body: RuleToggle, request: Request,
                _: dict = Depends(require_user)):
    rule = BY_NAME.get(name)
    if rule is None:
        raise _fail(request, 404, "error.no_such_rule", name=name)
    everything = dict(engine.config()["rules"])
    entry = dict(everything.get(name, {}))
    if body.enabled is not None:
        entry["enabled"] = bool(body.enabled)
    if body.fix is not None:
        if not rule.modifies and body.fix:
            raise _fail(request, 400, "error.rule_cannot_fix", name=name)
        entry["fix"] = bool(body.fix)
    everything[name] = entry
    store.set("rules", everything)
    return {"ok": True, "rule": name, **entry}


@app.post("/api/rules")
def toggle_rules(request: Request, body: dict = Body(...),
                 _: dict = Depends(require_user)):
    """Several rules at once — "report only", for instance."""
    incoming = body.get("rules")
    if not isinstance(incoming, dict):
        raise _fail(request, 400, "error.bad_rules_body")
    everything = dict(engine.config()["rules"])
    for name, value in incoming.items():
        if name not in BY_NAME or not isinstance(value, dict):
            raise _fail(request, 400, "error.no_such_rule", name=name)
        entry = dict(everything.get(name, {}))
        if "enabled" in value:
            entry["enabled"] = bool(value["enabled"])
        if "fix" in value:
            entry["fix"] = bool(value["fix"]) and BY_NAME[name].modifies
        everything[name] = entry
    store.set("rules", everything)
    return {"ok": True, "rules": everything}


# ---------------------------------------------------------------------------
# Findings and runs
# ---------------------------------------------------------------------------
def _localise(rows: list[dict], language: str) -> list[dict]:
    """Re-render the stored description in the requested language.

    The store keeps the English text plus the message key and its parameters, so
    an entry written months ago still shows up in whatever language is active
    now.
    """
    if language == i18n.DEFAULT:
        return rows
    out = []
    for row in rows:
        data = row.get("data")
        if isinstance(data, str):
            try:
                import json
                data = json.loads(data)
            except (ValueError, TypeError):
                data = {}
        key = (data or {}).get("_msg")
        if key:
            row = {**row, "description": i18n.t(key, language,
                                                **((data or {}).get("_params") or {}))}
        out.append(row)
    return out


@app.get("/api/findings")
def findings(request: Request, limit: int = 200, rule: str | None = None,
             fixed_only: bool = False, _: dict = Depends(require_user)):
    rows = store.findings(limit=max(1, min(limit, 1000)), rule=rule,
                          fixed_only=fixed_only)
    return _localise(rows, language_for(request))


@app.get("/api/fixed")
def fixed(request: Request, limit: int = 100, _: dict = Depends(require_user)):
    """Only what was actually changed — the record of work done."""
    rows = store.findings(limit=max(1, min(limit, 1000)), fixed_only=True)
    rows = [r for r in rows
            if not str(r.get("action") or "").startswith(("DRY RUN", "FAILED"))]
    return _localise(rows, language_for(request))


@app.get("/api/runs")
def runs(limit: int = 50, _: dict = Depends(require_user)):
    return store.runs(max(1, min(limit, 200)))


@app.post("/api/check")
def check(request: Request, deep: bool = False, _: dict = Depends(require_user)):
    if engine.running:
        raise _fail(request, 409, "error.already_running")
    if not store.services(enabled_only=True):
        raise _fail(request, 400, "error.no_services")
    return engine.run(deep=deep, trigger="manual")


@app.post("/api/maintenance/compact")
def compact(request: Request, _: dict = Depends(require_user)):
    cfg = engine.config()
    removed = store.trim_findings(keep=int(cfg.get("log_keep", 20000)),
                                  days=int(cfg.get("log_days", 90)))
    store.prune_seen(30)
    store.prune_sessions()
    before = store.size()["mb"]
    store.compact()
    after = store.size()["mb"]
    return {"ok": True, "removed": removed, "before_mb": before, "after_mb": after,
            "message": i18n.t("message.compacted", language_for(request),
                              removed=removed,
                              freed=max(0, round(before - after, 2)))}


@app.post("/api/notify/test")
def notify_test(request: Request, _: dict = Depends(require_user)):
    cfg = engine.config()
    notifier = engine.notifier(cfg)
    if not notifier.configured:
        raise _fail(request, 400, "error.pushover_not_configured")
    language = language_for(request)
    ok, note = notifier.send("Correctarr",
                             i18n.t("message.test_notification", language),
                             "info", url=cfg.get("public_url", ""))
    if not ok:
        raise HTTPException(502, note)
    return {"ok": True, "message": note}


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------
@app.get("/api/queue")
def queue(_: dict = Depends(require_user)):
    from .scoring import score
    out = []
    for service in engine.arr_services():
        try:
            if not service.reachable()[0]:
                continue
            profiles = {p["id"]: p for p in service.profiles()}
            formats = {f["name"]: f for f in service.custom_formats()}
            for entry in service.queue():
                item = entry.get("movie") or entry.get("series") or {}
                profile = profiles.get(item.get("qualityProfileId"))
                total, hits = score(entry, profile, formats) if profile else (None, [])
                size = entry.get("size") or 0
                out.append({
                    "service": service.name, "kind": service.kind, "id": entry["id"],
                    "release": entry.get("title"), "item": item.get("title"),
                    "year": item.get("year"),
                    "profile": profile.get("name") if profile else None,
                    "state": entry.get("trackedDownloadState"),
                    "status": entry.get("status"),
                    "gb": round(size / 1024 ** 3, 2),
                    "percent": (round(100 * (1 - (entry.get("sizeleft") or 0) / size), 1)
                                if size else None),
                    "score_then": entry.get("customFormatScore"),
                    "score_now": total, "hits": hits,
                    "messages": [m for s in (entry.get("statusMessages") or [])
                                 for m in (s.get("messages") or [])],
                })
        except ArrError as e:
            log.warning("Could not read the queue of %s: %s", service.name, e)
        finally:
            service.close()
    return out


@app.get("/api/indexers")
def indexers(_: dict = Depends(require_user)):
    """The indexer ratings for inspection — read only, changes nothing."""
    from .indexers import WEIGHTS, rank_deviation, ranked
    cfg = engine.config()
    out = []
    for group in engine._indexer_state(cfg):
        deviations = {d["view"].name: d for d in rank_deviation(
            group["views"], int(cfg.get("indexer_rank_tolerance", 2)))}
        out.append({
            "name": group["name"], "weights": WEIGHTS,
            "indexers": [{
                "name": v.name, "priority": v.priority, "enabled": v.enabled,
                "queries": v.queries, "grabs": v.grabs,
                "yield": round(100 * v.grabs / v.queries, 2) if v.queries else None,
                "errors": v.failed_queries + v.failed_grabs,
                "response_ms": round(v.response_ms),
                "mean_score": round(v.mean_score), "mean_gb": round(v.mean_gb, 1),
                "samples": v.samples, "rating": v.rating, "parts": v.parts,
                "solid": v.solid, "notes": v.notes,
                "deviation": ({k: val for k, val in deviations[v.name].items()
                               if k != "view"} if v.name in deviations else None),
            } for v in ranked(group["views"])],
        })
    return out
