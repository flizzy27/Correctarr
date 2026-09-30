"""What every router needs: the store, the engine, who is asking, and how to
say no in their language.

``store`` and ``engine`` are read through this module at the moment of the
request (``core.store``), never imported by name: an installation has exactly
one of each, and a name bound at import time would keep pointing at the old
one when it is replaced — which is how the tests give every case an empty
store of its own.
"""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .. import __version__, auth, i18n, logging_setup
from ..engine import Engine
from ..storage import Store

# Before anything below writes to the log: adopting the previous store and
# opening this one both do.
logging_setup.configure()
log = logging.getLogger("correctarr")

CONFIG_DIR = Path(os.getenv("CONFIG_DIR", "/config"))
#: What this program calls itself. One number, from one place.
VERSION = __version__
#: What the build called the image — a tag, or a branch and a commit. Useful
#: for reproducing a report, useless as a label, and long enough to break a
#: layout if it is treated as one.
BUILD = os.getenv("VERSION", "source")
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


# ---------------------------------------------------------------------------
# Who is asking
# ---------------------------------------------------------------------------
def origin_of(request: Request) -> str:
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


def is_set_up() -> bool:
    return auth.mode() == "off" or store.user_count() > 0


def _route_path(request: Request) -> str:
    """The path with the reverse proxy prefix taken off.

    Two proxy configurations are common and they behave differently:

      * ``proxy_pass http://host:8099/`` strips the prefix, so the request
        arrives as ``/api/status``.
      * ``proxy_pass http://host:8099`` does not, so it arrives as
        ``/correctarr/api/status``.

    Comparing the raw path against the list of open paths only works for the
    first. In the second, ``/correctarr/api/alive`` would not match
    ``/api/alive``, the health check would be sent to the sign-in page, and the
    container would be reported as unhealthy. Stripping the prefix makes both
    shapes behave the same.
    """
    path = request.url.path
    if BASE and (path == BASE or path.startswith(BASE + "/")):
        path = path[len(BASE):] or "/"
    return path


#: Methods that change nothing.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _from_this_interface(request: Request) -> bool:
    """Was a change sent by a page of this interface, or by no page at all?

    The session cookie is ``SameSite=Lax``, which keeps it off a request from
    another *site* — but every other service on the same host is the same site.
    A page served by anything else on that machine, on any port, could post to
    ``/api/check`` or ``/api/setup/restart`` with the cookie attached and no
    question asked.

    A browser says where a request comes from. ``Sec-Fetch-Site`` is the
    clearest answer and is sent over https; over plain http, which is how most
    people reach this on their own network, there is only ``Origin`` to
    compare with the address the request was sent to. A request with neither
    was not sent by a page — a script, curl, the service's own webhook — and a
    page is the only thing this guards against.
    """
    site = request.headers.get("sec-fetch-site")
    if site:
        return site in ("same-origin", "none")
    origin = request.headers.get("origin")
    if not origin:
        return True
    try:
        sent_from = urlsplit(origin).netloc.lower()
    except ValueError:
        return False
    addresses = {part.strip().lower()
                 for header in ("host", "x-forwarded-host")
                 for part in (request.headers.get(header) or "").split(",")
                 if part.strip()}
    return bool(sent_from) and sent_from in addresses


async def gatekeeper(request: Request, call_next):
    path = _route_path(request)
    if (request.method not in SAFE_METHODS and path != "/api/event"
            and not _from_this_interface(request)):
        log.warning("Refused %s %s sent from %s", request.method, path,
                    request.headers.get("origin") or "another page")
        return JSONResponse({"detail": i18n.t("error.foreign_origin",
                                              language_for(request))},
                            status_code=403)
    if path.startswith("/static") or path in OPEN_PATHS:
        return await call_next(request)

    if not is_set_up():
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


# ---------------------------------------------------------------------------
# Saying no
# ---------------------------------------------------------------------------
def fail(request: Request, status: int, message_key: str, /, **fields) -> HTTPException:
    """An error the interface can show in the user's own language.

    The first three parameters are positional only on purpose. A translation
    placeholder called ``key`` is entirely reasonable — ``error.unknown_setting``
    has one — and without the marker it would collide with the parameter name
    and raise a TypeError instead of returning the error. That turned a clean
    400 into a 500.
    """
    return HTTPException(status, i18n.t(message_key, language_for(request), **fields))


def fail_from_value_error(request: Request, error: ValueError) -> HTTPException:
    """Validation errors carry ``key|arg|arg`` so they can be translated."""
    parts = str(error).split("|")
    key = parts[0]
    language = language_for(request)
    if key.startswith("error.") and len(parts) > 1:
        label = i18n.t(f"settings.{parts[1]}.label", language)
        extra = parts[2] if len(parts) > 2 else ""
        return HTTPException(400, i18n.t(key, language, field=label, value=extra))
    return HTTPException(400, i18n.t(key, language))
