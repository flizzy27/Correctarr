"""Settings, rules, services and notifications as one file, and back.

What is in here is what somebody would have to set up again by hand after
losing ``/config``: every setting, what every rule is set to do, the services
and the notification connections. The findings, the history and the accounts
are not — they are a record of what happened on one installation, not a
configuration to carry to another.

Secrets are left out unless asked for. A backup is a file that gets copied to
places, and an API key in it is an API key in all of those places. Restoring
one without them keeps whatever key is already stored for the same service or
connection, so the common case — the same installation, set up again — needs
no keys at all.

A restore never deletes. Anything in the store that the backup does not
mention stays as it is; what the backup does mention is checked against the
same rules the interface is held to, and whatever fails is skipped and said
so, rather than taking the rest of the file down with it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from . import connection, notifications, policy
from . import settings as S
from .rules import BY_NAME, CATEGORIES

FORMAT = "correctarr-backup"
FORMAT_VERSION = 1

SEVERITIES = ("info", "warning", "error")
RULE_FIELDS = ("enabled", "action", *policy.CONDITIONS)
SERVICE_FIELDS = ("url", "enabled", "webhook")
CONNECTION_FIELDS = ("enabled", "config", "min_severity", "rules", "categories",
                     "fixed_only", "cooldown")


def export(store, config: dict, rules: dict, *, secrets: bool = False,
           version: str = "") -> dict:
    """Everything worth keeping, as plain data."""
    services = []
    for entry in store.services():
        services.append({
            "name": entry["name"], "kind": entry["kind"], "url": entry["url"],
            "api_key": entry["api_key"] if secrets else "",
            "enabled": bool(entry["enabled"]), "webhook": bool(entry["webhook"])})
    connections = []
    for entry in store.notifications():
        config_ = dict(entry.get("config") or {})
        if not secrets:
            for key in notifications.secret_keys(entry["kind"]):
                if key in config_:
                    config_[key] = ""
        connections.append({
            "name": entry["name"], "kind": entry["kind"],
            "enabled": entry["enabled"], "config": config_,
            "min_severity": entry["min_severity"], "rules": entry["rules"],
            "categories": entry["categories"], "fixed_only": entry["fixed_only"],
            "cooldown": entry["cooldown"]})
    values = {f.key: config.get(f.key, f.default) for f in S.FIELDS}
    if not secrets:
        values = {k: ("" if k in S.SECRETS else v) for k, v in values.items()}
    return {
        "format": FORMAT, "format_version": FORMAT_VERSION,
        "version": version, "created": datetime.now(UTC).isoformat(),
        "secrets": secrets,
        "settings": values,
        "rules": {name: {k: entry.get(k) for k in RULE_FIELDS if k in entry}
                  for name, entry in rules.items()},
        "services": services,
        "notifications": connections,
    }


@dataclass
class Plan:
    """What a restore would change, and exactly what it would write."""
    settings: dict = field(default_factory=dict)
    rules: dict = field(default_factory=dict)
    services: list = field(default_factory=list)
    connections: list = field(default_factory=list)
    changes: dict = field(default_factory=lambda: {
        "settings": [], "rules": [], "services": [], "notifications": []})
    #: ``(section, name, reason key, fields for the reason)``
    skipped: list = field(default_factory=list)

    @property
    def count(self) -> int:
        return sum(len(v) for v in self.changes.values())

    @property
    def reschedules(self) -> bool:
        return any(S.BY_KEY[k].reschedules for k in self.settings)


def plan(store, config: dict, rules: dict, backup: Any,
         service_kinds: tuple[str, ...]) -> Plan:
    """Check a backup against what is stored now. Raises ValueError with a key.

    ``config`` and ``rules`` are the current effective values, so that a
    setting left at its default and the same default in the backup do not
    count as a change.
    """
    if not isinstance(backup, dict) or backup.get("format") != FORMAT:
        raise ValueError("error.backup_unreadable")
    if int(backup.get("format_version") or 0) > FORMAT_VERSION:
        raise ValueError("error.backup_too_new")

    result = Plan()
    _plan_settings(result, config, backup.get("settings"))
    _plan_rules(result, rules, backup.get("rules"))
    _plan_services(result, store, backup.get("services"), service_kinds)
    _plan_connections(result, store, backup.get("notifications"))
    return result


def _plan_settings(result: Plan, config: dict, incoming: Any) -> None:
    if not isinstance(incoming, dict):
        return
    for key, value in incoming.items():
        field_ = S.BY_KEY.get(key)
        if field_ is None:
            result.skipped.append(("settings", key, "backup.skip_unknown", {}))
            continue
        # A secret left out of the backup is not a request to clear it.
        if key in S.SECRETS and value in ("", None, S.MASK):
            continue
        try:
            clean = field_.validate(value)
        except ValueError as e:
            result.skipped.append(("settings", key, "backup.skip_invalid",
                                   {"reason": str(e)}))
            continue
        if clean == config.get(key, field_.default):
            continue
        result.settings[key] = clean
        result.changes["settings"].append(
            {"key": key, "from": _shown(key, config.get(key, field_.default)),
             "to": _shown(key, clean)})


def _shown(key: str, value: Any) -> Any:
    """A value as it may be shown: a stored secret only as the placeholder."""
    return S.MASK if key in S.SECRETS and value else value


def _plan_rules(result: Plan, current: dict, incoming: Any) -> None:
    if not isinstance(incoming, dict):
        return
    for name, submitted in incoming.items():
        rule = BY_NAME.get(name)
        if rule is None or not isinstance(submitted, dict):
            result.skipped.append(("rules", str(name), "backup.skip_unknown", {}))
            continue
        before = dict(current.get(name) or {})
        after = dict(before)
        if "enabled" in submitted:
            after["enabled"] = bool(submitted["enabled"])
        # Conditions a rule cannot answer are dropped rather than refused: a
        # backup carries every condition for every rule, at zero where the
        # rule has none.
        wanted = {k: submitted[k] for k in ("action", *rule.conditions)
                  if k in submitted}
        if wanted:
            wanted.setdefault("action", before.get("action", policy.REPORT))
            try:
                after.update(policy.validate(wanted, rule.actions, rule.conditions))
            except ValueError as e:
                result.skipped.append(("rules", name, "backup.skip_invalid",
                                       {"reason": str(e)}))
                continue
        changed = [k for k in RULE_FIELDS if after.get(k) != before.get(k)]
        if not changed:
            continue
        result.rules[name] = after
        for key in changed:
            result.changes["rules"].append(
                {"rule": name, "field": key, "from": before.get(key),
                 "to": after.get(key)})


def _same(entry: dict, name: str, kind: str) -> bool:
    return (entry.get("kind") == kind
            and str(entry.get("name") or "").strip().lower() == name.strip().lower())


def _plan_services(result: Plan, store, incoming: Any,
                   kinds: tuple[str, ...]) -> None:
    if not isinstance(incoming, list):
        return
    existing = store.services()
    for submitted in incoming:
        if not isinstance(submitted, dict):
            continue
        name = str(submitted.get("name") or "").strip()[:64]
        kind = str(submitted.get("kind") or "")
        url = str(submitted.get("url") or "").strip().rstrip("/")
        if kind not in kinds:
            result.skipped.append(("services", name or kind, "backup.skip_unknown", {}))
            continue
        if not name or not url.startswith(("http://", "https://")):
            result.skipped.append(("services", name or kind, "backup.skip_invalid",
                                   {"reason": "error.url_scheme"}))
            continue
        match = next((e for e in existing if _same(e, name, kind)), None)
        key = str(submitted.get("api_key") or "").strip()
        if key == S.MASK:
            key = ""
        # The stored key goes only to the address it was entered for, as it
        # does when the service is saved by hand: a backup is a file anybody
        # can edit, and one naming another host must not carry the key there.
        if not key and (match is None or connection.address(match["url"])
                        != connection.address(url)):
            result.skipped.append(("services", name, "backup.skip_needs_key", {}))
            continue
        entry = {"name": name, "kind": kind, "url": url,
                 "api_key": key or match["api_key"],
                 "enabled": bool(submitted.get("enabled", True)),
                 "webhook": bool(submitted.get("webhook", False))}
        if match is None:
            result.services.append(entry)
            result.changes["services"].append(
                {"name": name, "kind": kind, "change": "add", "fields": []})
            continue
        before = {"url": match["url"], "enabled": bool(match["enabled"]),
                  "webhook": bool(match["webhook"])}
        changed = [k for k in SERVICE_FIELDS if entry[k] != before[k]]
        if entry["api_key"] != match["api_key"]:
            changed.append("api_key")
        if not changed:
            continue
        result.services.append({**entry, "id": match["id"]})
        result.changes["services"].append(
            {"name": name, "kind": kind, "change": "update", "fields": changed})


def _plan_connections(result: Plan, store, incoming: Any) -> None:
    if not isinstance(incoming, list):
        return
    existing = store.notifications()
    for submitted in incoming:
        if not isinstance(submitted, dict):
            continue
        name = str(submitted.get("name") or "").strip()[:64]
        kind = str(submitted.get("kind") or "")
        if kind not in notifications.KINDS:
            result.skipped.append(("notifications", name or kind,
                                   "backup.skip_unknown", {}))
            continue
        match = next((e for e in existing if _same(e, name or kind, kind)), None)
        config = dict(submitted.get("config") or {})
        # An empty secret is one the backup left out. Marked as unchanged, the
        # stored one is put back — or, for a new connection, it stays empty
        # and the check below says it is missing.
        for secret in notifications.secret_keys(kind):
            if not config.get(secret):
                config[secret] = S.MASK
        config = notifications.restore_secrets(
            kind, config, (match or {}).get("config") or {}, S.MASK)
        for secret in notifications.secret_keys(kind):
            if config.get(secret) == S.MASK:
                config[secret] = ""
        try:
            config = notifications.validate(kind, config)
        except ValueError as e:
            missing = str(e).startswith("error.channel_field_missing")
            result.skipped.append((
                "notifications", name or kind,
                "backup.skip_needs_key" if missing else "backup.skip_invalid",
                {} if missing else {"reason": str(e)}))
            continue
        severity = str(submitted.get("min_severity") or "warning")
        try:
            cooldown = max(0, min(1440, int(submitted.get("cooldown", 5))))
        except (TypeError, ValueError):
            cooldown = 5
        entry = {
            "name": name or kind.capitalize(), "kind": kind,
            "enabled": bool(submitted.get("enabled", True)), "config": config,
            "min_severity": severity if severity in SEVERITIES else "warning",
            # A rule or category this build does not have is dropped from the
            # filter. Kept, it would match nothing; the connection would go
            # quiet about exactly the thing it was set up for.
            "rules": [r for r in (submitted.get("rules") or []) if r in BY_NAME],
            "categories": [c for c in (submitted.get("categories") or [])
                           if c in CATEGORIES],
            "fixed_only": bool(submitted.get("fixed_only", False)),
            "cooldown": cooldown}
        if match is None:
            result.connections.append(entry)
            result.changes["notifications"].append(
                {"name": entry["name"], "kind": kind, "change": "add", "fields": []})
            continue
        changed = [k for k in CONNECTION_FIELDS if entry[k] != match.get(k)]
        if not changed:
            continue
        result.connections.append({**entry, "id": match["id"]})
        result.changes["notifications"].append(
            {"name": entry["name"], "kind": kind, "change": "update",
             "fields": changed})


def apply(store, result: Plan, rules: dict) -> None:
    """Write what the plan says. ``rules`` is every rule's current setting."""
    if result.settings:
        store.set_many(result.settings)
    if result.rules:
        store.set("rules", {**rules, **result.rules})
    for entry in result.services:
        store.save_service(entry)
    for entry in result.connections:
        store.save_notification(entry)
