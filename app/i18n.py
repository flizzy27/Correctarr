"""Translations.

Every string the user can see lives in ``app/locales/<code>.json`` and is
referenced by a key. Nothing user facing is written into the Python or
JavaScript source, which keeps the two languages honest: a missing translation
shows up as its key rather than quietly falling back to the other language in
the middle of a sentence.

The active language comes from, in order:

  1. the ``language`` setting, when it is anything other than ``auto``
  2. the ``Accept-Language`` header of the request
  3. English

English is the reference. ``de.json`` is checked against it by the test suite,
so a key added on one side and forgotten on the other fails the build.
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

LOCALE_DIR = Path(__file__).parent / "locales"
DEFAULT = "en"

# Only languages that actually have a file. Adding one means dropping a file in
# and extending this tuple.
AVAILABLE = ("en", "de")

LANGUAGE_NAMES = {"en": "English", "de": "Deutsch"}


@lru_cache(maxsize=8)
def bundle(language: str) -> dict[str, str]:
    """All strings for one language, flattened to ``a.b.c`` keys."""
    code = language if language in AVAILABLE else DEFAULT
    path = LOCALE_DIR / f"{code}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.error("Could not read locale %s: %s", code, e)
        return {} if code == DEFAULT else bundle(DEFAULT)
    return _flatten(raw)


def _flatten(node: Any, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in node.items():
        full = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out.update(_flatten(value, full))
        else:
            out[full] = str(value)
    return out


def t(key: str, language: str = DEFAULT, /, **fields: Any) -> str:
    """Look a key up. Falls back to English, then to the key itself.

    Returning the key rather than an empty string is deliberate: a missing
    translation should be obvious in the interface, not invisible.

    ``key`` and ``language`` are positional only. Placeholders called ``key``
    or ``language`` are entirely reasonable — ``error.unknown_setting`` uses
    ``{key}`` — and without the marker they collide with the parameter names
    and raise a TypeError instead of returning a string.
    """
    text = bundle(language).get(key)
    if text is None and language != DEFAULT:
        text = bundle(DEFAULT).get(key)
    if text is None:
        log.debug("Missing translation: %s", key)
        return key
    if not fields:
        return text
    try:
        return text.format(**fields)
    except (KeyError, IndexError, ValueError):
        log.warning("Placeholder mismatch in %s", key)
        return text


def negotiate(header: str | None) -> str:
    """Pick a language from an ``Accept-Language`` header.

    Handles the usual ``de-DE,de;q=0.9,en;q=0.8`` shape. Region subtags are
    dropped, so ``de-AT`` still lands on German.
    """
    if not header:
        return DEFAULT
    ranked: list[tuple[float, str]] = []
    for part in header.split(","):
        piece = part.strip()
        if not piece:
            continue
        code, _, params = piece.partition(";")
        weight = 1.0
        if params.strip().startswith("q="):
            try:
                weight = float(params.strip()[2:])
            except ValueError:
                weight = 0.0
        # q=0 means "not acceptable", so it must not win by being the only
        # entry left.
        if weight <= 0:
            continue
        base = code.strip().split("-")[0].lower()
        if base in AVAILABLE:
            ranked.append((weight, base))
    if not ranked:
        return DEFAULT
    return max(ranked, key=lambda x: x[0])[1]


def resolve(setting: str | None, header: str | None) -> str:
    """The language for this request."""
    if setting and setting != "auto" and setting in AVAILABLE:
        return setting
    return negotiate(header)


def missing_keys() -> dict[str, list[str]]:
    """Which keys a translation is missing compared to English.

    Used by the test suite so that a forgotten translation fails the build
    instead of surfacing as a raw key in someone's browser.
    """
    reference = set(bundle(DEFAULT))
    return {code: sorted(reference - set(bundle(code)))
            for code in AVAILABLE if code != DEFAULT}


def extra_keys() -> dict[str, list[str]]:
    """Keys a translation has that English does not — usually a typo."""
    reference = set(bundle(DEFAULT))
    return {code: sorted(set(bundle(code)) - reference)
            for code in AVAILABLE if code != DEFAULT}
