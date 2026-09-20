"""A plain webhook.

For anything not covered by a dedicated channel: Home Assistant, n8n, Node-RED,
Apprise, a shell script behind a tiny server. The whole report goes out as
JSON, structured rather than pre-rendered, so the receiving end can do whatever
it likes with it.

The shape is deliberately stable. Anyone wiring an automation to it should not
have to rewrite it after an update, so fields are added here but never renamed
or removed.
"""
from __future__ import annotations

import json

import httpx

from ..base import Channel, ChannelError, ConfigField, Report

#: Raised whenever the payload shape changes in a way a receiver could notice.
PAYLOAD_VERSION = 1


class Webhook(Channel):
    kind = "webhook"
    LIMIT = 100_000

    FIELDS = (
        ConfigField("url", "text", placeholder="https://example.com/hook"),
        ConfigField("method", "choice", required=False, default="POST",
                    choices=("POST", "PUT")),
        ConfigField("secret_header", "text", required=False,
                    placeholder="X-Webhook-Token"),
        ConfigField("secret_value", "secret", required=False),
        ConfigField("include_findings", "switch", required=False, default=True),
    )

    def deliver(self, report: Report) -> tuple[bool, str]:
        url = self.value("url")
        if not url.startswith(("http://", "https://")):
            raise ChannelError("The address has to start with http:// or https://")

        payload = {
            "version": PAYLOAD_VERSION,
            "source": "correctarr",
            "event": "test" if report.is_test else "run",
            "severity": report.severity,
            "title": report.headline(),
            "total": report.total,
            "fixed": report.fixed,
            "dry_run": report.dry_run,
            "url": report.url,
            "language": report.language,
            "groups": [{
                "rule": group.rule,
                "title": group.title(report.language),
                "severity": group.severity,
                "count": len(group.findings),
            } for group in report.groups],
        }
        if self.value("include_findings") and not report.is_test:
            payload["findings"] = [{
                "rule": f.rule,
                "severity": f.severity,
                "title": f.title,
                "description": f.describe(report.language),
                "service": f.service,
                "action": f.action,
                "data": {k: v for k, v in (f.data or {}).items()
                         if not k.startswith("_")},
            } for group in report.groups for f in group.findings]

        headers = {"Content-Type": "application/json",
                   "User-Agent": "Correctarr"}
        if self.value("secret_header") and self.value("secret_value"):
            headers[self.value("secret_header")] = self.value("secret_value")

        body = json.dumps(payload, ensure_ascii=False, default=str)
        try:
            with httpx.Client(timeout=20) as client:
                response = client.request(self.value("method") or "POST", url,
                                          content=body.encode("utf-8"),
                                          headers=headers)
        except httpx.RequestError as e:
            raise ChannelError(f"The endpoint is unreachable: {e}") from e
        if response.status_code < 300:
            return True, f"sent ({response.status_code})"
        raise ChannelError(f"The endpoint returned {response.status_code}: "
                           f"{response.text[:150]}")
