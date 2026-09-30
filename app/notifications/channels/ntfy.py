"""ntfy.

Self-hosted or ntfy.sh. Sent as JSON to the server root rather than as headers
to the topic URL: the header form needs every value to be Latin-1, and release
names are full of characters that are not.

The message limit is 4096 **bytes**, not characters. A longer message is not
refused — it arrives as an attachment called ``attachment.txt``, which is worse
— and with umlauts and symbols a report of 4000 characters is well over it.
"""
from __future__ import annotations

from ..base import Channel, ChannelError, ConfigField, Report

PRIORITY = {"info": 2, "warning": 3, "error": 4}
TAGS = {"info": ["information_source"], "warning": ["warning"],
        "error": ["rotating_light"]}


def _bytes(text: str) -> int:
    return len(text.encode("utf-8"))


class Ntfy(Channel):
    kind = "ntfy"
    LIMIT = 4096
    DESTINATION = ("server",)

    FIELDS = (
        ConfigField("server", "text", default="https://ntfy.sh",
                    placeholder="https://ntfy.sh", schemes=("http://", "https://")),
        ConfigField("topic", "text", placeholder="correctarr-a1b2c3"),
        ConfigField("token", "secret", required=False,
                    placeholder="tk_… for a protected topic"),
        ConfigField("username", "text", required=False),
        ConfigField("password", "secret", required=False),
    )

    def deliver(self, report: Report) -> tuple[bool, str]:
        server = (self.value("server") or "https://ntfy.sh").rstrip("/")
        if not server.startswith(("http://", "https://")):
            raise ChannelError("The server address has to start with http:// or https://")

        body = (self._test_body(report.language) if report.is_test
                else "\n".join(report.lines(per_group=4, symbols=False)))
        payload = {
            "topic": self.value("topic"),
            "title": report.headline()[:200],
            "message": self._fit((body.strip() or "—").split("\n"),
                                 language=report.language, measure=_bytes),
            "priority": PRIORITY.get(report.severity, 3),
            "tags": TAGS.get(report.severity, []),
        }
        if report.url:
            payload["click"] = report.url

        headers = {}
        if self.value("token"):
            headers["Authorization"] = f"Bearer {self.value('token')}"
        auth = None
        if not headers and self.value("username"):
            auth = (self.value("username"), self.value("password"))

        response = self._request(server, "ntfy", json=payload, headers=headers,
                                 auth=auth)
        if response.status_code < 300:
            return True, "sent"
        if response.status_code in (401, 403):
            raise ChannelError("ntfy refused the credentials for this topic")
        raise self._refused(response, "ntfy")
