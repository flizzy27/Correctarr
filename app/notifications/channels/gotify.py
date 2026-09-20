"""Gotify.

Self-hosted, token per application. The token goes in the query string because
that is what Gotify documents; it also accepts an ``X-Gotify-Key`` header, and
that is used here instead so the token never lands in a proxy access log.
"""
from __future__ import annotations

import httpx

from ..base import Channel, ChannelError, ConfigField, Report

PRIORITY = {"info": 2, "warning": 5, "error": 8}


class Gotify(Channel):
    kind = "gotify"
    LIMIT = 4000

    FIELDS = (
        ConfigField("server", "text", placeholder="https://gotify.example.com"),
        ConfigField("token", "secret", placeholder="A…"),
    )

    def deliver(self, report: Report) -> tuple[bool, str]:
        server = (self.value("server") or "").rstrip("/")
        if not server.startswith(("http://", "https://")):
            raise ChannelError("The server address has to start with http:// or https://")

        body = (self._test_body(report.language) if report.is_test
                else "\n".join(report.lines(per_group=4, symbols=False)))
        payload = {
            "title": report.headline()[:250],
            "message": self._trim(body.strip() or "—", language=report.language),
            "priority": PRIORITY.get(report.severity, 5),
        }
        if report.url:
            # Gotify renders these when the client supports it and ignores them
            # otherwise, so it is safe to always send.
            payload["extras"] = {
                "client::notification": {"click": {"url": report.url}},
            }

        try:
            with httpx.Client(timeout=20) as client:
                response = client.post(f"{server}/message", json=payload,
                                       headers={"X-Gotify-Key": self.value("token")})
        except httpx.RequestError as e:
            raise ChannelError(f"Gotify is unreachable: {e}") from e
        if response.status_code < 300:
            return True, "sent"
        if response.status_code in (401, 403):
            raise ChannelError("Gotify refused the application token")
        raise ChannelError(f"Gotify returned {response.status_code}: "
                           f"{response.text[:150]}")
