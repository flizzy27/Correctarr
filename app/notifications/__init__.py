"""Notification channels and the routing between them.

A run produces findings. This decides which of them each configured connection
should hear about, assembles one report per connection, and hands it over.

Routing is per connection rather than global, because the useful setups are not
uniform: an error-only push to a phone, everything into a Discord channel, and
a webhook that only fires when something was actually changed. A single global
threshold cannot express that.

Three brakes keep it quiet, and all three are needed:

  * **Deduplication**, upstream — a long-running problem is reported once, not
    on every pass. Without it a stuck folder reports every single minute.
  * **A cooldown**, per connection — runs close together do not send twice.
  * **A severity threshold**, per connection.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from ..policy import really_happened
from .base import (
    SEVERITY_RANK,
    Channel,
    ChannelError,
    ConfigField,
    Group,
    Report,
    group_findings,
)
from .channels.discord import Discord
from .channels.gotify import Gotify
from .channels.ntfy import Ntfy
from .channels.pushover import Pushover
from .channels.telegram import Telegram
from .channels.webhook import Webhook

log = logging.getLogger(__name__)

__all__ = ["Channel", "ChannelError", "ConfigField", "Group", "Report",
           "KINDS", "build", "describe_kinds", "dispatch", "group_findings"]

#: Every provider that can be configured. Adding one means writing the class
#: and putting it here; the store, the API, the interface and the translations
#: all follow from the class itself.
KINDS: dict[str, type[Channel]] = {
    Pushover.kind: Pushover,
    Telegram.kind: Telegram,
    Discord.kind: Discord,
    Ntfy.kind: Ntfy,
    Gotify.kind: Gotify,
    Webhook.kind: Webhook,
}

# Last send per connection id. Kept in memory on purpose: a restart should let
# the next run through rather than sit on a cooldown nobody can see.
_last_sent: dict[int, float] = {}
_lock = threading.Lock()


def describe_kinds() -> list[dict]:
    """Everything the interface needs to render a form for each provider."""
    return [KINDS[kind].describe() for kind in KINDS]


def build(connection: dict) -> Channel:
    """Turn a stored row into a usable channel."""
    kind = connection.get("kind")
    channel_class = KINDS.get(kind)
    if channel_class is None:
        raise ChannelError(f"Unknown notification kind: {kind}")
    return channel_class(connection.get("config") or {},
                         name=connection.get("name") or kind)


def validate(kind: str, config: dict) -> dict:
    channel_class = KINDS.get(kind)
    if channel_class is None:
        raise ValueError(f"error.unknown_channel_kind|{kind}")
    return channel_class.validate(config)


def secret_keys(kind: str) -> set[str]:
    channel_class = KINDS.get(kind)
    return channel_class.secret_keys() if channel_class else set()


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------
def selects(connection: dict, finding) -> bool:
    """Should this connection hear about this finding?"""
    threshold = SEVERITY_RANK.get(connection.get("min_severity") or "warning", 1)
    if SEVERITY_RANK.get(finding.severity, 1) < threshold:
        return False

    if connection.get("fixed_only") and not really_happened(
            getattr(finding, "action", "")):
        return False

    # An empty list means everything. Naming rules is the exception, not the
    # rule, so the default has to be "all" — otherwise a new rule would be
    # silently muted on every existing connection.
    wanted_rules = connection.get("rules") or []
    if wanted_rules and finding.rule not in wanted_rules:
        return False

    wanted_categories = connection.get("categories") or []
    if wanted_categories:
        from ..rules import BY_NAME
        rule = BY_NAME.get(finding.rule)
        if rule is None or rule.category not in wanted_categories:
            return False
    return True


def on_cooldown(connection_id: int, minutes: int) -> int:
    """Seconds left before this connection may send again. 0 means go ahead."""
    if minutes <= 0:
        return 0
    with _lock:
        last = _last_sent.get(connection_id, 0.0)
    remaining = minutes * 60 - (time.monotonic() - last)
    return max(0, int(remaining))


def note_sent(connection_id: int) -> None:
    with _lock:
        _last_sent[connection_id] = time.monotonic()


def reset_cooldowns() -> None:
    with _lock:
        _last_sent.clear()


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
def dispatch(connections: list[dict], findings: list, *, language: str = "en",
             url: str = "", dry_run: bool = False) -> list[dict]:
    """Send one report per connection. Returns what happened, per connection.

    Every channel is isolated: one that is misconfigured, unreachable or simply
    broken must not stop the others, and must never take the run down with it.
    """
    outcomes: list[dict] = []
    for connection in connections:
        if not connection.get("enabled", True):
            continue
        name = connection.get("name") or connection.get("kind")
        connection_id = int(connection.get("id") or 0)

        selected = [f for f in findings if selects(connection, f)]
        if not selected:
            outcomes.append({"id": connection_id, "name": name,
                             "ok": False, "detail": "nothing to report"})
            continue

        waiting = on_cooldown(connection_id, int(connection.get("cooldown") or 0))
        if waiting:
            outcomes.append({"id": connection_id, "name": name, "ok": False,
                             "detail": f"cooldown, {waiting}s left"})
            continue

        report = Report(
            groups=group_findings(selected),
            total=len(selected),
            fixed=sum(1 for f in selected
                      if really_happened(f.action)),
            language=language, url=url, dry_run=dry_run)

        try:
            channel = build(connection)
        except ChannelError as e:
            outcomes.append({"id": connection_id, "name": name,
                             "ok": False, "detail": str(e)})
            continue

        ok, detail = channel.send(report)
        if ok:
            note_sent(connection_id)
        else:
            log.warning("Notification via %s failed: %s", name, detail)
        outcomes.append({"id": connection_id, "name": name,
                         "ok": ok, "detail": detail})
    return outcomes


def send_test(connection: dict, language: str = "en",
              url: str = "") -> tuple[bool, str]:
    channel = build(connection)
    return channel.send_test(language=language, url=url)


def redact(connection: dict, mask: str) -> dict[str, Any]:
    """A copy safe to hand to the interface: secrets replaced by a placeholder."""
    config = dict(connection.get("config") or {})
    for key in secret_keys(connection.get("kind", "")):
        if config.get(key):
            config[key] = mask
    return {**connection, "config": config}


def restore_secrets(kind: str, incoming: dict, stored: dict, mask: str) -> dict:
    """Put back any secret that came in unchanged as the placeholder."""
    merged = dict(incoming)
    for key in secret_keys(kind):
        if merged.get(key) == mask:
            merged[key] = (stored or {}).get(key, "")
    return merged
