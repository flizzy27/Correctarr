"""Every notification channel against the limits and habits of its provider.

Nothing is sent: the transport answers in place of the provider and keeps what
it was given.
"""
from __future__ import annotations

import json
import re

import httpx
import pytest

from app import connection, notifications
from app.notifications.base import Report, group_findings
from app.notifications.channels.discord import EMBED_LIMIT, Discord
from app.notifications.channels.ntfy import Ntfy
from app.notifications.channels.pushover import Pushover
from app.notifications.channels.telegram import Telegram
from app.rules import ALL, Finding

BOT_TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghijk"


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    waits = []
    monkeypatch.setattr(connection, "sleep", waits.append)
    return waits


@pytest.fixture
def provider(monkeypatch):
    """Answer every outgoing request; keep each one for inspection."""
    class Provider:
        def __init__(self):
            self.sent: list[httpx.Request] = []
            self.answers: list = []

        def answer(self, *responses):
            self.answers = list(responses)

        def handle(self, transport, request):
            request.read()
            self.sent.append(request)
            reply = self.answers.pop(0) if self.answers else httpx.Response(200, json={"ok": True})
            if isinstance(reply, Exception):
                raise reply
            return reply

        def body(self, index: int = -1) -> dict:
            return json.loads(self.sent[index].content)

    stand_in = Provider()
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request",
                        lambda transport, request: stand_in.handle(transport, request))
    return stand_in


def big_report(rules: int = 30, per_rule: int = 12) -> Report:
    names = [r.name for r in ALL][:rules]
    findings = [Finding(rule=name, severity="error", title=f"Some_Film_{i} *uncut* [2019] & <more> " * 2,
                        message="finding.missing_items", action="Removed_from_queue")
                for name in names for i in range(per_rule)]
    return Report(groups=group_findings(findings), total=len(findings), fixed=0,
                  url="http://correctarr.example/")


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------
def embed_size(embed: dict) -> int:
    return (len(embed.get("title", "")) + len(embed.get("description", ""))
            + len(embed.get("footer", {}).get("text", ""))
            + sum(len(f["name"]) + len(f["value"]) for f in embed.get("fields", [])))


def test_a_large_run_still_fits_in_one_discord_embed(provider):
    ok, _ = Discord({"webhook_url": "https://discord.com/api/webhooks/1/abc"}).send(big_report())
    assert ok
    embed = provider.body()["embeds"][0]
    assert embed_size(embed) <= EMBED_LIMIT
    assert len(embed["fields"]) <= 25
    assert all(len(f["value"]) <= 1024 and len(f["name"]) <= 256 for f in embed["fields"])
    assert "more rules" in embed["description"]


def test_discord_does_not_read_a_release_name_as_formatting(provider):
    Discord({"webhook_url": "https://discord.com/api/webhooks/1/abc"}).send(big_report(1, 1))
    value = provider.body()["embeds"][0]["fields"][0]["value"]
    assert r"Some\_Film\_0 \*uncut\* \[2019\]" in value


def test_a_short_rate_limit_is_waited_out_once(provider, no_waiting):
    provider.answer(httpx.Response(429, json={"retry_after": 1.5}), httpx.Response(204))
    ok, _ = Discord({"webhook_url": "https://discord.com/api/webhooks/1/abc"}).send(big_report(1, 1))
    assert ok
    assert no_waiting == [1.5]
    assert len(provider.sent) == 2


def test_a_long_rate_limit_is_not_waited_for(provider, no_waiting):
    provider.answer(httpx.Response(429, headers={"Retry-After": "600"}))
    ok, detail = Discord({"webhook_url": "https://discord.com/api/webhooks/1/abc"}).send(big_report(1, 1))
    assert not ok
    assert no_waiting == []
    assert "600s" in detail


def test_the_webhook_address_is_not_in_the_error(provider):
    provider.answer(httpx.ConnectError("refused"), httpx.ConnectError("refused"))
    ok, detail = Discord({"webhook_url": "https://discord.com/api/webhooks/1/secretpart_abcdefghijklmnop"}).send(
        big_report(1, 1))
    assert not ok
    assert "secretpart" not in detail
    assert len(provider.sent) == 2, "nothing was sent, so it was tried once more"


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def test_a_long_telegram_message_is_cut_between_lines(provider):
    telegram = Telegram({"bot_token": BOT_TOKEN, "chat_id": "42"})
    ok, detail = telegram.send(big_report())
    assert ok, detail
    text = provider.body()["text"]
    assert len(text) <= 4096
    assert text.count("<b>") == text.count("</b>")
    assert not re.search(r"&[a-z]*$", text.split("\n")[-2]), "no entity cut in half"


def test_the_link_cannot_break_out_of_its_attribute(provider):
    report = big_report(1, 1)
    report.url = 'http://correctarr.example/"><b>x'
    Telegram({"bot_token": BOT_TOKEN, "chat_id": "42"}).send(report)
    assert '&quot;&gt;' in provider.body()["text"]


