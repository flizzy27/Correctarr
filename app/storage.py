"""SQLite storage: settings, findings, services, users, sessions.

About the schema
----------------
The schema version lives in ``PRAGMA user_version``. Every structural change is
appended to ``MIGRATIONS`` and applied exactly once at startup. That is what
lets an existing installation survive an update without anyone having to delete
the file.

Two rules for new migrations:

  * A migration that has been released is **never** edited afterwards. Anyone
    who already ran it would not pick the change up.
  * New columns get a default. Otherwise ``ALTER TABLE`` fails on existing rows.

Before running any migration the store writes a backup of the file next to it.
If something goes wrong, the original is still there, untouched.
"""
from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import policy

log = logging.getLogger(__name__)

_lock = threading.RLock()

SCHEMA_VERSION = 2


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------
_M1 = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS findings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    service     TEXT NOT NULL,
    rule        TEXT NOT NULL,
    severity    TEXT NOT NULL,
    title       TEXT NOT NULL,
    description TEXT NOT NULL,
    action      TEXT,
    data        TEXT
);
CREATE INDEX IF NOT EXISTS idx_findings_at   ON findings(at DESC);
CREATE INDEX IF NOT EXISTS idx_findings_rule ON findings(rule, at DESC);

CREATE TABLE IF NOT EXISTS seen (
    key       TEXT PRIMARY KEY,
    last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS services (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL,
    kind    TEXT NOT NULL,
    url     TEXT NOT NULL,
    api_key TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    webhook INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS progress (
    key        TEXT PRIMARY KEY,
    bytes_left INTEGER NOT NULL,
    since      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    found       INTEGER NOT NULL,
    fixed       INTEGER NOT NULL,
    error       TEXT,
    deep        INTEGER NOT NULL DEFAULT 0,
    trigger     TEXT NOT NULL DEFAULT 'schedule'
);

CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    hash       TEXT NOT NULL,
    created    TEXT NOT NULL,
    last_login TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    token   TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created TEXT NOT NULL,
    expires TEXT NOT NULL,
    origin  TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires);
"""

# Version 2: notification connections. Until here there was one hard wired
# Pushover configuration living in the settings table; now any number of
# connections of any kind can be configured, each with its own routing.
_M2 = """
CREATE TABLE IF NOT EXISTS notifications (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 1,
    config       TEXT NOT NULL DEFAULT '{}',
    min_severity TEXT NOT NULL DEFAULT 'warning',
    rules        TEXT NOT NULL DEFAULT '[]',
    categories   TEXT NOT NULL DEFAULT '[]',
    fixed_only   INTEGER NOT NULL DEFAULT 0,
    cooldown     INTEGER NOT NULL DEFAULT 5,
    created      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notifications_enabled ON notifications(enabled);
"""

MIGRATIONS: list[tuple[int, str]] = [
    (1, _M1),
    (2, _M2),
]

# Column mapping used when adopting a store written by a pre-release build.
# Those tables were named in German; the data is worth keeping, the names are
# not.
_LEGACY_TABLES = {
    "einstellungen": ("settings", {"schluessel": "key", "wert": "value"}),
    "protokoll": ("findings", {
        "zeit": "at", "dienst": "service", "regel": "rule", "schwere": "severity",
        "titel": "title", "beschreibung": "description", "aktion": "action",
        "daten": "data"}),
    "gesehen": ("seen", {"kennung": "key", "zuletzt": "last_seen"}),
    "dienste": ("services", {
        "name": "name", "art": "kind", "basis": "url", "schluessel": "api_key",
        "an": "enabled", "webhook": "webhook"}),
    "fortschritt": ("progress", {
        "schluessel": "key", "restbytes": "bytes_left", "seit": "since"}),
    "laeufe": ("runs", {
        "zeit": "at", "dauer_ms": "duration_ms", "funde": "found",
        "behoben": "fixed", "fehler": "error", "tief": "deep"}),
}


class Store:
    """Every access to the database goes through here.

    Connections are opened per call and **closed**. A bare
    ``with sqlite3.connect(...)`` does not close; it only ends the transaction,
    leaving the connection open until the garbage collector gets to it.
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._prepare()
        self._migrate()
        # Deliberately outside _migrate: that returns early when there is
        # nothing to migrate. If the schema step once succeeded and the copy
        # then failed, the version has already moved on, so the next start
        # would skip the migration — and with it the adoption — leaving that
        # data stranded for good. The same reasoning applies to the settings
        # that became a notification connection.
        self._adopt_legacy()
        self._adopt_pushover_settings()

    # -- connection ------------------------------------------------------------
    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=30, isolation_level="DEFERRED")
        c.row_factory = sqlite3.Row
        try:
            with closing(c), c:
                yield c
        except sqlite3.Error:
            log.exception("Database error")
            raise

    def _prepare(self) -> None:
        """One-off settings on the file itself.

        WAL separates reading from writing: the web UI can read while a run is
        writing. Without it the two block each other regularly on a 60 second
        schedule.
        """
        with closing(sqlite3.connect(self.path, timeout=30)) as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=15000")
            c.commit()

    # -- migrations ------------------------------------------------------------
    def version(self) -> int:
        with closing(sqlite3.connect(self.path, timeout=30)) as c:
            return int(c.execute("PRAGMA user_version").fetchone()[0])

    def _migrate(self) -> None:
        current = self.version()
        pending = [(n, sql) for n, sql in MIGRATIONS if n > current]
        if not pending:
            return

        if current > 0 and Path(self.path).exists():
            backup = f"{self.path}.v{current}.backup"
            try:
                shutil.copy2(self.path, backup)
                log.info("Backup written before migrating: %s", backup)
            except OSError as e:
                log.warning("Could not write a backup (%s) — migrating anyway", e)

        for number, sql in pending:
            log.info("Upgrading database to schema version %d", number)
            with closing(sqlite3.connect(self.path, timeout=30)) as c:
                try:
                    c.executescript(sql)
                except sqlite3.OperationalError as e:
                    if "duplicate column name" not in str(e).lower():
                        raise
                    log.info("Schema version %d was already partly present: %s", number, e)
                c.execute(f"PRAGMA user_version={number}")
                c.commit()
        log.info("Database is at schema version %d", self.version())

    def _adopt_legacy(self) -> None:
        """Copy rows over from a store written by a pre-release build.

        Those builds used German table and column names. Renaming them in place
        is not worth the risk, so the rows are copied into the new tables and
        the old ones are dropped once that succeeded.
        """
        with closing(sqlite3.connect(self.path, timeout=30)) as c:
            c.row_factory = sqlite3.Row
            present = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            legacy = [t for t in _LEGACY_TABLES if t in present]
            if not legacy:
                return
            log.info("Adopting data from a previous build: %s", ", ".join(sorted(legacy)))

            for old_table in legacy:
                new_table, columns = _LEGACY_TABLES[old_table]
                have = {r["name"] for r in c.execute(
                    f"PRAGMA table_info({old_table})").fetchall()}
                pairs = [(o, n) for o, n in columns.items() if o in have]
                if not pairs:
                    continue
                old_cols = ", ".join(f'"{o}"' for o, _ in pairs)
                new_cols = ", ".join(f'"{n}"' for _, n in pairs)
                marks = ", ".join("?" for _ in pairs)
                rows = c.execute(f"SELECT {old_cols} FROM {old_table}").fetchall()
                if rows:
                    c.executemany(
                        f"INSERT OR IGNORE INTO {new_table} ({new_cols}) VALUES ({marks})",
                        [tuple(r) for r in rows])
                log.info("  %s → %s: %d rows", old_table, new_table, len(rows))
                c.execute(f"DROP TABLE {old_table}")
            c.commit()

    def _adopt_pushover_settings(self) -> None:
        """Turn the old single Pushover configuration into a connection.

        Up to version 1 there was exactly one notification target, configured
        through four settings. Anyone who had it working should not have to set
        it up again, and should certainly not discover months later that their
        alerts stopped.
        """
        with _lock, self._conn() as c:
            already = c.execute(
                "SELECT COUNT(*) c FROM notifications WHERE kind='pushover'"
            ).fetchone()["c"]
            if already:
                return
            rows = {r["key"]: r["value"] for r in c.execute(
                "SELECT key, value FROM settings WHERE key LIKE 'pushover_%'"
            ).fetchall()}
        if not rows:
            return

        def read(key: str, fallback: Any = "") -> Any:
            try:
                return json.loads(rows[key])
            except (KeyError, json.JSONDecodeError):
                return fallback

        app_token, user_key = read("pushover_app"), read("pushover_user")
        if not (app_token and user_key):
            return

        self.save_notification({
            "name": "Pushover",
            "kind": "pushover",
            "enabled": bool(read("pushover_enabled", False)),
            "config": {"app_token": app_token, "user_key": user_key,
                       "devices": read("pushover_devices"),
                       "sound": read("pushover_sound", "pianobar") or "pianobar"},
            "min_severity": read("pushover_min_severity", "warning") or "warning",
            "fixed_only": bool(read("pushover_fixed_only", False)),
            "cooldown": int(read("pushover_cooldown", 5) or 5),
        })
        with _lock, self._conn() as c:
            c.execute("DELETE FROM settings WHERE key LIKE 'pushover_%'")
        log.info("The Pushover settings became a notification connection")

    # -- notification connections ----------------------------------------------
    @staticmethod
    def _notification_row(row: sqlite3.Row) -> dict:
        def decode(value: str, fallback: Any) -> Any:
            try:
                return json.loads(value)
            except (TypeError, json.JSONDecodeError):
                return fallback

        return {
            "id": row["id"], "name": row["name"], "kind": row["kind"],
            "enabled": bool(row["enabled"]),
            "config": decode(row["config"], {}),
            "min_severity": row["min_severity"],
            "rules": decode(row["rules"], []),
            "categories": decode(row["categories"], []),
            "fixed_only": bool(row["fixed_only"]),
            "cooldown": int(row["cooldown"]),
            "created": row["created"],
        }

    def notifications(self, enabled_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM notifications"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY id"
        with _lock, self._conn() as c:
            return [self._notification_row(r) for r in c.execute(sql).fetchall()]

    def notification(self, notification_id: int) -> dict | None:
        with _lock, self._conn() as c:
            row = c.execute("SELECT * FROM notifications WHERE id=?",
                            (notification_id,)).fetchone()
        return self._notification_row(row) if row else None

    def save_notification(self, entry: dict) -> int:
        fields = (
            str(entry.get("name", "")).strip() or entry.get("kind", "notification"),
            entry.get("kind", "webhook"),
            1 if entry.get("enabled", True) else 0,
            json.dumps(entry.get("config") or {}, ensure_ascii=False),
            entry.get("min_severity") or "warning",
            json.dumps(list(entry.get("rules") or []), ensure_ascii=False),
            json.dumps(list(entry.get("categories") or []), ensure_ascii=False),
            1 if entry.get("fixed_only") else 0,
            int(entry.get("cooldown", 5) or 0),
        )
        with _lock, self._conn() as c:
            if entry.get("id"):
                c.execute(
                    "UPDATE notifications SET name=?,kind=?,enabled=?,config=?,"
                    "min_severity=?,rules=?,categories=?,fixed_only=?,cooldown=? "
                    "WHERE id=?", (*fields, entry["id"]))
                return int(entry["id"])
            cursor = c.execute(
                "INSERT INTO notifications(name,kind,enabled,config,min_severity,"
                "rules,categories,fixed_only,cooldown,created) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)", (*fields, _now()))
            return int(cursor.lastrowid)

    def delete_notification(self, notification_id: int) -> None:
        with _lock, self._conn() as c:
            c.execute("DELETE FROM notifications WHERE id=?", (notification_id,))

    # -- settings --------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        with _lock, self._conn() as c:
            r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if not r:
            return default
        try:
            return json.loads(r["value"])
        except json.JSONDecodeError:
            log.warning("Setting %s is unreadable, falling back to the default", key)
            return default

    def set(self, key: str, value: Any) -> None:
        with _lock, self._conn() as c:
            c.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, json.dumps(value, ensure_ascii=False)))

    def set_many(self, values: dict[str, Any]) -> None:
        """Several settings in one transaction — either all of them or none."""
        if not values:
            return
        with _lock, self._conn() as c:
            c.executemany(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [(k, json.dumps(v, ensure_ascii=False)) for k, v in values.items()])

    def all_settings(self) -> dict:
        with _lock, self._conn() as c:
            rows = c.execute("SELECT key,value FROM settings").fetchall()
        out = {}
        for r in rows:
            try:
                out[r["key"]] = json.loads(r["value"])
            except json.JSONDecodeError:
                log.warning("Setting %s is unreadable and will be skipped", r["key"])
        return out

    # -- findings --------------------------------------------------------------
    def record(self, finding) -> None:
        with _lock, self._conn() as c:
            c.execute("INSERT INTO findings(at,service,rule,severity,title,"
                      "description,action,data) VALUES(?,?,?,?,?,?,?,?)",
                      (_now(), finding.service, finding.rule, finding.severity,
                       finding.title, finding.description, finding.action,
                       json.dumps(finding.data, ensure_ascii=False, default=str)))

    def findings(self, limit: int = 200, rule: str | None = None,
                 fixed_only: bool = False, since: str | None = None) -> list[dict]:
        sql = "SELECT * FROM findings WHERE 1=1"
        params: list[Any] = []
        if rule:
            sql += " AND rule=?"
            params.append(rule)
        if fixed_only:
            sql += " AND action IS NOT NULL"
        if since:
            sql += " AND at >= ?"
            params.append(since)
        sql += " ORDER BY at DESC LIMIT ?"
        params.append(limit)
        with _lock, self._conn() as c:
            return [dict(r) for r in c.execute(sql, params).fetchall()]

    def trim_findings(self, keep: int = 20000, days: int = 0) -> int:
        """Keep the findings table small.

        Without this it grows without bound — measured at 1,053 rows in 22
        hours, so roughly 400,000 a year. Trimmed by count and optionally by
        age. Returns the number of rows removed.
        """
        removed = 0
        with _lock, self._conn() as c:
            if days > 0:
                cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
                removed += c.execute("DELETE FROM findings WHERE at < ?", (cutoff,)).rowcount
            if keep > 0:
                removed += c.execute(
                    "DELETE FROM findings WHERE id NOT IN "
                    "(SELECT id FROM findings ORDER BY id DESC LIMIT ?)", (keep,)).rowcount
        if removed:
            log.info("Removed %d findings", removed)
        return removed

    def compact(self) -> None:
        """Hand freed space back to the filesystem."""
        with closing(sqlite3.connect(self.path, timeout=60)) as c:
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            c.execute("VACUUM")

    # -- runs ------------------------------------------------------------------
    def record_run(self, duration_ms: int, found: int, fixed: int,
                   error: str | None = None, deep: bool = False,
                   trigger: str = "schedule") -> None:
        with _lock, self._conn() as c:
            c.execute("INSERT INTO runs(at,duration_ms,found,fixed,error,deep,trigger) "
                      "VALUES(?,?,?,?,?,?,?)",
                      (_now(), duration_ms, found, fixed, error,
                       1 if deep else 0, trigger))
            c.execute("DELETE FROM runs WHERE id NOT IN "
                      "(SELECT id FROM runs ORDER BY id DESC LIMIT 500)")

    def runs(self, limit: int = 50) -> list[dict]:
        with _lock, self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM runs ORDER BY at DESC LIMIT ?", (limit,)).fetchall()]

    #: What counts as something having actually been changed, in SQL. A dry run
    #: says what it would have done and a failure says it could not — counting
    #: either as work done is how the interface came to report fixes that never
    #: happened, on a badge nobody had reason to distrust.
    #:
    #: The prefixes are taken from :mod:`app.policy` rather than spelled out,
    #: so this and ``policy.really_happened`` cannot drift apart. They are
    #: always English in the column, whatever language the interface is in.
    _REALLY_DONE = ("action IS NOT NULL AND action != '' "
                    f"AND action NOT LIKE '{policy.DRY_PREFIX}%' "
                    f"AND action NOT LIKE '{policy.FAILED_PREFIX}%'")

    def summary(self) -> dict:
        last_24h = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
        done = self._REALLY_DONE
        with _lock, self._conn() as c:
            total = c.execute("SELECT COUNT(*) c FROM findings").fetchone()["c"]
            fixed = c.execute(
                f"SELECT COUNT(*) c FROM findings WHERE {done}").fetchone()["c"]
            recent = c.execute("SELECT COUNT(*) c FROM findings WHERE at >= ?",
                               (last_24h,)).fetchone()["c"]
            recent_fixed = c.execute(
                f"SELECT COUNT(*) c FROM findings WHERE at >= ? AND {done}",
                (last_24h,)).fetchone()["c"]
            per_rule = c.execute("SELECT rule, COUNT(*) c FROM findings "
                                 "GROUP BY rule ORDER BY c DESC").fetchall()
        return {"total": total, "fixed": fixed,
                "last_24h": recent, "last_24h_fixed": recent_fixed,
                "per_rule": {r["rule"]: r["c"] for r in per_rule}}

    def size(self) -> dict:
        p = Path(self.path)
        total = sum(f.stat().st_size for f in
                    (p, Path(f"{self.path}-wal"), Path(f"{self.path}-shm"))
                    if f.exists())
        with _lock, self._conn() as c:
            rows = c.execute("SELECT COUNT(*) c FROM findings").fetchone()["c"]
        return {"bytes": total, "mb": round(total / 1024 ** 2, 2),
                "findings": rows, "schema": self.version()}

    # -- per download progress -------------------------------------------------
    def check_progress(self, key: str, bytes_left: int) -> float:
        """Remember how much of a download is left and return for how many
        minutes that number has not moved. If it moves, the count restarts."""
        now = datetime.now(UTC)
        with _lock, self._conn() as c:
            r = c.execute("SELECT bytes_left, since FROM progress WHERE key=?",
                          (key,)).fetchone()
            if r is None or int(r["bytes_left"]) != int(bytes_left):
                c.execute("INSERT INTO progress(key,bytes_left,since) VALUES(?,?,?) "
                          "ON CONFLICT(key) DO UPDATE SET bytes_left=excluded.bytes_left, "
                          "since=excluded.since", (key, bytes_left, now.isoformat()))
                return 0.0
            try:
                since = datetime.fromisoformat(r["since"])
            except ValueError:
                return 0.0
        return (now - since).total_seconds() / 60

    def prune_progress(self, active: set[str], prefix: str = "") -> None:
        """Drop entries that are no longer in the queue they belong to.

        ``prefix`` names whose queue was just looked at. Without it every
        caller dropped every key it did not recognise — and since the rule that
        watches for stalled downloads runs once per service, two services took
        turns deleting each other's rows. Nothing ever survived to a second
        pass, so nothing was ever measured as standing still, and the rule
        could not fire at all on any install with more than one service.
        """
        with _lock, self._conn() as c:
            known = [r["key"] for r in c.execute("SELECT key FROM progress").fetchall()]
            gone = [(k,) for k in known
                    if k.startswith(prefix) and k not in active]
            if gone:
                c.executemany("DELETE FROM progress WHERE key=?", gone)

    # -- services --------------------------------------------------------------
    def services(self, enabled_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM services"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY id"
        with _lock, self._conn() as c:
            return [dict(r) for r in c.execute(sql).fetchall()]

    def service(self, service_id: int) -> dict | None:
        with _lock, self._conn() as c:
            r = c.execute("SELECT * FROM services WHERE id=?", (service_id,)).fetchone()
        return dict(r) if r else None

    def save_service(self, s: dict) -> int:
        fields = (str(s.get("name", "")).strip(), s.get("kind", "radarr"),
                  str(s.get("url") or "").rstrip("/"), str(s.get("api_key", "")).strip(),
                  1 if s.get("enabled", True) else 0, 1 if s.get("webhook") else 0)
        with _lock, self._conn() as c:
            if s.get("id"):
                c.execute("UPDATE services SET name=?,kind=?,url=?,api_key=?,"
                          "enabled=?,webhook=? WHERE id=?", (*fields, s["id"]))
                return int(s["id"])
            cur = c.execute("INSERT INTO services(name,kind,url,api_key,enabled,webhook) "
                            "VALUES(?,?,?,?,?,?)", fields)
            return int(cur.lastrowid)

    def delete_service(self, service_id: int) -> None:
        with _lock, self._conn() as c:
            c.execute("DELETE FROM services WHERE id=?", (service_id,))

    # -- deduplication of findings ---------------------------------------------
    def is_new(self, key: str, hours: int = 12) -> bool:
        """True when this finding has not been reported for ``hours``.

        Needed because checks run every minute: the same long-running problem —
        a stuck unpack folder, say — would otherwise be recorded and pushed out
        every single minute.
        """
        now = datetime.now(UTC)
        cutoff = now - timedelta(hours=hours)
        with _lock, self._conn() as c:
            r = c.execute("SELECT last_seen FROM seen WHERE key=?", (key,)).fetchone()
            new = True
            if r:
                try:
                    new = datetime.fromisoformat(r["last_seen"]) < cutoff
                except ValueError:
                    new = True
            c.execute("INSERT INTO seen(key,last_seen) VALUES(?,?) "
                      "ON CONFLICT(key) DO UPDATE SET last_seen=excluded.last_seen",
                      (key, now.isoformat()))
        return new

    def prune_seen(self, days: int = 30) -> None:
        """Drop old deduplication entries. Runs once per run, not once per
        finding — it used to scan the table for every single finding."""
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with _lock, self._conn() as c:
            c.execute("DELETE FROM seen WHERE last_seen < ?", (cutoff,))

    # -- users -----------------------------------------------------------------
    def user_count(self) -> int:
        with _lock, self._conn() as c:
            return int(c.execute("SELECT COUNT(*) c FROM users").fetchone()["c"])

    def user_by_name(self, name: str) -> dict | None:
        with _lock, self._conn() as c:
            r = c.execute("SELECT * FROM users WHERE name=? COLLATE NOCASE",
                          (name,)).fetchone()
        return dict(r) if r else None

    def user_by_id(self, user_id: int) -> dict | None:
        with _lock, self._conn() as c:
            r = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(r) if r else None

    def create_user(self, name: str, password_hash: str) -> int:
        with _lock, self._conn() as c:
            cur = c.execute("INSERT INTO users(name,hash,created) VALUES(?,?,?)",
                            (name.strip(), password_hash, _now()))
            return int(cur.lastrowid)

    def set_user_hash(self, user_id: int, password_hash: str) -> None:
        with _lock, self._conn() as c:
            c.execute("UPDATE users SET hash=? WHERE id=?", (password_hash, user_id))

    def note_login(self, user_id: int) -> None:
        with _lock, self._conn() as c:
            c.execute("UPDATE users SET last_login=? WHERE id=?", (_now(), user_id))

    # -- sessions --------------------------------------------------------------
    def create_session(self, token: str, user_id: int, days: int,
                       origin: str = "") -> None:
        expires = (datetime.now(UTC) + timedelta(days=days)).isoformat()
        with _lock, self._conn() as c:
            c.execute("INSERT INTO sessions(token,user_id,created,expires,origin) "
                      "VALUES(?,?,?,?,?)", (token, user_id, _now(), expires, origin[:200]))

    def session(self, token: str) -> dict | None:
        with _lock, self._conn() as c:
            r = c.execute("SELECT * FROM sessions WHERE token=? AND expires > ?",
                          (token, _now())).fetchone()
        return dict(r) if r else None

    def end_session(self, token: str) -> None:
        with _lock, self._conn() as c:
            c.execute("DELETE FROM sessions WHERE token=?", (token,))

    def end_all_sessions(self, user_id: int) -> None:
        """After a password change every previous session stops being valid."""
        with _lock, self._conn() as c:
            c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

    def prune_sessions(self) -> None:
        with _lock, self._conn() as c:
            c.execute("DELETE FROM sessions WHERE expires <= ?", (_now(),))
