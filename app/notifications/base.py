"""The shape every notification channel has.

A channel is a place findings get sent to: Pushover, Telegram, Discord, a plain
webhook. Each one declares the fields it needs, so the interface can build its
own form and the server can validate what comes back without knowing anything
about the provider.

The same structure is what makes routing possible. A report is assembled once,
as data, and every channel renders it in whatever shape its provider wants —
HTML for Pushover, an embed for Discord, MarkdownV2 for Telegram, plain text
for ntfy. Formatting in the channel rather than in the caller is the only way
to get that right; a single pre-rendered string always ends up wrong somewhere.
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, ClassVar

from ..i18n import t

log = logging.getLogger(__name__)

# Order matters: a threshold of "warning" means warning and error.
SEVERITIES = ("info", "warning", "error")
SEVERITY_RANK = {name: index for index, name in enumerate(SEVERITIES)}

# Not every provider handles every character. Keeping the marks here means a
# channel can pick the set that survives its own escaping.
SYMBOLS = {"error": "⚠️", "warning": "❗", "info": "ℹ️"}


class ChannelError(Exception):
    """The provider refused the message or could not be reached."""


@dataclass(frozen=True)
class ConfigField:
    """One input on the channel's form.

    ``key`` doubles as the translation key:
    ``channels.<channel>.<key>.label`` and ``.help``.
    """
    key: str
    kind: str = "text"          # text | secret | number | switch | choice
    required: bool = True
    default: Any = ""
    choices: tuple[str, ...] = ()
    placeholder: str = ""

    def validate(self, value: Any) -> Any:
        if self.kind == "switch":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if self.kind == "number":
            try:
                return int(value)
            except (TypeError, ValueError):
                raise ValueError(f"error.not_a_number|{self.key}")
        text = str(value or "").strip()
        if self.kind == "choice" and text and text not in self.choices:
            raise ValueError(f"error.not_a_choice|{self.key}|{', '.join(self.choices)}")
        if len(text) > 2000:
            raise ValueError(f"error.too_long|{self.key}")
        return text


@dataclass
class Group:
    """The findings of one rule, kept together so a report reads as a list of
    problems rather than a wall of lines."""
    rule: str
    severity: str
    findings: list = field(default_factory=list)

    def title(self, language: str) -> str:
        return t(f"rules.{self.rule}.title", language)


@dataclass
class Report:
    """What one run produced, ready to be rendered by any channel."""
    groups: list[Group]
    total: int
    fixed: int
    language: str = "en"
    url: str = ""
    dry_run: bool = False
    #: Set for the button in the interface, so a test does not pretend a run
    #: happened.
    is_test: bool = False

    @property
    def severity(self) -> str:
        if not self.groups:
            return "info"
        return max((g.severity for g in self.groups),
                   key=lambda s: SEVERITY_RANK.get(s, 1))

    def headline(self) -> str:
        if self.is_test:
            return t("notify.test_title", self.language)
        line = t("notify.title", self.language, count=self.total)
        if self.fixed:
            line += t("notify.title_fixed", self.language, count=self.fixed)
        if self.dry_run:
            line += t("notify.title_dry_run", self.language)
        return line

    def lines(self, per_group: int = 4, symbols: bool = True) -> list[str]:
        """The body as plain lines, worst rule first.

        Every channel needs roughly this and then decorates it. Producing the
        structure once keeps the channels short and keeps them consistent with
        each other.
        """
        out: list[str] = []
        remainder = 0
        for group in self.groups:
            symbol = f"{SYMBOLS.get(group.severity, '')} " if symbols else ""
            out.append(f"{symbol}{group.title(self.language)} ({len(group.findings)})")
            for finding in group.findings[:per_group]:
                line = f"• {finding.title[:64]}"
                action = str(getattr(finding, "action", "") or "")
                if action and not action.startswith(("DRY RUN", "FAILED")):
                    line += f" → {action[:48]}"
                out.append(line)
            if len(group.findings) > per_group:
                remainder += len(group.findings) - per_group
            out.append("")
        if remainder:
            out.append(t("notify.and_more", self.language, count=remainder))
        return [line for line in out if line is not None]


def group_findings(findings: list) -> list[Group]:
    """Group by rule, worst severity first."""
    grouped: OrderedDict[str, Group] = OrderedDict()
    ordered = sorted(findings, key=lambda f: -SEVERITY_RANK.get(f.severity, 1))
    for finding in ordered:
        group = grouped.get(finding.rule)
        if group is None:
            group = Group(rule=finding.rule, severity=finding.severity)
            grouped[finding.rule] = group
        group.findings.append(finding)
        # A group takes the worst severity any of its findings has.
        if SEVERITY_RANK.get(finding.severity, 1) > SEVERITY_RANK.get(group.severity, 1):
            group.severity = finding.severity
    return list(grouped.values())


class Channel:
    """Base class for every provider.

    Subclasses set ``kind`` and ``FIELDS`` and implement ``deliver``. Everything
    else — validation, masking, the test message — is handled here so a new
    provider stays small.
    """

    kind: ClassVar[str] = ""
    FIELDS: ClassVar[tuple[ConfigField, ...]] = ()
    #: Some providers cap the message length hard and simply reject anything
    #: longer, so trimming has to happen before sending rather than after.
    LIMIT: ClassVar[int] = 4000
    #: Whether the provider renders any markup at all.
    SUPPORTS_MARKUP: ClassVar[bool] = False

    def __init__(self, config: dict[str, Any], name: str = ""):
        self.config = config
        self.name = name or self.kind

    # -- configuration ---------------------------------------------------------
    @classmethod
    def describe(cls) -> dict:
        return {
            "kind": cls.kind,
            "fields": [{
                "key": f.key, "kind": f.kind, "required": f.required,
                "default": f.default, "choices": list(f.choices),
                "placeholder": f.placeholder,
            } for f in cls.FIELDS],
        }

    @classmethod
    def validate(cls, config: dict[str, Any]) -> dict[str, Any]:
        """Check and coerce a configuration. Raises ValueError with a key."""
        clean: dict[str, Any] = {}
        for field_ in cls.FIELDS:
            raw = config.get(field_.key, field_.default)
            value = field_.validate(raw)
            if field_.required and value in ("", None):
                raise ValueError(f"error.channel_field_missing|{field_.key}")
            clean[field_.key] = value
        return clean

    @classmethod
    def secret_keys(cls) -> set[str]:
        return {f.key for f in cls.FIELDS if f.kind == "secret"}

    def value(self, key: str, default: Any = "") -> Any:
        return self.config.get(key, default)

    # -- sending ---------------------------------------------------------------
    def deliver(self, report: Report) -> tuple[bool, str]:
        raise NotImplementedError

    def send(self, report: Report) -> tuple[bool, str]:
        try:
            return self.deliver(report)
        except ChannelError as e:
            return False, str(e)
        except Exception as e:                                  # noqa: BLE001
            # A broken channel must never take a run down with it.
            log.exception("Channel %s failed", self.name)
            return False, str(e)

    def send_test(self, language: str = "en", url: str = "") -> tuple[bool, str]:
        return self.send(Report(groups=[], total=0, fixed=0, language=language,
                                url=url, is_test=True))

    # -- helpers for subclasses ------------------------------------------------
    def _trim(self, text: str, limit: int | None = None,
              suffix_key: str = "notify.truncated", language: str = "en") -> str:
        """Cut to length without ending mid-word."""
        cap = limit or self.LIMIT
        if len(text) <= cap:
            return text
        suffix = "\n" + t(suffix_key, language)
        keep = max(0, cap - len(suffix))
        cut = text[:keep]
        # Prefer a line break, then a space, then wherever we landed.
        for separator in ("\n", " "):
            index = cut.rfind(separator)
            if index > keep * 0.6:
                cut = cut[:index]
                break
        return cut + suffix

    def _test_body(self, language: str) -> str:
        return t("notify.test_body", language)
