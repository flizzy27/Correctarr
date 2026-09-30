"""Discord, through a webhook.

An embed is used rather than plain content: it carries a colour, which makes
severity readable at a glance in a busy channel, and it survives long bodies
better than a message does.

The limits are separate and all enforced by Discord rather than truncated by
it: 256 for the title and for a field name, 4096 for the description, 1024 for
a field value, 25 fields — and 6000 across the whole embed. That last one is
the one that bit: twenty rules with five findings each came to well over it,
and a large run, the one most worth hearing about, was refused outright.

Release names are full of the characters Discord reads as formatting.
``Some_Film_2019`` came out with half its name in italics, so everything taken
from a finding is escaped.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime

from ...i18n import t
from ..base import Channel, ChannelError, ConfigField, Report

# Colours chosen to read as severity, not as branding.
COLOUR = {"error": 0xF0574D, "warning": 0xD9A03A, "info": 0x4F8CFF}

TITLE_LIMIT = 256
DESCRIPTION_LIMIT = 4096
FIELD_NAME_LIMIT = 256
FIELD_VALUE_LIMIT = 1024
MAX_FIELDS = 25
#: Across title, description, footer and every field name and value.
EMBED_LIMIT = 6000
CONTENT_LIMIT = 2000
PER_GROUP = 5

_MARKDOWN = re.compile(r"([\\*_~`|>\[\]])")


def escape(text: str) -> str:
    return _MARKDOWN.sub(r"\\\1", text or "")


class Discord(Channel):
    kind = "discord"
    LIMIT = DESCRIPTION_LIMIT
    SUPPORTS_MARKUP = True

    FIELDS = (
        ConfigField("webhook_url", "secret", schemes=("https://",),
                    placeholder="https://discord.com/api/webhooks/…"),
        ConfigField("username", "text", required=False, default="Correctarr"),
        ConfigField("avatar_url", "text", required=False, schemes=("https://",)),
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
            self._add_fields(embed, report)
        else:
            embed["description"] = "—"

        payload: dict = {"embeds": [embed]}
        if self.value("username"):
            payload["username"] = self.value("username")[:80]
        if self.value("avatar_url"):
            payload["avatar_url"] = self.value("avatar_url")
        # A mention only goes on something worth interrupting for.
        if self.value("mention") and report.severity == "error" and not report.is_test:
            payload["content"] = self.value("mention")[:CONTENT_LIMIT]

        response = self._request(url, "Discord", json=payload)
        if response.status_code in (200, 204):
            return True, "sent"
        raise self._refused(response, "Discord")

    def _add_fields(self, embed: dict, report: Report) -> None:
        """One field per rule, for as many rules as fit in the embed.

        One field per rule reads far better in Discord than one long block,
        and it keeps each piece under its own limit.
        """
        language = report.language
        room = EMBED_LIMIT - len(embed["title"]) - len(embed["footer"]["text"])
        # Kept back for the line that says how many rules did not fit.
        room -= 80
        fields = []
        for group in report.groups[:MAX_FIELDS]:
            lines = []
            for finding in group.findings[:PER_GROUP]:
                line = f"• {escape(finding.title[:70])}"
                action = str(getattr(finding, "action", "") or "")
                if action and not action.startswith(("DRY RUN", "FAILED")):
                    line += f"\n  ↳ *{escape(action[:60])}*"
                lines.append(line)
            if len(group.findings) > PER_GROUP:
                lines.append(t("notify.and_more", language,
                               count=len(group.findings) - PER_GROUP))
            value = self._fit(lines, FIELD_VALUE_LIMIT, language) or "—"
            name = f"{group.title(language)} ({len(group.findings)})"[:FIELD_NAME_LIMIT]
            if len(name) + len(value) > room:
                break
            room -= len(name) + len(value)
            fields.append({"name": name, "value": value, "inline": False})
        embed["fields"] = fields
        left_out = len(report.groups) - len(fields)
        if left_out:
            embed["description"] = t("notify.more_rules", language, count=left_out)