def test_telegram_waits_out_its_own_retry_after(provider, no_waiting):
    provider.answer(httpx.Response(429, json={"ok": False, "description": "Too Many Requests",
                                              "parameters": {"retry_after": 3}}),
                    httpx.Response(200, json={"ok": True, "result": {}}))
    ok, _ = Telegram({"bot_token": BOT_TOKEN, "chat_id": "42"}).send(big_report(1, 1))
    assert ok
    assert no_waiting == [3.0]


def test_a_topic_id_is_sent_as_a_number(provider):
    Telegram({"bot_token": BOT_TOKEN, "chat_id": "-100", "thread_id": "17"}).send(big_report(1, 1))
    assert provider.body()["message_thread_id"] == 17


def test_the_bot_token_never_appears_in_an_error(provider):
    provider.answer(httpx.ConnectError(f"cannot reach https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"),
                    httpx.ConnectError(f"cannot reach https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"))
    ok, detail = Telegram({"bot_token": BOT_TOKEN, "chat_id": "42"}).send(big_report(1, 1))
    assert not ok
    assert BOT_TOKEN.split(":")[1] not in detail


def test_telegram_says_in_its_own_words_what_is_wrong(provider):
    provider.answer(httpx.Response(400, json={"ok": False,
                                              "description": "Bad Request: chat not found"}))
    ok, detail = Telegram({"bot_token": BOT_TOKEN, "chat_id": "42"}).send(big_report(1, 1))
    assert not ok
    assert detail == "Bad Request: chat not found"


# ---------------------------------------------------------------------------
# Pushover, ntfy, Gotify, webhook
# ---------------------------------------------------------------------------
def test_pushover_stays_under_its_limit_and_renders_markup(provider):
    ok, _ = Pushover({"app_token": "a" * 30, "user_key": "u" * 30}).send(big_report())
    assert ok
    sent = dict(httpx.QueryParams(provider.sent[-1].content.decode()))
    assert len(sent["message"]) <= 1024
    assert sent["html"] == "1"
    assert sent["message"].count("<b>") == sent["message"].count("</b>")


def test_pushover_names_the_problem(provider):
    provider.answer(httpx.Response(400, json={"status": 0, "errors": ["application token is invalid"]}))
    ok, detail = Pushover({"app_token": "a" * 30, "user_key": "u" * 30}).send(big_report(1, 1))
    assert not ok
    assert "application token is invalid" in detail


def test_ntfy_counts_bytes_and_posts_to_the_root(provider):
    report = big_report()
    for group in report.groups:
        for finding in group.findings:
            finding.title = "Über – Ärger 🎬 " * 3
    ok, _ = Ntfy({"server": "https://ntfy.example", "topic": "t"}).send(report)
    assert ok
    assert str(provider.sent[-1].url) == "https://ntfy.example"
    assert len(provider.body()["message"].encode("utf-8")) <= 4096


def test_gotify_error_text_is_the_servers(provider):
    from app.notifications.channels.gotify import Gotify
    provider.answer(httpx.Response(400, json={"error": "Bad Request",
                                              "errorDescription": "x", "message": "priority invalid"}))
    ok, detail = Gotify({"server": "https://gotify.example", "token": "A" * 15}).send(big_report(1, 1))
    assert not ok
    assert "400" in detail


def test_a_webhook_is_not_followed_to_another_address(provider):
    from app.notifications.channels.webhook import Webhook
    provider.answer(httpx.Response(302, headers={"Location": "https://elsewhere.example/"}))
    ok, _ = Webhook({"url": "https://hook.example/", "secret_header": "X-Token",
                     "secret_value": "s3cret-value"}).send(big_report(1, 1))
    assert not ok
    assert len(provider.sent) == 1


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind, config", [
    ("webhook", {"url": "ftp://example.com/hook"}),
    ("webhook", {"url": "javascript:alert(1)"}),
    ("discord", {"webhook_url": "http://discord.com/api/webhooks/1/x"}),
    ("ntfy", {"server": "file:///etc/passwd", "topic": "t"}),
    ("gotify", {"server": "gopher://x", "token": "t"}),
])
def test_an_address_with_the_wrong_scheme_is_refused_when_saved(kind, config):
    with pytest.raises(ValueError, match="url_scheme"):
        notifications.validate(kind, config)


def test_a_stored_secret_follows_only_its_own_server():
    stored = {"server": "https://ntfy.example", "topic": "t", "token": "tk_stored"}
    same = notifications.restore_secrets(
        "ntfy", {"server": "https://ntfy.example/", "topic": "t", "token": "MASK"}, stored, "MASK")
    assert same["token"] == "tk_stored"
    moved = notifications.restore_secrets(
        "ntfy", {"server": "https://collector.example", "topic": "t", "token": "MASK"}, stored, "MASK")
    assert moved["token"] == ""


def test_a_secret_is_restored_for_a_provider_with_a_fixed_address():
    stored = {"app_token": "a" * 30, "user_key": "u" * 30}
    back = notifications.restore_secrets(
        "pushover", {"app_token": "MASK", "user_key": "MASK"}, stored, "MASK")
    assert back == stored
