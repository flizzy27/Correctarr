"""Signing in and out, the first account, and changing a password."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from .. import auth, i18n
from . import core
from .core import (
    BASE,
    VERSION,
    current_user,
    fail,
    is_set_up,
    language_for,
    log,
    origin_of,
    require_user,
)

router = APIRouter()


class Credentials(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


def _is_https(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0]
    return forwarded.strip() == "https"


def _set_cookie(response: Response, token: str, request: Request) -> None:
    response.set_cookie(
        auth.COOKIE, token, max_age=auth.SESSION_DAYS * 86400,
        httponly=True, samesite="lax", secure=_is_https(request),
        path=BASE or "/")


@router.get("/api/auth/state")
def auth_state(request: Request):
    user = current_user(request)
    return {"set_up": is_set_up(), "mode": auth.mode(),
            "signed_in": user is not None,
            "user": user["name"] if user else None,
            "version": VERSION}


@router.post("/api/auth/setup")
def auth_setup(body: Credentials, request: Request, response: Response):
    """Create the first account. Afterwards this path is closed."""
    if is_set_up():
        raise fail(request, 409, "error.already_set_up")
    if (problem := auth.username_problem(body.name)):
        raise fail(request, 400, problem)
    if (problem := auth.password_problem(body.password, body.name)):
        raise fail(request, 400, problem, min=auth.MIN_PASSWORD_LENGTH)
    store = core.store
    user_id = store.create_user(body.name, auth.hash_password(body.password))
    token, token_digest = auth.new_token()
    store.create_session(token_digest, user_id, auth.SESSION_DAYS, origin_of(request))
    _set_cookie(response, token, request)
    log.info("First account created: %s", body.name)
    return {"ok": True, "user": body.name}


@router.post("/api/auth")
def sign_in(body: Credentials, request: Request, response: Response):
    origin = origin_of(request)
    # Per origin and per account. The origin alone can be made up: uvicorn
    # takes X-Forwarded-For from anyone so that a reverse proxy works, and a
    # new made-up address each attempt was a fresh set of tries each time.
    account = auth.account_key(body.name)
    if (wait := max(auth.retry_after(origin), auth.retry_after(account))):
        raise fail(request, 429, "error.too_many_attempts", seconds=wait)

    store = core.store
    user = store.user_by_name(body.name)
    # The hash is computed even without a match, otherwise the response time
    # reveals whether the user name exists.
    stored_hash = user["hash"] if user else auth.hash_password("no-such-user")
    correct = auth.verify_password(body.password, stored_hash)

    if not (user and correct):
        auth.note_failure(origin)
        auth.note_failure(account)
        log.warning("Failed sign-in for %r from %s", body.name, origin)
        raise fail(request, 401, "error.bad_credentials")

    auth.note_success(origin)
    auth.note_success(account)
    if auth.needs_rehash(stored_hash):
        store.set_user_hash(user["id"], auth.hash_password(body.password))
        log.info("Upgraded the password hash for %s", user["name"])
    store.note_login(user["id"])
    token, token_digest = auth.new_token()
    store.create_session(token_digest, user["id"], auth.SESSION_DAYS, origin)
    _set_cookie(response, token, request)
    return {"ok": True, "user": user["name"]}


@router.post("/api/auth/signout")
def sign_out(request: Request, response: Response):
    token = request.cookies.get(auth.COOKIE)
    if token:
        core.store.end_session(auth.digest(token))
    response.delete_cookie(auth.COOKIE, path=BASE or "/")
    return {"ok": True}


class PasswordChange(BaseModel):
    current: str = Field(min_length=1, max_length=256)
    replacement: str = Field(min_length=1, max_length=256)


@router.post("/api/auth/password")
def change_password(body: PasswordChange, request: Request, response: Response,
                    user: dict = Depends(require_user)):
    if auth.mode() == "off":
        raise fail(request, 400, "error.auth_disabled")
    store = core.store
    full = store.user_by_id(user["id"])
    if not full or not auth.verify_password(body.current, full["hash"]):
        auth.note_failure(origin_of(request))
        raise fail(request, 401, "error.current_password_wrong")
    if (problem := auth.password_problem(body.replacement, full["name"])):
        raise fail(request, 400, problem, min=auth.MIN_PASSWORD_LENGTH)
    store.set_user_hash(full["id"], auth.hash_password(body.replacement))
    # Every previous session stops being valid, including on other devices.
    # Someone changing their password usually wants exactly that.
    store.end_all_sessions(full["id"])
    token, token_digest = auth.new_token()
    store.create_session(token_digest, full["id"], auth.SESSION_DAYS, origin_of(request))
    _set_cookie(response, token, request)
    log.info("Password changed for %s, all other sessions ended", full["name"])
    return {"ok": True, "message": i18n.t("message.password_changed",
                                          language_for(request))}
