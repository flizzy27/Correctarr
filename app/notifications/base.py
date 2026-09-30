"""The shape every notification channel has.

A channel is a place findings get sent to: Pushover, Telegram, Discord, a plain
webhook. Each one declares the fields it needs, so the interface can build its
own form and the server can validate what comes back without knowing anything
about the provider.

The same structure is what makes routing possible. A report is assembled once,
as data, and every channel renders it in whatever shape its provider wants —
HTML for Pushover and Telegram, an embed for Discord, plain text for ntfy.
Formatting in the channel rather than in the caller is the only way to get that
right; a single pre-rendered string always ends up wrong somewhere.

Sending is shared as well. A provider that says "too many requests" and how
long to wait is waited for once, if the wait is short; a connection that could
not be opened is tried once more, because nothing was sent. Nothing else is
repeated: a message that may have arrived is not sent a second time.
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

import httpx

from .. import connection
from ..i18n import t
from ..logging_setup import redact

log = logging.getLogger(__name__)

#: Per request. A provider that has not answered by then is not going to.
TIMEOUT = 20.0

#: The longest "retry after" that is waited out. Discord and Telegram usually
#: ask for a second or two; anything longer means the limit is not a burst,
#: and the run should not stand still for it.
MOST_WAIT = 10.0

# Order matters: a threshold of "warning" means warning and error.
SEVERITIES = ("info", "warning", "error")
SEVERITY_RANK = {name: index for index, name in enumerate(SEVERITIES)}

# Not every provider handles every character. Keeping the marks here means a
# channel can pick the set that survives its own escaping.
SYMBOLS = {"error": "⚠️", "warning": "❗", "info": "ℹ️"}

#: Groups that are not a rule. The safety fuse reports through the same
#: channels as everything else, under a heading of its own.
HEADINGS = {"safety": "safety.heading"}


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
    #: For an address: the schemes it may start with. Checked when the
    #: connection is saved, not only when the first message fails.
    schemes: tuple[str, ...] = ()

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
        if self.schemes and text and not text.lower().startswith(self.schemes):
            raise ValueError("error.url_scheme_https" if self.schemes == ("https://",)
                             else "error.url_scheme")
        return text


@dataclass
class Group:
    """The findings of one rule, kept together so a report reads as a list of
    problems rather than a wall of lines."""
    rule: str
    severity: str
    findings: list = field(default_factory=list)

    def title(self, language: str) -> str:
        if self.rule in HEADINGS:
            return t(HEADINGS[self.rule], language)
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
    #: A headline of its own, for a message that is not the report of a run.
    headline_key: str = ""

    @property
    def severity(self) -> str:
        if not self.groups:
            return "info"
        return max((g.severity for g in self.groups),
                   key=lambda s: SEVERITY_RANK.get(s, 1))

    def headline(self) -> str:
        if self.is_test:
            return t("notify.test_title", self.language)
        if self.headline_key:
            return t(self.headline_key, self.language)
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
    #: The fields that decide where the secrets are sent. A stored secret is
    #: only put back behind the placeholder while these stay the same —
    #: otherwise changing the server of a saved connection and pressing "test"
    #: would hand its token to whatever server had been typed in.
    DESTINATION: ClassVar[tuple[str, ...]] = ()
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

    def _fit(self, lines: list[str], limit: int | None = None,
             language: str = "en",
             measure: Callable[[str], int] = len) -> str:
        """Join whole lines up to the limit, then say that some were left out.

        For a body with markup :meth:`_trim` is wrong: it cuts wherever the
        limit falls, and a cut through ``<b>`` or ``&amp;`` is a message
        Telegram refuses outright. Every line here carries its own markup, so
        stopping between two lines always leaves something well formed.
        """
        cap = limit or self.LIMIT
        text = "\n".join(lines)
        if measure(text) <= cap:
            return text
        suffix = t("notify.truncated", language)
        kept: list[str] = []
        for line in lines:
            if measure("\n".join([*kept, line, suffix])) > cap:
                break
            kept.append(line)
        return "\n".join([*kept, suffix])

    def _test_body(self, language: str) -> str:
        return t("notify.test_body", language)

    def _request(self, url: str, provider: str, *, method: str = "POST",
                 **kwargs) -> httpx.Response:
        """Send once, and a second time only where that cannot double up."""
        for attempt in (1, 2):
            try:
                with httpx.Client(timeout=TIMEOUT, follow_redirects=False) as client:
                    response = client.request(method, url, **kwargs)
            except httpx.ConnectError as e:
                if attempt == 1:
                    continue
                raise ChannelError(connection.unreachable(provider, e)) from e
            except httpx.TimeoutException as e:
                raise ChannelError(f"{provider} did not answer within "
                                   f"{TIMEOUT:.0f}s") from e
            except httpx.RequestError as e:
                raise ChannelError(connection.unreachable(provider, e)) from e
            except httpx.InvalidURL as e:
                raise ChannelError(f"The address for {provider} is not valid: "
                                   f"{connection.describe(e)}") from e
            if response.status_code == 429 and attempt == 1:
                wait = retry_after(response)
                if wait is not None and wait <= MOST_WAIT:
                    log.info("%s asks to wait %.1fs, waiting", provider, wait)
                    connection.sleep(wait)
                    continue
            return response
        raise ChannelError(f"{provider} could not be reached")    # not reached

    def _refused(self, response: httpx.Response, provider: str) -> ChannelError:
        """The error for an answer that was not a success, in plain words."""
        if response.status_code == 429:
            wait = retry_after(response)
            later = f" — try again in {wait:.0f}s" if wait else ""
            return ChannelError(f"{provider} is limiting how often it may be "
                                f"sent to{later}")
        detail = explain(response)
        return ChannelError(f"{provider} returned {response.status_code}"
                            + (f": {detail}" if detail else ""))


def retry_after(response: httpx.Response) -> float | None:
    """How long a provider asked to wait, from the header or the body.

    Discord sends it in both, Telegram only in the body, under ``parameters``.
    """
    candidates: list[Any] = [response.headers.get("retry-after")]
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        candidates.append(body.get("retry_after"))
        candidates.append((body.get("parameters") or {}).get("retry_after"))
    for value in candidates:
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        if seconds >= 0:
            return seconds
    return None


def explain(response: httpx.Response) -> str:
    """What the provider said went wrong, in its own words where it has any.

    Every one of them has a field for it and they all call it something
    different. Shown to whoever pressed "test", so it is worth finding.
    """
    try:
        body = response.json()
    except ValueError:
        return connection.short_text(response, 150)
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            return redact("; ".join(str(e) for e in errors))[:150]
        for key in ("description", "message", "error"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return redact(value)[:150]
    return connection.short_text(response, 150)
