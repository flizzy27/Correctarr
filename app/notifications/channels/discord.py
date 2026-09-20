"""Discord, through a webhook.

An embed is used rather than plain content: it carries a colour, which makes
severity readable at a glance in a busy channel, and it survives long bodies
better than a message does.

The limits are separate and all enforced by Discord rather than truncated by
it: 256 for the title, 4096 for the description, 6000 across the whole embed,
and 25 fields. A message over any of them is rejected outright.
"""
from __future__ import annotations

from datetime import UTC, datetime

import httpx

from ..base import Channel, ChannelError, ConfigField, Report

# Colours chosen to read as severity, not as branding.
COLOUR = {"error": 0xF0574D, "warning": 0xD9A03A, "info": 0x4F8CFF}

TITLE_LIMIT = 256
DESCRIPTION_LIMIT = 4096
FIELD_VALUE_LIMIT = 1024
MAX_FIELDS = 20


class Discord(Channel):
    kind = "discord"
    LIMIT = DESCRIPTION_LIMIT
    SUPPORTS_MARKUP = True

    FIELDS = (
        ConfigField("webhook_url", "secret",
                    placeholder="https://discord.com/api/webhooks/…"),
        ConfigField("username", "text", required=False, default="Correctarr"),
        ConfigField("avatar_url", "text", required=False),
        ConfigField("mention", "text", required=False,
                    placeholder="<@123…> or <@&role id>"),
    )

    def deliver(self, report: Report) -> tuple[bool, str]:
        url = self.value("webhook_url")
        if not url.startswith("https://"):
            raise ChannelError("The webhook address has to start with https://")

        embed = {
            "title": report.headline()[:TITLE_LIMIT],
            "color": COLOUR.get(report.severity, COLOUR["info"]),
            "timestamp": datetime.now(UTC).isoformat(),
            "footer": {"text": "Correctarr"},
        }
        if report.url:
            embed["url"] = report.url

        if report.is_test:
            embed["description"] = self._test_body(report.language)
        elif report.groups:
            # One field per rule reads far better in Discord than one long
            # block, and it keeps each piece under its own limit.
            fields = []
            for group in report.groups[:MAX_FIELDS]:
                lines = []
                for finding in group.findings[:5]:
                    line = f"• {finding.title[:70]}"
                    action = str(getattr(finding, "action", "") or "")
                    if action and not action.startswith(("DRY RUN", "FAILED")):
                        line += f"\n  ↳ *{action[:60]}*"
                    lines.append(line)
                if len(group.findings) > 5:
                    lines.append(f"… and {len(group.findings) - 5} more")
                value = "\n".join(lines)[:FIELD_VALUE_LIMIT]
                fields.append({
                    "name": f"{group.title(report.language)} ({len(group.findings)})",
                    "value": value or "—",
                    "inline": False,
                })
            embed["fields"] = fields
            if len(report.groups) > MAX_FIELDS:
                embed["description"] = (
                    f"… and {len(report.groups) - MAX_FIELDS} more rules")
        else:
            embed["description"] = "—"

        payload: dict = {"embeds": [embed]}
        if self.value("username"):
            payload["username"] = self.value("username")[:80]
        if self.value("avatar_url"):
            payload["avatar_url"] = self.value("avatar_url")
        # A mention only goes on something worth interrupting for.
        if self.value("mention") and report.severity == "error" and not report.is_test:
            payload["content"] = self.value("mention")

        try:
            with httpx.Client(timeout=20) as client:
                response = client.post(url, json=payload)
        except httpx.RequestError as e:
            raise ChannelError(f"Discord is unreachable: {e}") from e
        if response.status_code in (200, 204):
            return True, "sent"
        if response.status_code == 429:
            raise ChannelError("Discord is rate limiting this webhook")
        raise ChannelError(f"Discord returned {response.status_code}: "
                           f"{response.text[:150]}")
