"""Notification connections, and the summary sent through them."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import digest, notifications
from .. import settings as S
from ..rules import ALL, BY_NAME, CATEGORIES
from . import core, jobs
from .core import fail, fail_from_value_error, language_for, require_user

router = APIRouter()


class NotificationBody(BaseModel):
    id: int | None = None
    name: str = Field(default="", max_length=64)
    kind: str
    enabled: bool = True
    config: dict = Field(default_factory=dict)
    min_severity: str = "warning"
    rules: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    fixed_only: bool = False
    cooldown: int = 5


def _prepared_connection(body: NotificationBody, request: Request) -> dict:
    """Validate a submitted connection and put back any unchanged secret."""
    if body.kind not in notifications.KINDS:
        raise fail(request, 400, "error.unknown_channel_kind", kind=body.kind)
    if body.min_severity not in ("info", "warning", "error"):
        raise fail(request, 400, "error.unknown_severity")
    unknown = [r for r in body.rules if r not in BY_NAME]
    if unknown:
        raise fail(request, 400, "error.no_such_rule", name=unknown[0])
    unknown_categories = [c for c in body.categories if c not in CATEGORIES]
    if unknown_categories:
        raise fail(request, 400, "error.unknown_category",
                   name=unknown_categories[0])

    stored = core.store.notification(body.id) if body.id else None
    config = notifications.restore_secrets(
        body.kind, body.config, (stored or {}).get("config") or {}, S.MASK)
    try:
        config = notifications.validate(body.kind, config)
    except ValueError as e:
        raise fail_from_value_error(request, e) from e

    return {**body.model_dump(), "config": config,
            "cooldown": max(0, min(1440, body.cooldown))}


@router.get("/api/notifications/kinds")
def notification_kinds(_: dict = Depends(require_user)):
    """What can be configured, and which fields each one needs."""
    return {"kinds": notifications.describe_kinds(),
            "severities": ["info", "warning", "error"],
            "categories": list(CATEGORIES),
            "rules": [r.name for r in ALL]}


@router.get("/api/notifications")
def list_notifications(_: dict = Depends(require_user)):
    return [notifications.redact(entry, S.MASK) for entry in core.store.notifications()]


@router.post("/api/notifications")
def save_notification(body: NotificationBody, request: Request,
                      _: dict = Depends(require_user)):
    prepared = _prepared_connection(body, request)
    if not prepared["name"]:
        prepared["name"] = body.kind.capitalize()
    new_id = core.store.save_notification(prepared)
    return {"ok": True, "id": new_id}


@router.delete("/api/notifications/{notification_id}")
def delete_notification(notification_id: int, request: Request,
                        _: dict = Depends(require_user)):
    if not core.store.notification(notification_id):
        raise fail(request, 404, "error.no_such_connection")
    core.store.delete_notification(notification_id)
    return {"ok": True}


@router.post("/api/notifications/test")
def test_notification(body: NotificationBody, request: Request,
                      _: dict = Depends(require_user)):
    """Send a test to a connection that may not be saved yet.

    Testing before saving is the point: nobody should have to store a wrong
    token to find out it is wrong.
    """
    prepared = _prepared_connection(body, request)
    ok, detail = notifications.send_test(
        prepared, language=language_for(request),
        url=core.engine.config().get("public_url", ""))
    if not ok:
        raise HTTPException(502, detail)
    return {"ok": True, "message": detail}


@router.post("/api/notifications/telegram/chats")
def telegram_chats(body: NotificationBody, request: Request,
                   _: dict = Depends(require_user)):
    """The chats a Telegram bot can currently reach.

    The chat id is the awkward half of setting Telegram up: a bot cannot write
    to anyone who has not written to it first, and the id is shown nowhere.
    Reading the bot's pending updates turns that into a list to pick from.
    """
    if body.kind != "telegram":
        raise fail(request, 400, "error.unknown_channel_kind", kind=body.kind)
    prepared = _prepared_connection(body, request)
    channel = notifications.build(prepared)
    try:
        bot = channel.describe_bot()
        chats = channel.discover_chats()
    except notifications.ChannelError as e:
        raise HTTPException(502, str(e)) from e
    return {"ok": True, "bot": bot.get("username") or bot.get("first_name", ""),
            "chats": chats}


# ---------------------------------------------------------------------------
# The summary
# ---------------------------------------------------------------------------
@router.get("/api/digest")
def digest_view(request: Request, _: dict = Depends(require_user)):
    """The summary as it would go out now, and when the next one is due."""
    cfg = core.engine.config()
    period = cfg.get("digest", "off")
    job = jobs.scheduler.get_job("digest")
    summary = jobs.summary(language_for(request),
                           period if period in digest.PERIODS else "weekly")
    return {"period": period,
            "next": job.next_run_time.isoformat() if job and job.next_run_time else None,
            "last_sent": core.store.get(digest.SENT_KEY),
            "connections": len(core.engine.notification_targets()),
            "preview": digest.preview(summary)}


@router.post("/api/digest/send")
def digest_send(request: Request, _: dict = Depends(require_user)):
    """Send the summary now, whatever the setting — to see what it looks like.

    Sent even when there is nothing in it, with a line saying so: a test that
    stays silent cannot be told apart from one that failed.
    """
    targets = core.engine.notification_targets()
    if not targets:
        raise fail(request, 400, "error.no_connections")
    cfg = core.engine.config()
    period = cfg.get("digest", "off")
    period = period if period in digest.PERIODS else "weekly"
    language = jobs.digest_language(cfg)
    summary = jobs.summary(language, period) or digest.Summary(
        groups=[], total=0, fixed=0, language=language,
        url=cfg.get("public_url", ""), headline_key="digest.nothing")
    # Not remembered as sent. Trying it out on a Sunday evening must not be
    # why Monday's summary stays away.
    outcomes = digest.send(targets, summary)
    return {"ok": any(o["ok"] for o in outcomes), "sent": outcomes}
