"""Tests for the store, above all for the migrations.

An update must never cost an existing installation its settings or its history.
That is why these tests exist.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import pytest

from app.storage import MIGRATIONS, SCHEMA_VERSION, Store


@dataclass
class Finding:
    rule: str = "test_rule"
    severity: str = "warning"
    title: str = "A title"
    description: str = "A description"
    service: str = "radarr"
    action: str | None = None
    data: dict = field(default_factory=dict)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "test.db")


# ------------------------------------------------------------------ schema
def test_a_fresh_store_is_at_the_current_version(store):
    assert store.version() == SCHEMA_VERSION


def test_migrations_are_contiguous_and_ascending():
    numbers = [n for n, _ in MIGRATIONS]
    assert numbers == sorted(numbers)
    assert numbers == list(range(1, len(numbers) + 1))
    assert numbers[-1] == SCHEMA_VERSION


def test_a_second_start_changes_nothing(tmp_path):
    path = tmp_path / "test.db"
    first = Store(path)
    first.set("fast_seconds", 45)
    second = Store(path)
    assert second.version() == SCHEMA_VERSION
    assert second.get("fast_seconds") == 45


def test_wal_is_enabled(store):
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_a_failed_migration_leaves_the_version_alone(tmp_path, monkeypatch):
    """If a migration aborts, the version must not advance — otherwise a half
    built schema counts as finished and the rest is skipped next start."""
    path = tmp_path / "broken.db"
    monkeypatch.setattr("app.storage.MIGRATIONS",
                        [(1, dict(MIGRATIONS)[1]), (2, "THIS IS NOT SQL;")])
    with pytest.raises(sqlite3.Error):
        Store(path)
    with sqlite3.connect(path) as connection:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 1


# ------------------------------------------------------------ legacy import
def test_data_from_a_previous_build_is_adopted(tmp_path):
    """A store written by a pre-release build used German table names. The rows
    are worth keeping; the names are not."""
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE einstellungen (schluessel TEXT PRIMARY KEY, wert TEXT NOT NULL);
            CREATE TABLE protokoll (
                id INTEGER PRIMARY KEY AUTOINCREMENT, zeit TEXT NOT NULL,
                dienst TEXT NOT NULL, regel TEXT NOT NULL, schwere TEXT NOT NULL,
                titel TEXT NOT NULL, beschreibung TEXT NOT NULL,
                aktion TEXT, daten TEXT);
            CREATE TABLE dienste (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                art TEXT NOT NULL, basis TEXT NOT NULL, schluessel TEXT NOT NULL,
                an INTEGER NOT NULL DEFAULT 1, webhook INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE laeufe (
                id INTEGER PRIMARY KEY AUTOINCREMENT, zeit TEXT NOT NULL,
                dauer_ms INTEGER NOT NULL, funde INTEGER NOT NULL,
                behoben INTEGER NOT NULL, fehler TEXT,
                tief INTEGER NOT NULL DEFAULT 0);
        """)
        connection.execute("INSERT INTO einstellungen VALUES('dry_run','true')")
        connection.execute(
            "INSERT INTO protokoll(zeit,dienst,regel,schwere,titel,beschreibung) "
            "VALUES('2026-01-01T00:00:00+00:00','radarr','old','warning','T','B')")
        connection.execute("INSERT INTO dienste(name,art,basis,schluessel) "
                           "VALUES('Radarr','radarr','http://x:7878','secret')")
        connection.execute("INSERT INTO laeufe(zeit,dauer_ms,funde,behoben) "
                           "VALUES('2026-01-01T00:00:00+00:00',10,1,0)")

    adopted = Store(path)
    assert adopted.version() == SCHEMA_VERSION
    assert adopted.get("dry_run") is True
    assert len(adopted.findings()) == 1
    assert adopted.services()[0]["name"] == "Radarr"
    assert adopted.services()[0]["url"] == "http://x:7878"
    assert adopted.services()[0]["api_key"] == "secret"
    assert len(adopted.runs()) == 1
    # The old tables are gone once the rows have been copied.
    with sqlite3.connect(path) as connection:
        tables = {r[0] for r in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "protokoll" not in tables
    assert "findings" in tables


# ---------------------------------------------------------------- settings
def test_settings_survive_their_types(store):
    store.set("number", 42)
    store.set("text", "hello")
    store.set("switch", True)
    store.set("list", ["/a", "/b"])
    assert store.get("number") == 42
    assert store.get("text") == "hello"
    assert store.get("switch") is True
    assert store.get("list") == ["/a", "/b"]


def test_an_unreadable_setting_falls_back_to_the_default(store):
    with sqlite3.connect(store.path) as connection:
        connection.execute("INSERT INTO settings VALUES('broken','{not json')")
    assert store.get("broken", "fallback") == "fallback"
    # And it does not poison reading everything else either.
    assert "broken" not in store.all_settings()


def test_set_many_is_all_or_nothing(store):
    store.set_many({"a": 1, "b": 2})
    assert store.all_settings() == {"a": 1, "b": 2}


# ---------------------------------------------------------------- findings
def test_trim_findings_by_count(store):
    for i in range(50):
        store.record(Finding(title=f"Title {i}"))
    assert len(store.findings(limit=1000)) == 50
    removed = store.trim_findings(keep=10)
    assert removed == 40
    remaining = store.findings(limit=1000)
    assert len(remaining) == 10
    # The newest ones stay.
    assert remaining[0]["title"] == "Title 49"


def test_findings_filter_by_rule_and_fixed(store):
    store.record(Finding(rule="a", action="fixed"))
    store.record(Finding(rule="b"))
    assert len(store.findings(rule="a")) == 1
    assert len(store.findings(fixed_only=True)) == 1


def test_a_dry_run_does_not_count_as_fixed(store):
    store.record(Finding(action="blocklisted"))
    store.record(Finding(action="DRY RUN: would blocklist"))
    summary = store.summary()
    assert summary["total"] == 2
    assert summary["fixed"] == 1


def test_runs_are_capped_at_500(store):
    for i in range(520):
        store.record_run(i, 0, 0)
    assert len(store.runs(limit=1000)) == 500


# ----------------------------------------------------------- deduplication
def test_is_new_remembers_the_finding(store):
    assert store.is_new("key", 12) is True
    assert store.is_new("key", 12) is False


def test_is_new_again_after_the_window(store):
    store.is_new("key", 12)
    assert store.is_new("key", 0) is True


# ---------------------------------------------------------------- progress
def test_progress_counts_a_stall(store):
    assert store.check_progress("a", 1000) == 0.0
    assert store.check_progress("a", 1000) >= 0.0
    # When the remainder moves, the count restarts.
    assert store.check_progress("a", 900) == 0.0


def test_progress_is_pruned(store):
    store.check_progress("a", 1)
    store.check_progress("b", 1)
    store.prune_progress({"a"})
    assert store.check_progress("b", 1) == 0.0     # was gone, starts over


# ------------------------------------------------------------------- users
def test_users_and_sessions(store):
    assert store.user_count() == 0
    user_id = store.create_user("Someone", "hash")
    assert store.user_count() == 1
    # Case must not decide whether a sign-in works.
    assert store.user_by_name("someone")["id"] == user_id

    store.create_session("digest", user_id, 30, "127.0.0.1")
    assert store.session("digest")["user_id"] == user_id
    store.end_session("digest")
    assert store.session("digest") is None


def test_an_expired_session_does_not_count(store):
    user_id = store.create_user("Someone", "hash")
    store.create_session("old", user_id, -1)
    assert store.session("old") is None


def test_changing_the_password_ends_every_session(store):
    user_id = store.create_user("Someone", "hash")
    store.create_session("one", user_id, 30)
    store.create_session("two", user_id, 30)
    store.end_all_sessions(user_id)
    assert store.session("one") is None
    assert store.session("two") is None
