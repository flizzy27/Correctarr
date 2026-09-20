"""Authentication: users, passwords, sessions.

How it works
------------
On the very first start there is no user. The web UI shows the setup page and
creates the first account. After that every page and every API call requires a
session.

The ``AUTH`` environment variable has two positions:

  ``on``   The default. Login required.
  ``off``  No login. Only sensible when something in front — Authelia,
           Authentik, a proxy with basic auth — already handles it.

Why it is built this way
------------------------
* **The password is never stored in the clear.** PBKDF2-HMAC-SHA256 with
  600,000 rounds and a random per-user salt. The round count is part of the
  stored hash so it can be raised later without invalidating old accounts: on
  the next successful login the hash is silently upgraded.
* **The database holds a digest of the session token, not the token.** Anyone
  who gets hold of the file cannot log in with it.
* **Comparisons are constant time** (``hmac.compare_digest``), otherwise the
  response time leaks how many characters matched.
* **Failed attempts are throttled.** After a handful of failures per origin the
  wait grows. That is enough against guessing on a home network.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time

log = logging.getLogger(__name__)

ALGORITHM = "pbkdf2_sha256"
ROUNDS = 600_000
SALT_BYTES = 16
SESSION_DAYS = 30
COOKIE = "correctarr_session"

MIN_PASSWORD_LENGTH = 8

# Failed attempts per origin: (count, time of the last attempt)
_attempts: dict[str, tuple[int, float]] = {}
_attempt_lock = threading.Lock()
THROTTLE_AFTER = 5
THROTTLE_MAX_SECONDS = 300

OBVIOUS_PASSWORDS = frozenset({
    "password", "passwort", "12345678", "correctarr", "abcd1234",
    "qwerty123", "administrator", "changeme", "letmein1",
})


def mode() -> str:
    """``on`` or ``off``. Anything unrecognised counts as ``on`` — when in
    doubt, keep the door shut."""
    raw = (os.getenv("AUTH") or "on").strip().lower()
    return "off" if raw in ("off", "0", "false", "no", "disabled") else "on"


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
def hash_password(password: str, rounds: int = ROUNDS) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    raw = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return "$".join((ALGORITHM, str(rounds),
                     base64.b64encode(salt).decode(),
                     base64.b64encode(raw).decode()))


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, rounds, salt_b64, hash_b64 = stored.split("$", 3)
    except ValueError:
        log.warning("Unreadable password hash")
        return False
    if algorithm != ALGORITHM:
        log.warning("Unknown hash algorithm: %s", algorithm)
        return False
    try:
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        round_count = int(rounds)
    except (ValueError, TypeError):
        return False
    raw = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, round_count)
    return hmac.compare_digest(raw, expected)


def needs_rehash(stored: str) -> bool:
    """Was this hashed with fewer rounds than we use today?"""
    try:
        algorithm, rounds, _, _ = stored.split("$", 3)
        return algorithm != ALGORITHM or int(rounds) < ROUNDS
    except (ValueError, TypeError):
        return True


def password_problem(password: str, username: str = "") -> str | None:
    """Returns a translation key when the password will not do, else None.

    Deliberately short: length beats special characters. Enforced character
    classes reliably produce ``Password1!`` and nothing better.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        return "error.password_too_short"
    if password.strip() != password:
        return "error.password_whitespace"
    if username and password.lower() == username.lower():
        return "error.password_is_username"
    if password.lower() in OBVIOUS_PASSWORDS:
        return "error.password_obvious"
    return None


def username_problem(name: str) -> str | None:
    name = (name or "").strip()
    if len(name) < 3:
        return "error.username_too_short"
    if len(name) > 64:
        return "error.username_too_long"
    if any(c in name for c in "\t\n\r\x00"):
        return "error.username_bad_characters"
    return None


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
def new_token() -> tuple[str, str]:
    """Returns (token for the cookie, digest for the database)."""
    token = secrets.token_urlsafe(32)
    return token, digest(token)


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Throttling
# ---------------------------------------------------------------------------
def retry_after(origin: str) -> int:
    """Seconds this origin still has to wait. 0 means go ahead."""
    with _attempt_lock:
        count, last = _attempts.get(origin, (0, 0.0))
    if count < THROTTLE_AFTER:
        return 0
    # Doubles per failure above the threshold, capped. The exponent is capped
    # too: without that, a few thousand attempts would ask Python to build a
    # number with a few thousand digits before min() throws it away.
    steps = min(count - THROTTLE_AFTER + 1, 16)
    window = min(THROTTLE_MAX_SECONDS, 2 ** steps)
    remaining = window - (time.monotonic() - last)
    return max(0, int(remaining))


def note_failure(origin: str) -> None:
    with _attempt_lock:
        count, _ = _attempts.get(origin, (0, 0.0))
        _attempts[origin] = (count + 1, time.monotonic())
        # The map must not grow without bound.
        if len(_attempts) > 2000:
            oldest = sorted(_attempts.items(), key=lambda kv: kv[1][1])[:1000]
            for key, _ in oldest:
                _attempts.pop(key, None)


def note_success(origin: str) -> None:
    with _attempt_lock:
        _attempts.pop(origin, None)
