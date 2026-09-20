"""Tests for authentication, the settings schema and the translations.

All three are places where a mistake does damage right away: a weak hash, an
unvalidated setting, or a missing translation showing up as a raw key in
somebody's browser.
"""
from __future__ import annotations

import pytest

from app import auth, i18n
from app import settings as S
from app.rules import ALL, CATEGORIES


# ------------------------------------------------------------------ passwords
def test_a_hash_never_contains_the_password():
    stored = auth.hash_password("mypassword123")
    assert "mypassword123" not in stored
    assert stored.startswith("pbkdf2_sha256$")


def test_the_same_password_gives_different_hashes():
    """A random per-user salt — otherwise comparing hashes reveals who shares a
    password."""
    assert auth.hash_password("identical") != auth.hash_password("identical")


def test_verify_accepts_the_right_one_and_rejects_the_rest():
    stored = auth.hash_password("a-very-long-password")
    assert auth.verify_password("a-very-long-password", stored) is True
    assert auth.verify_password("a-very-long-passwor", stored) is False
    assert auth.verify_password("", stored) is False


@pytest.mark.parametrize("broken", ["", "no-dollar", "a$b$c", "unknown$1$x$y"])
def test_a_broken_hash_is_rejected(broken):
    assert auth.verify_password("anything", broken) is False


def test_needs_rehash_spots_too_few_rounds():
    assert auth.needs_rehash(auth.hash_password("x", rounds=1000)) is True
    assert auth.needs_rehash(auth.hash_password("x")) is False


@pytest.mark.parametrize("password,should_complain", [
    ("short", True),
    ("1234567", True),
    ("12345678", True),            # too obvious
    ("password", True),
    (" with spaces ", True),
    ("anordinarypassphrase", False),
    ("A sentence as a password!", False),
])
def test_password_problem(password, should_complain):
    assert (auth.password_problem(password) is not None) is should_complain


def test_the_password_must_not_be_the_username():
    assert auth.password_problem("someperson", "SomePerson") is not None


@pytest.mark.parametrize("name,should_complain", [
    ("ab", True), ("abc", False), ("x" * 65, True), ("with\ttab", True),
])
def test_username_problem(name, should_complain):
    assert (auth.username_problem(name) is not None) is should_complain


def test_every_auth_problem_has_a_translation():
    """A problem key that has no string would reach the user as a raw key."""
    english = i18n.bundle("en")
    for password in ("short", "password", " x ", "12345678"):
        key = auth.password_problem(password, "user")
        if key:
            assert key in english, key
    for name in ("ab", "x" * 70, "with\ttab"):
        key = auth.username_problem(name)
        if key:
            assert key in english, key


# ------------------------------------------------------------------ sessions
def test_the_token_and_its_digest_differ():
    """The database holds the digest, not the token. Anyone who gets the file
    cannot sign in with it."""
    token, stored = auth.new_token()
    assert token != stored
    assert auth.digest(token) == stored
    assert len(token) >= 32


def test_tokens_do_not_repeat():
    assert len({auth.new_token()[0] for _ in range(200)}) == 200


def test_throttling_kicks_in_only_after_several_failures():
    origin = "test-throttle-1"
    for _ in range(auth.THROTTLE_AFTER - 1):
        auth.note_failure(origin)
    assert auth.retry_after(origin) == 0
    auth.note_failure(origin)
    assert auth.retry_after(origin) > 0
    auth.note_success(origin)
    assert auth.retry_after(origin) == 0


# ------------------------------------------------------------------ settings
def test_every_field_belongs_to_a_known_group():
    for field in S.FIELDS:
        assert field.group in S.GROUPS, field.key


def test_keys_are_unique():
    keys = [f.key for f in S.FIELDS]
    assert len(keys) == len(set(keys))


def test_every_default_passes_its_own_validation():
    """A default that fails its own check makes the interface unusable the first
    time anyone saves."""
    for field in S.FIELDS:
        field.validate(field.default)


@pytest.mark.parametrize("key,value", [
    ("fast_seconds", 5),              # below the minimum
    ("fast_seconds", 99999),          # above the maximum
    ("fast_seconds", "not a number"),
    ("pushover_min_severity", "immediately"),
    ("theme", "light"),               # there is deliberately no light theme
    ("path_downloads", "relative/without/slash"),
])
def test_invalid_values_are_rejected(key, value):
    with pytest.raises(ValueError):
        S.validate(key, value)


@pytest.mark.parametrize("key,value,expected", [
    ("fast_seconds", "45", 45),
    ("dry_run", "yes", True),
    ("dry_run", 0, False),
    ("title_similarity", "0.5", 0.5),
    ("extra_cleanup_paths", "/a\n \n/b", ["/a", "/b"]),
    ("path_downloads", "  /downloads  ", "/downloads"),
])
def test_valid_values_are_coerced(key, value, expected):
    assert S.validate(key, value) == expected


