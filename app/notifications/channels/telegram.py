"""Telegram.

Set up through a bot: talk to @BotFather, create a bot, take the token. The
chat id is the harder half — the bot cannot message anyone who has not written
to it first, and the id is not shown anywhere in the app. ``discover_chats``
exists for exactly that: it reads the bot's pending updates and reports the
chats that have written to it, so the interface can offer them as a list
instead of asking somebody to construct an API call by hand.

HTML is used rather than MarkdownV2 on purpose. MarkdownV2 requires escaping
sixteen characters, several of which turn up in release names constantly —
dots, hyphens, brackets, plus signs. One missed escape and Telegram rejects
the whole message.
"""
from __future__ import annotations

import httpx

from ..base import Channel, ChannelError, ConfigField, Report

BASE = "https://api.telegram.org"
LIMIT = 4096


def escape(text: str) -> str:
    """Telegram's HTML mode: only these three have to go."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Telegram(Channel):
    kind = "telegram"
    LIMIT = LIMIT
    SUPPORTS_MARKUP = True

    FIELDS = (
        ConfigField("bot_token", "secret", placeholder="123456:ABC-DEF…"),
        ConfigField("chat_id", "text", placeholder="-1001234567890"),
        ConfigField("thread_id", "text", required=False,
                    placeholder="only for a topic in a group"),
        ConfigField("silent", "switch", required=False, default=False),
    )

    # -- helpers ---------------------------------------------------------------
    def _call(self, method: str, payload: dict) -> dict:
        url = f"{BASE}/bot{self.value('bot_token')}/{method}"
        try:
            with httpx.Client(timeout=20) as client:
                response = client.post(url, json=payload)
        except httpx.RequestError as e:
            raise ChannelError(f"Telegram is unreachable: {e}") from e
        try:
            body = response.json()
        except ValueError as e:
            raise ChannelError(f"Telegram returned {response.status_code}") from e
        if not body.get("ok"):
            # Telegram's own wording is more useful than anything we could add.
            raise ChannelError(body.get("description")
                               or f"Telegram returned {response.status_code}")
        return body.get("result") or {}

    def describe_bot(self) -> dict:
        """Who the token belongs to. Used to confirm a token before saving."""
        return self._call("getMe", {})

    def discover_chats(self) -> list[dict]:
        """Chats that have written to this bot, so the id does not have to be
        looked up by hand.

        Telegram only keeps pending updates for 24 hours and drops them once a
        webhook is set, so this can legitimately come back empty. The interface
        says so rather than pretending the bot is broken.
        """
        updates = self._call("getUpdates", {"limit": 100, "timeout": 0})
        seen: dict[str, dict] = {}
        for update in updates if isinstance(updates, list) else []:
            message = (update.get("message") or update.get("channel_post")
                       or update.get("my_chat_member") or {})
            chat = message.get("chat") or {}
            if not chat.get("id"):
                continue
            name = (chat.get("title") or " ".join(
                p for p in (chat.get("first_name"), chat.get("last_name")) if p)
                or chat.get("username") or str(chat["id"]))
            seen[str(chat["id"])] = {"id": str(chat["id"]), "name": name,
                                     "type": chat.get("type", "")}
        return list(seen.values())

    # -- sending ---------------------------------------------------------------
    def deliver(self, report: Report) -> tuple[bool, str]:
        if report.is_test:
            body = escape(self._test_body(report.language))
        else:
            rendered = []
            for line in report.lines(per_group=5):
                if line.startswith(("⚠️", "❗", "ℹ️")):
                    symbol, _, rest = line.partition(" ")
                    rendered.append(f"{symbol} <b>{escape(rest)}</b>")
                else:
                    rendered.append(escape(line))
            body = "\n".join(rendered)

        heading = f"<b>{escape(report.headline())}</b>"
        if report.url:
            heading = f'<a href="{escape(report.url)}">{heading}</a>'
        text = self._trim(f"{heading}\n\n{body}".strip(), language=report.language)

        payload = {
            "chat_id": self.value("chat_id"),
            "text": text,
            "parse_mode": "HTML",
            "disable_notification": bool(self.value("silent")),
            "link_preview_options": {"is_disabled": True},
        }
        if self.value("thread_id"):
            payload["message_thread_id"] = self.value("thread_id")
        self._call("sendMessage", payload)
        return True, "sent"
