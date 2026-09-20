"""Pushover notifications.

Principles:
  * Only what is new. A long-running problem is reported once, not on every run.
  * Bundled. One run produces at most one message.
  * Grouped by rule, so twelve findings are not twelve paragraphs.
  * With a cooldown: runs close together do not send twice.
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Iterable

import httpx

from .i18n import t

log = logging.getLogger(__name__)

ENDPOINT = "https://api.pushover.net/1/messages.json"
LIMIT = 1024                      # Pushover refuses anything longer

# Pushover priorities: -2 silent, -1 quiet, 0 normal, 1 loud, 2 acknowledge
PRIORITY = {"info": -1, "warning": 0, "error": 1}
RANK = {"info": 0, "warning": 1, "error": 2}
SYMBOL = {"error": "⚠️", "warning": "❗", "info": "ℹ️"}


def _escape(text: str) -> str:
    """Pushover allows only b, i, u, font and a — everything else must be
    escaped, or the markup shows up literally."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Pushover:
    def __init__(self, app_key: str, user_key: str,
                 devices: str = "", sound: str = "pianobar"):
        self.app_key = (app_key or "").strip()
        self.user_key = (user_key or "").strip()
        self.devices = (devices or "").strip()
        self.sound = sound or "pianobar"

    @property
    def configured(self) -> bool:
        return bool(self.app_key and self.user_key)

    def send(self, title: str, text: str, severity: str = "warning",
             url: str = "", url_title: str = "") -> tuple[bool, str]:
        if not self.configured:
            return False, "Pushover is not configured"
        payload = {
            "token": self.app_key,
            "user": self.user_key,
            "title": title[:250],
            "message": text[:LIMIT],
            "priority": PRIORITY.get(severity, 0),
            "sound": self.sound,
            # Without this the <b> markers show up literally in the message.
            "html": 1,
        }
        if self.devices:
            payload["device"] = self.devices
        if url:
            payload["url"] = url
            payload["url_title"] = url_title or "Open Correctarr"
        try:
            with httpx.Client(timeout=20) as client:
                response = client.post(ENDPOINT, data=payload)
            if response.status_code == 200:
                return True, "sent"
            return False, f"{response.status_code}: {response.text[:150]}"
        except httpx.RequestError as e:
            return False, f"unreachable: {e}"

    # -- report for one run ----------------------------------------------------
    _last_sent = 0.0                  # shared across instances

    def report(self, findings: Iterable, min_severity: str = "warning",
               fixed_only: bool = False, public_url: str = "",
               cooldown_minutes: int = 5,
               language: str = "en") -> tuple[bool, str]:
        threshold = RANK.get(min_severity, 1)
        selected = [f for f in findings
                    if RANK.get(f.severity, 1) >= threshold
                    and (not fixed_only or f.action)]
        if not selected:
            return False, "nothing to report"

        now = time.monotonic()
        if now - Pushover._last_sent < cooldown_minutes * 60:
            remaining = int(cooldown_minutes * 60 - (now - Pushover._last_sent))
            return False, f"cooldown, {remaining}s left"
        Pushover._last_sent = now

        fixed = [f for f in selected
                 if f.action and not str(f.action).startswith("DRY RUN")]
        highest = max(selected, key=lambda f: RANK.get(f.severity, 1)).severity

        # Group by rule, worst first.
        groups: OrderedDict[str, list] = OrderedDict()
        for finding in sorted(selected, key=lambda f: -RANK.get(f.severity, 1)):
            groups.setdefault(finding.rule, []).append(finding)

        lines: list[str] = []
        remainder = 0
        for rule, group in groups.items():
            heading = t(f"rules.{rule}.title", language)
            symbol = SYMBOL.get(group[0].severity, "")
            lines.append(f"{symbol} <b>{_escape(heading)}</b> ({len(group)})")
            for finding in group[:4]:
                line = f"• {_escape(finding.title[:52])}"
                if finding.action and not str(finding.action).startswith("DRY RUN"):
                    line += f" → <i>{_escape(str(finding.action)[:40])}</i>"
                lines.append(line)
            if len(group) > 4:
                remainder += len(group) - 4
            lines.append("")

        if remainder:
            lines.append(t("notify.and_more", language, count=remainder))
        text = "\n".join(lines).strip()

        # Trim safely below the length limit without ending mid-word.
        if len(text) > LIMIT:
            text = text[:LIMIT - 30].rsplit("\n", 1)[0] + "\n" + t("notify.truncated", language)

        title = t("notify.title", language, count=len(selected))
        if fixed:
            title += t("notify.title_fixed", language, count=len(fixed))
        return self.send(title, text, highest, url=public_url,
                         url_title=t("notify.open", language))