def test_an_unknown_key_is_rejected():
    with pytest.raises(ValueError):
        S.validate("does_not_exist", 1)


def test_secrets_are_masked():
    """There are no secrets in the settings any more — they live on the
    notification connections. The machinery stays because a future setting
    might need it, so it is still tested."""
    assert frozenset() == S.SECRETS, (
        "a secret setting was added; check it is masked on the way out")
    assert S.mask({"fast_seconds": 60}) == {"fast_seconds": 60}


def test_the_mask_coming_back_is_only_honoured_for_secrets():
    assert S.unmask("fast_seconds", S.MASK, 60) == S.MASK


# ------------------------------------------------------------ cleanup paths
def test_cleanup_paths_are_derived():
    config = {"path_downloads": "/downloads", "path_incomplete": "/incomplete",
              "extra_cleanup_paths": ["/extra"]}
    assert S.cleanup_paths(config) == ["/downloads", "/incomplete", "/extra"]


@pytest.mark.parametrize("dangerous", ["/", "", "/mnt", "/mnt/user", "/config",
                                       "/app", "/etc", "/usr", "relative"])
def test_dangerous_paths_never_enter_the_list(dangerous):
    """Anyone who types "/" here would delete half the system."""
    config = {"path_downloads": dangerous, "path_incomplete": "",
              "extra_cleanup_paths": [dangerous]}
    assert S.cleanup_paths(config) == []


def test_duplicate_paths_appear_once():
    config = {"path_downloads": "/downloads", "path_incomplete": "/downloads/",
              "extra_cleanup_paths": ["/downloads"]}
    assert S.cleanup_paths(config) == ["/downloads"]


# -------------------------------------------------------------- translations
def test_every_language_is_complete():
    """A key added on one side and forgotten on the other fails the build here
    rather than surfacing as a raw key in somebody's browser."""
    missing = i18n.missing_keys()
    for code, keys in missing.items():
        assert not keys, f"{code} is missing: {keys[:10]}"


def test_no_language_has_keys_english_does_not():
    extra = i18n.extra_keys()
    for code, keys in extra.items():
        assert not keys, f"{code} has extra keys: {keys[:10]}"


def test_every_setting_has_a_label_and_help_text():
    english = i18n.bundle("en")
    for field in S.FIELDS:
        assert f"settings.{field.key}.label" in english, field.key
        assert f"settings.{field.key}.help" in english, field.key


def test_every_rule_has_a_title_and_help_text():
    english = i18n.bundle("en")
    for rule in ALL:
        assert f"rules.{rule.name}.title" in english, rule.name
        assert f"rules.{rule.name}.help" in english, rule.name


def test_every_category_has_a_name():
    english = i18n.bundle("en")
    for category in CATEGORIES:
        assert f"category.{category}" in english, category


def test_every_theme_has_a_name():
    english = i18n.bundle("en")
    for theme in S.THEMES:
        assert f"theme.{theme}" in english, theme


def test_every_rule_belongs_to_a_known_category():
    for rule in ALL:
        assert rule.category in CATEGORIES, rule.name


def test_a_rule_that_cannot_act_does_not_fix_by_default():
    """Otherwise the interface would offer a fix toggle that does nothing."""
    for rule in ALL:
        if rule.fix_by_default:
            assert rule.modifies, rule.name


def test_rule_names_are_unique():
    names = [r.name for r in ALL]
    assert len(names) == len(set(names))


def test_there_are_twenty_six_rules():
    """A guard against a rule silently disappearing during a refactor."""
    assert len(ALL) == 26


@pytest.mark.parametrize("header,expected", [
    ("de-DE,de;q=0.9,en;q=0.8", "de"),
    ("en-US,en;q=0.9", "en"),
    ("fr-FR,fr;q=0.9", "en"),            # no French bundle, falls back
    ("de-AT", "de"),
    ("", "en"),
    (None, "en"),
])
def test_language_negotiation(header, expected):
    assert i18n.negotiate(header) == expected


def test_an_explicit_setting_beats_the_header():
    assert i18n.resolve("de", "en-US,en;q=0.9") == "de"
    assert i18n.resolve("auto", "de-DE,de;q=0.9") == "de"
    assert i18n.resolve(None, "de-DE") == "de"


def test_a_missing_key_returns_the_key():
    """Visible in the interface rather than invisible."""
    assert i18n.t("no.such.key", "en") == "no.such.key"


def test_placeholders_are_filled():
    text = i18n.t("finding.stalled", "en", minutes=30, percent="42")
    assert "30" in text and "42" in text


def test_a_placeholder_mismatch_does_not_raise():
    assert i18n.t("finding.stalled", "en") == i18n.bundle("en")["finding.stalled"]
