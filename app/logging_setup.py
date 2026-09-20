"""Logging setup — with redaction of credentials.

Why this is needed: httpx logs every request including the full URL at INFO
level, and SABnzbd expects its API key as part of the URL. The result was the
key appearing in clear text in ``docker logs`` several times a minute — and so
in every log excerpt anyone pastes into a forum thread while asking for help.

Two measures:

  1. httpx is moved to WARNING. Requests are logged here instead, briefly and
     without secrets.
  2. A filter sits over all output and redacts anything that looks like a key
     anyway, so third party code cannot leak one either.
"""
from __future__ import annotations

import logging
import os
import re

REDACTED = "***"

_PATTERNS = (
    # apikey=… / api_key=… / token=… in URLs and free text
    re.compile(r"(?i)\b(api[-_]?key|apikey|token|password|passwd|user[-_]?key)"
               r"(=|\"?\s*[:=]\s*\"?)([A-Za-z0-9_\-]{8,})"),
    re.compile(r"(?i)(X-Api-Key['\"\s:=]+)([A-Za-z0-9_\-]{8,})"),
)


def redact(text: str) -> str:
    for pattern in _PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + m.group(2) + REDACTED, text)
    return text


class Redactor(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str) and ("=" in record.msg or ":" in record.msg):
                record.msg = redact(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: redact(v) if isinstance(v, str) else v
                                   for k, v in record.args.items()}
                else:
                    record.args = tuple(redact(a) if isinstance(a, str) else a
                                        for a in record.args)
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

    # We log our own requests, without the key in the URL.
    for name in ("httpx", "httpcore", "hpack"):
        logging.getLogger(name).setLevel(logging.WARNING)
    # The uvicorn access log only repeats what we already know and floods the
    # output on a 60 second schedule.
    logging.getLogger("uvicorn.access").setLevel(
        logging.INFO if level == "DEBUG" else logging.WARNING)
    logging.getLogger("apscheduler.scheduler").setLevel(logging.WARNING)
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
