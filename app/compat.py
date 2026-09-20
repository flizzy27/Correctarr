"""Staying compatible with versions of the services that do not exist yet.

The services this talks to move, and nobody publishes a compatibility policy
for any of them. What they do instead is signal — in headers, in the shape of
what they return, in fields they mark as replaced and then keep sending for a
while. Almost all of that signalling is free: it arrives on requests that were
being made anyway. Ignoring it is how an integration works fine for two years
and then breaks on a Tuesday with no warning anyone could have acted on.

So this module collects the signals and turns them into something a person can
read before anything breaks.

What is watched
---------------
**The version.** Every API response carries the application version in a
header. Recording it costs nothing and means the interface can say which
version each service is on without a single extra request.

**Replaced fields.** When a response comes from part of the API the service has
marked as replaced, it says so in a header. That is the one genuine advance
warning available, and it is worth surfacing rather than dropping.

**Field names that are moving.** A field can be renamed while the old name
keeps working for a release or two. :func:`field` reads a value under every
name it has had or is about to have, so the rename is a non-event.

**Starting up.** A service that is still booting answers with a specific status
that means "ask again", not "broken". Treating it as an outage produces a
frightening error on every reboot.

What is deliberately NOT done
-----------------------------
**No version negotiation.** The API path has been stable across several major
versions of each application and there is no second version to negotiate with.
Building a negotiator would add a failure mode to guard against one that does
not exist.

**No feature probing by trial.** Asking a service to do something it does not
know about does not produce a polite refusal — it produces a server error with
a stack trace, and a line in its log. What is available per application is
written down here instead.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

log = logging.getLogger(__name__)

#: Carries the application version on every API response.
VERSION_HEADER = "X-Application-Version"

#: Set when a response comes from a part of the API marked as replaced. The
#: only advance warning these services give.
DEPRECATION_HEADER = "Deprecation"

#: The service is still booting. Not an outage — ask again shortly.
STARTING_UP = 503

#: Field names that are in the middle of being renamed. Read under every
#: spelling, so whichever one the service is sending today works.
#:
#: The pairs here are not speculation: each is a field the services carry under
#: the first name while already announcing the second.
ALIASES: dict[str, tuple[str, ...]] = {
    "sizeleft": ("sizeleft", "sizeLeft"),
    "timeleft": ("timeleft", "timeLeft"),
}

#: Commands, per application. These differ in ways that look like typos and are
#: not: one application checks for updates with a name the other spells
#: backwards, and the singular and plural of the search commands do not match.
#: Getting one wrong is a server error, not a refusal, so they are written down
#: rather than guessed.
COMMANDS = {
    "radarr": {
        "search": "MoviesSearch",
        "refresh": "RefreshMovie",
        "rescan": "RescanMovie",
        "rename": "RenameMovie",
        "missing_search": "MissingMoviesSearch",
        "cutoff_search": "CutoffUnmetMoviesSearch",
        "check_update": "ApplicationCheckUpdate",
    },
    "sonarr": {
        "search": "EpisodeSearch",
        "series_search": "SeriesSearch",
        "refresh": "RefreshSeries",
        "rescan": "RescanSeries",
        "rename": "RenameSeries",
        "missing_search": "MissingEpisodeSearch",
        "cutoff_search": "CutoffUnmetEpisodeSearch",
        "check_update": "ApplicationUpdateCheck",
    },
}

#: History event types, by name. The numbers behind these names are NOT the
#: same in both applications — 6 means "file deleted" in one and "file renamed"
#: in the other — so a filter built on numbers silently returns the wrong rows.
#: Everything here matches on the name and accepts the number only where both
#: applications agree on it.
GRABBED = ("grabbed", 1)
IMPORTED = ("downloadFolderImported", 3)
FAILED = ("downloadFailed", 4)


def field(source: dict, name: str, default: Any = None) -> Any:
    """Read a field under every spelling it is known by.

    ``field(entry, "sizeleft")`` finds the value whether the service is still
    sending the old name or has moved to the new one. A name with no aliases is
    simply read as given, so this is safe to use everywhere.
    """
    for key in ALIASES.get(name, (name,)):
        value = source.get(key)
        if value is not None:
            return value
    return default


def number(source: dict, name: str, default: float = 0.0) -> float:
    """The same, for a value that has to be a number to be useful."""
    value = field(source, name)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def event_is(entry: dict, kind: tuple) -> bool:
    """Is this history entry of the given kind?

    Matched by name first. The numeric forms in :data:`GRABBED` and friends are
    only the ones both applications agree on.
    """
    value = entry.get("eventType")
    if isinstance(value, str):
        return value.lower() == str(kind[0]).lower()
    return value in kind[1:]


def command_for(kind: str, purpose: str) -> str | None:
    """The command name this application uses for a given purpose."""
    return COMMANDS.get(kind, {}).get(purpose)


# ---------------------------------------------------------------------------
# What has been observed about a running service
# ---------------------------------------------------------------------------
_VERSION = re.compile(r"^(\d+)\.(\d+)")


@dataclass
class Observed:
    """What the responses from one service have said about it.

    Filled in from headers as requests happen, so none of it costs a request of
    its own.
    """
    version: str = ""
    #: Paths that answered with a "this has been replaced" header, and how
    #: often. Worth showing to a person: it is the only notice that arrives
    #: before something stops working.
    deprecated: dict[str, int] = dataclass_field(default_factory=dict)

    def note(self, path: str, headers) -> None:
        version = headers.get(VERSION_HEADER)
        if version and version != self.version:
            if self.version:
                log.info("Version changed from %s to %s", self.version, version)
            self.version = str(version)[:32]
        if headers.get(DEPRECATION_HEADER):
            clean = path.split("?")[0].strip("/")[:60]
            first = clean not in self.deprecated
            self.deprecated[clean] = self.deprecated.get(clean, 0) + 1
            if first:
                log.warning("%s is marked as replaced by the service and will "
                            "stop working eventually", clean)

    @property
    def major(self) -> int:
        match = _VERSION.match(self.version)
        return int(match.group(1)) if match else 0

    @property
    def minor(self) -> int:
        match = _VERSION.match(self.version)
        return int(match.group(2)) if match else 0

    def at_least(self, major: int, minor: int = 0) -> bool:
        """Is the service at least this version? False when it is not known.

        The cautious direction on purpose: an unknown version is treated as
        older, so anything guarded by this is skipped rather than attempted
        against a service that may not support it.
        """
        if not self.version:
            return False
        return (self.major, self.minor) >= (major, minor)
