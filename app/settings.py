"""The settings schema — one single source of truth.

Why a schema instead of a plain dictionary of defaults:

  * The web UI builds itself from it. A new setting needs exactly one entry
    here, no HTML and no JavaScript.
  * Values are validated **on the server**. A tampered request cannot set a one
    second schedule and hammer the Arr services with it.
  * Secrets are marked as such and only ever leave the server masked. An
    earlier build handed the Pushover keys out in clear text from the status
    endpoint — on an interface that had no login at all.
  * Labels and help texts are translation keys, not literal strings, so the
    interface exists in every shipped language without touching this file.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Groups — they also define the order in the interface
# ---------------------------------------------------------------------------
GROUPS = ("schedule", "paths", "detection", "cleanup", "indexers",
          "notifications", "appearance", "maintenance")

# Every theme is dark. A light one is deliberately missing: this runs next to
# Radarr, Sonarr and Prowlarr, and those are dark. Switching tabs into a white
# page is unpleasant.
THEMES: dict[str, dict[str, str]] = {
    "midnight": {"accent": "#4f8cff", "base": "#0f1218"},
    "deepsea":  {"accent": "#2dd4bf", "base": "#0a1315"},
    "slate":    {"accent": "#a8b6cc", "base": "#101318"},
    "amber":    {"accent": "#f0a01e", "base": "#13110c"},
    "violet":   {"accent": "#a78bfa", "base": "#110f19"},
    "moss":     {"accent": "#52c97a", "base": "#0d130f"},
    "charcoal": {"accent": "#e8eaee", "base": "#0a0a0b"},
}

MASK = "••••••••"

# Paths that must never end up in the cleanup list, whatever anyone types.
FORBIDDEN_PATHS = frozenset({
    "", "/", "/mnt", "/mnt/user", "/mnt/cache", "/config", "/app",
    "/etc", "/usr", "/var", "/home", "/root", "/boot",
})


@dataclass(frozen=True)
class Field:
    """One setting.

    ``key`` doubles as the translation key: ``settings.<key>.label`` and
    ``settings.<key>.help``.
    """
    key: str
    kind: str                   # number | text | path | secret | switch | choice | list
    group: str
    default: Any
    minimum: float | None = None
    maximum: float | None = None
    step: float = 1
    unit: str = ""              # translation key suffix, e.g. "unit.seconds"
    choices: tuple[str, ...] = ()
    advanced: bool = False
    reschedules: bool = False   # changing it rebuilds the schedule

    def validate(self, value: Any) -> Any:
        """Coerce and check. Raises ValueError carrying a translation key."""
        if self.kind == "switch":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)

        if self.kind == "number":
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"error.not_a_number|{self.key}")
            if self.minimum is not None and number < self.minimum:
                raise ValueError(f"error.below_minimum|{self.key}|{_pretty(self.minimum)}")
            if self.maximum is not None and number > self.maximum:
                raise ValueError(f"error.above_maximum|{self.key}|{_pretty(self.maximum)}")
            whole = float(self.step).is_integer() and self.step >= 1
            return int(number) if whole else number

        if self.kind == "choice":
            text = str(value)
            if text not in self.choices:
                raise ValueError(f"error.not_a_choice|{self.key}|{', '.join(self.choices)}")
            return text

        if self.kind == "list":
            if isinstance(value, str):
                items = [line.strip() for line in value.replace("\r", "").split("\n")]
            elif isinstance(value, (list, tuple)):
                items = [str(item).strip() for item in value]
            else:
                raise ValueError(f"error.not_a_list|{self.key}")
            return [item for item in items if item]

        if self.kind in ("path", "text", "secret"):
            text = str(value or "").strip()
            if self.kind == "path" and text and not text.startswith("/"):
                raise ValueError(f"error.not_absolute|{self.key}")
            if len(text) > 2000:
                raise ValueError(f"error.too_long|{self.key}")
            return text

        raise ValueError(f"error.unknown_kind|{self.kind}")


def _pretty(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


# ---------------------------------------------------------------------------
# The fields
# ---------------------------------------------------------------------------
FIELDS: tuple[Field, ...] = (
    # -- schedule -----------------------------------------------------------
    Field("fast_seconds", "number", "schedule", 60,
          minimum=20, maximum=3600, unit="unit.seconds", reschedules=True),
    Field("deep_minutes", "number", "schedule", 90,
          minimum=5, maximum=1440, unit="unit.minutes", reschedules=True),
    Field("event_debounce", "number", "schedule", 8,
          minimum=1, maximum=300, unit="unit.seconds"),
    Field("events_enabled", "switch", "schedule", True),
    Field("dry_run", "switch", "schedule", False),

    # -- paths --------------------------------------------------------------
    Field("path_downloads", "path", "paths", "/downloads"),
    Field("path_incomplete", "path", "paths", "/incomplete"),
    Field("path_movies", "path", "paths", "/movies"),
    Field("path_series", "path", "paths", "/series"),
    Field("extra_cleanup_paths", "list", "paths", [], advanced=True),

    # -- detection ----------------------------------------------------------
    Field("year_tolerance", "number", "detection", 1,
          minimum=0, maximum=5, unit="unit.years"),
    Field("title_similarity", "number", "detection", 0.45,
          minimum=0.1, maximum=0.95, step=0.05),
    Field("stalled_minutes", "number", "detection", 120,
          minimum=10, maximum=10080, unit="unit.minutes"),
    Field("loop_grabs", "number", "detection", 4,
          minimum=2, maximum=50, unit="unit.grabs"),
    Field("loop_hours", "number", "detection", 12,
          minimum=1, maximum=168, unit="unit.hours"),
    Field("disk_threshold_gb", "number", "detection", 250,
          minimum=1, maximum=10000, unit="unit.gb"),
    Field("give_up_hours", "number", "detection", 6,
          minimum=0, maximum=720, unit="unit.hours"),
    Field("unpack_timeout_hours", "number", "detection", 2,
          minimum=0.5, maximum=72, step=0.5, unit="unit.hours"),
    Field("downloader_done_hours", "number", "detection", 1,
          minimum=0.5, maximum=168, step=0.5, unit="unit.hours"),
    # Empty on purpose: there is no sensible default for which audio languages
    # matter. The rule stays quiet until somebody says.
    Field("audio_languages", "text", "detection", ""),

    # -- cleanup ------------------------------------------------------------
    Field("trash_age_hours", "number", "cleanup", 24,
          minimum=1, maximum=720, unit="unit.hours"),
    Field("trash_empty_hours", "number", "cleanup", 2,
          minimum=0.5, maximum=168, step=0.5, unit="unit.hours"),
    Field("trash_video_mb", "number", "cleanup", 300,
          minimum=10, maximum=20000, unit="unit.mb"),

    # -- indexers -----------------------------------------------------------
    Field("indexer_days", "number", "indexers", 30,
          minimum=1, maximum=365, unit="unit.days"),
    Field("indexer_min_queries", "number", "indexers", 200,
          minimum=10, maximum=100000, unit="unit.queries"),
    Field("indexer_min_yield", "number", "indexers", 0.2,
          minimum=0.01, maximum=50, step=0.01, unit="unit.percent"),
    Field("indexer_rank_tolerance", "number", "indexers", 2,
          minimum=1, maximum=20, unit="unit.places"),

    # -- notifications ------------------------------------------------------
    Field("pushover_enabled", "switch", "notifications", False),
    Field("pushover_app", "secret", "notifications", ""),
    Field("pushover_user", "secret", "notifications", ""),
    Field("pushover_devices", "text", "notifications", ""),
    Field("pushover_sound", "text", "notifications", "pianobar"),
    Field("pushover_min_severity", "choice", "notifications", "warning",
          choices=("info", "warning", "error")),
    Field("pushover_fixed_only", "switch", "notifications", False),
    Field("pushover_cooldown", "number", "notifications", 5,
          minimum=0, maximum=1440, unit="unit.minutes"),
    Field("recheck_hours", "number", "notifications", 12,
          minimum=1, maximum=720, unit="unit.hours"),

    # -- appearance ---------------------------------------------------------
    Field("language", "choice", "appearance", "auto",
          choices=("auto", "en", "de")),
    Field("theme", "choice", "appearance", "midnight",
          choices=tuple(THEMES)),
    Field("density", "choice", "appearance", "normal",
          choices=("spacious", "normal", "compact")),
    Field("public_url", "text", "appearance", ""),

    # -- maintenance --------------------------------------------------------
    Field("log_keep", "number", "maintenance", 20000,
          minimum=500, maximum=1000000, unit="unit.entries"),
    Field("log_days", "number", "maintenance", 90,
          minimum=0, maximum=3650, unit="unit.days"),
)

BY_KEY: dict[str, Field] = {f.key: f for f in FIELDS}
SECRETS = frozenset(f.key for f in FIELDS if f.kind == "secret")


def defaults() -> dict[str, Any]:
    return {f.key: f.default for f in FIELDS}


def validate(key: str, value: Any) -> Any:
    field_ = BY_KEY.get(key)
    if field_ is None:
        raise ValueError(f"error.unknown_setting|{key}")
    return field_.validate(value)


def mask(values: dict[str, Any]) -> dict[str, Any]:
    """Replace secrets with a placeholder.

    The interface needs to know **whether** a key is stored, not which one. If
    the placeholder comes back unchanged the stored value stays (see
    ``unmask``).
    """
    out = dict(values)
    for key in SECRETS:
        if out.get(key):
            out[key] = MASK
    return out


def unmask(key: str, value: Any, stored: Any) -> Any:
    """If the placeholder came back untouched, keep what is stored."""
    if key in SECRETS and isinstance(value, str) and value == MASK:
        return stored
    return value


def describe() -> dict:
    """Everything the interface needs to render the settings itself."""
    return {
        "groups": list(GROUPS),
        "themes": THEMES,
        "fields": [{
            "key": f.key, "kind": f.kind, "group": f.group, "default": f.default,
            "minimum": f.minimum, "maximum": f.maximum, "step": f.step,
            "unit": f.unit, "choices": list(f.choices),
            "advanced": f.advanced, "reschedules": f.reschedules,
        } for f in FIELDS],
    }


def cleanup_paths(config: dict) -> list[str]:
    """The directories cleanup is allowed to touch.

    Derived rather than freely editable on purpose: anyone who types ``/`` here
    would delete half the system. Finished and incomplete downloads are always
    included, anything further comes from a separate list — and a handful of
    system directories can never get in, however they are spelled.
    """
    candidates = [config.get("path_downloads"), config.get("path_incomplete")]
    candidates += list(config.get("extra_cleanup_paths") or [])
    clean: list[str] = []
    for raw in candidates:
        path = str(raw or "").strip().rstrip("/")
        if not path.startswith("/") or path in FORBIDDEN_PATHS:
            continue
        if path not in clean:
            clean.append(path)
    return clean
