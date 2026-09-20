"""Pushover.

Two things that cost time the first time round:

  * Without ``html=1`` the ``<b>`` marks show up literally in the message.
  * The body is capped at 1024 characters and anything longer is refused
    outright rather than truncated, so the trimming has to happen here.
"""
from __future__ import annotations

import httpx

from ..base import Channel, ChannelError, ConfigField, Report

ENDPOINT = "https://api.pushover.net/1/messages.json"

# -2 silent, -1 quiet, 0 normal, 1 loud, 2 requires acknowledgement
PRIORITY = {"info": -1, "warning": 0, "error": 1}


def escape(text: str) -> str:
    """Pushover allows only b, i, u, font and a — the rest must be escaped."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Pushover(Channel):
    kind = "pushover"
    LIMIT = 1024
    SUPPORTS_MARKUP = True

    FIELDS = (
        ConfigField("app_token", "secret", placeholder="a1b2c3…"),
        ConfigField("user_key", "secret", placeholder="u1v2w3…"),
        ConfigField("devices", "text", required=False),
        ConfigField("sound", "text", required=False, default="pianobar"),
    )

    def deliver(self, report: Report) -> tuple[bool, str]:
        if report.is_test:
            body = escape(self._test_body(report.language))
        else:
            body = "\n".join(
                escape(line) if not line.startswith(("⚠️", "❗", "ℹ️"))
                else _bold_heading(line)
                for line in report.lines(per_group=4))
        body = self._trim(body.strip(), language=report.language)

        payload = {
            "token": self.value("app_token"),
            "user": self.value("user_key"),
            "title": report.headline()[:250],
            "message": body or escape(self._test_body(report.language)),
            "priority": PRIORITY.get(report.severity, 0),
            "sound": self.value("sound") or "pianobar",
            "html": 1,
        }
        if self.value("devices"):
            payload["device"] = self.value("devices")
        if report.url:
            payload["url"] = report.url
            payload["url_title"] = "Correctarr"

        try:
            with httpx.Client(timeout=20) as client:
                response = client.post(ENDPOINT, data=payload)
        except httpx.RequestError as e:
            raise ChannelError(f"Pushover is unreachable: {e}") from e
        if response.status_code == 200:
            return True, "sent"
        raise ChannelError(f"Pushover returned {response.status_code}: "
                           f"{response.text[:150]}")


def _bold_heading(line: str) -> str:
    """A group heading arrives as "<symbol> Title (3)"; only the title is
    emphasised so the count stays readable."""
    symbol, _, rest = line.partition(" ")
    return f"{symbol} <b>{escape(rest)}</b>"
