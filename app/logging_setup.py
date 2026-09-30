"""Logging setup — with redaction of credentials.

Why this is needed: httpx logs every request including the full URL at INFO
level, and SABnzbd expects its API key as part of the URL. The result was the
key appearing in clear text in ``docker logs`` several times a minute — and so
in every log excerpt anyone pastes into a forum thread while asking for help.

Three measures:

  1. httpx is moved to WARNING. Requests are logged here instead, briefly and
     without secrets.
  2. A filter sits over all output and redacts anything that looks like a key
     anyway, so third party code cannot leak one either — including the text
     of an exception with its traceback.
  3. The same filter sits on uvicorn's own loggers. uvicorn sets up its
     handlers before this module runs and does not pass its records on, so a
     filter on the root handlers never saw them — and with ``LOGLEVEL=DEBUG``
     the access log printed every webhook call with its token.
"""
from __future__ import annotations

import logging
import os
import re

REDACTED = "***"

#: In every pattern the **last** group is the secret. Everything around it
#: stays, so a redacted line still says which key it was.
_PATTERNS = (
    # apikey=… / api_key=… / token=… in URLs and free text, "token": "…" in JSON
    re.compile(r"(?i)\b(api[-_]?key|apikey|token|app[-_]?token|bot[-_]?token|"
               r"password|passwd|secret[-_]?value|user[-_]?key|x-gotify-key)"
               r"(=|\"?'?\s*[:=]\s*\"?'?)([A-Za-z0-9_\-]{8,})"),
    re.compile(r"(?i)(X-Api-Key['\"\s:=]+)([A-Za-z0-9_\-]{8,})"),
    # A Telegram bot token is part of the URL path: /bot123456:AAF…/sendMessage
    re.compile(r"(bot\d{5,}:)([A-Za-z0-9_\-]{20,})"),
    # A Discord webhook carries its secret in the path as well.
    re.compile(r"(?i)(/api/webhooks/\d+/)([A-Za-z0-9_\-]{20,})"),
    re.compile(r"(?i)(\bBearer\s+)([A-Za-z0-9_\-.~+/=]{8,})"),
    # user:password@host in an address
    re.compile(r"(://[^/\s:@]+:)([^@\s/]+)(?=@)"),
)


def redact(text: str) -> str:
    for pattern in _PATTERNS:
        text = pattern.sub(_keep_all_but_the_secret, text)
    return text


def _keep_all_but_the_secret(match: re.Match) -> str:
    whole = match.group(0)
    start = match.start(match.lastindex) - match.start(0)
    end = match.end(match.lastindex) - match.start(0)
    return whole[:start] + REDACTED + whole[end:]


class Redactor(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: redact(v) if isinstance(v, str) else v
                                   for k, v in record.args.items()}
                else:
                    record.args = tuple(redact(a) if isinstance(a, str) else a
                                        for a in record.args)
            if record.exc_info and not record.exc_text:
                # The formatter uses this text when it is already there, so
                # the traceback is rendered once, here, and redacted.
                record.exc_text = redact(
                    logging.Formatter().formatException(record.exc_info))
        except Exception:                                       # noqa: BLE001
            # Logging must never be the reason something crashes.
            pass
        return True


def configure() -> None:
    level = (os.getenv("LOGLEVEL") or "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-7s %(name)-20s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    redactor = Redactor()
    for handler in logging.getLogger().handlers:
        handler.addFilter(redactor)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, Redactor) for f in logger.filters):
            logger.addFilter(redactor)

    # We log our own requests, without the key in the URL.
    for name in ("httpx", "httpcore", "hpack"):
        logging.getLogger(name).setLevel(logging.WARNING)
    # The uvicorn access log only repeats what we already know and floods the
    # output on a 60 second schedule.
    logging.getLogger("uvicorn.access").setLevel(
        logging.INFO if level == "DEBUG" else logging.WARNING)
    logging.getLogger("apscheduler.scheduler").setLevel(logging.WARNING)
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
