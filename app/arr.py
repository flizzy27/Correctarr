"""Radarr and Sonarr.

Both speak the same v3 API but differ in their nouns (movie/series,
movieId/seriesId). This class hides that so the rules do not have to care.

The HTTP connection is built once per instance and kept open. Previously every
single request created a fresh client with its own TCP and TLS handshake — with
more than twenty requests per run on a 60 second schedule, that adds up.

An instance lives for one pass, and a pass is a snapshot of one moment. What it
reads is therefore remembered until something is changed: the per service rules
and the rules that run once per pass asked for the same queue, profiles, formats
and library, and a deep pass paid for eight requests twice (measured against
live services: 51 requests before, 43 after).
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from . import compat, connection

log = logging.getLogger(__name__)

# The only kinds the constructor accepts. An unknown value would be a bug in
# the store and should surface early, not on the first request.
KINDS = ("radarr", "sonarr")


class ArrError(Exception):
    """The service is unreachable or refused the request."""


class GoneError(ArrError):
    """The thing asked about no longer exists.

    Kept apart from every other refusal because it is not one. A queue entry
    that vanished between the check and the action has almost always done so
    for a good reason — it finished and was imported, or somebody removed it —
    and reporting that as a failure made the record say something went wrong
    when nothing had.
    """


class Arr:
    def __init__(self, kind: str, url: str, api_key: str,
                 timeout: float = 30.0, name: str = "",
                 service_id: int | None = None, verify: bool = True):
        if kind not in KINDS:
            raise ValueError(f"Unknown kind: {kind}")
        self.kind = kind
        self.url = connection.normalise(url)
        self.api_key = api_key
        self.timeout = timeout
        #: False accepts a self-signed certificate. Chosen per service, never
        #: assumed: an https address that cannot be verified is refused.
        self.verify = verify
        self.name = name or kind.capitalize()
        #: The row this was built from. Two Radarr instances are the same
        #: *kind* and have nothing else in common: a queue id from one names a
        #: different entry, or none at all, in the other. Everything that has
        #: to come back to the instance it started at goes through this.
        self.service_id = service_id
        self._client: httpx.Client | None = None
        #: Filled in from response headers as requests happen — the version the
        #: service reports, and any part of the API it has marked as replaced.
        self.observed = compat.Observed()
        #: History answered once per instance, see :meth:`history`.
        self._history: dict[int, list[dict]] = {}
        #: Everything else read in this pass, until something is changed.
        self._read: dict[str, Any] = {}

    def __repr__(self) -> str:                      # never includes the key
        return f"<Arr {self.kind} {self.url}>"

    # -- plumbing --------------------------------------------------------------
    @property
    def client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            self._client = connection.open_client(
                f"{self.url}/api/v3/", timeout=self.timeout, verify=self.verify,
                headers={"X-Api-Key": self.api_key, "Accept": "application/json"},
                connections=8)
        return self._client

    def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            self._client.close()

    def _call(self, method: str, path: str, **kwargs) -> Any:
        path = path.lstrip("/")
        if method != "GET":
            # Whatever was read before a change may be what it changed.
            self._read.clear()
        started = time.monotonic()
        try:
            client = self.client
        except (ValueError, httpx.InvalidURL) as e:
            raise ArrError(f"The address of {self.name} is not valid: "
                           f"{connection.describe(e)}") from e
        # A service that is still booting answers 503 and says so. That is
        # not an outage, and a restart of the host otherwise produces a
        # frightening error on every single rule at once — so it is asked
        # again, for a change as well: 503 means the request was not taken.
        response = connection.request(client, method, path, name=self.name,
                                      fail=ArrError, timeout=self.timeout,
                                      **kwargs)
        log.debug("%s %s %s -> %s in %.0fms", self.name, method, path,
                  response.status_code, (time.monotonic() - started) * 1000)
        self.observed.note(path, response.headers)

        if response.status_code == compat.STARTING_UP:
            raise ArrError(f"{self.name} is still starting up")
        if response.status_code == 401:
            raise ArrError(f"{self.name} rejected the API key")
        if response.status_code == 404:
            raise GoneError(f"{self.name} does not know {path} — "
                            f"is the address and version right?")
        if response.status_code >= 400:
            raise ArrError(f"{self.name} {method} {path} returned "
                           f"{response.status_code}: {connection.short_text(response)}")
        return connection.decode(response, name=self.name,
                                 what=self.kind.capitalize(), fail=ArrError)

    def _once(self, key: str, read) -> Any:
        """Read something at most once until the next change. See the module."""
        if key not in self._read:
            self._read[key] = read()
        return self._read[key]

    def _pages(self, path: str, params: dict, page_size: int,
               most: int) -> list[dict]:
        """Every record of a paged list, up to ``most``.

        One page used to be all anybody asked for. A queue of more than a
        thousand entries lost the rest without a word, and the blocklist —
        newest first — never showed the old entries the stale blocklist rule
        is looking for once there were more than five hundred.

        ``most`` is there so a library with tens of thousands of wanted
        episodes is not walked in full every ninety minutes.
        """
        rows: list[dict] = []
        page = 1
        while True:
            data = self._call("GET", path, params={
                **params, "page": page, "pageSize": page_size})
            if not isinstance(data, dict):
                return rows
            batch = data.get("records") or []
            rows += batch
            try:
                total = int(data.get("totalRecords") or 0)
            except (TypeError, ValueError):
                total = 0
            if not batch or len(batch) < page_size or len(rows) >= total:
                return rows
            if len(rows) >= most:
                log.info("%s has %d records under %s, read the first %d",
                         self.name, total, path, len(rows))
                return rows[:most]
            page += 1

    def reachable(self) -> tuple[bool, str]:
        try:
            status = self._call("GET", "system/status")
        except ArrError as e:
            return False, str(e)
        if not isinstance(status, dict):
            return False, (f"{self.name} did not answer like "
                           f"{self.kind.capitalize()} does")
        if status.get("version"):
            self.observed.version = str(status["version"])[:32]
        name = status.get("appName") or self.kind.capitalize()
        # Someone who enters Radarr but means Sonarr should find out now.
        if name.lower() in ("radarr", "sonarr") and name.lower() != self.kind:
            return False, (f"{name} is running there, but this entry says "
                           f"{self.kind.capitalize()}")
        return True, f"{name} {status.get('version', '?')}"

    # -- reading ---------------------------------------------------------------
    def queue(self) -> list[dict]:
        # includeEpisode is what lets a queue entry be held against the date
        # its content actually aired. Without it a Sonarr entry carries the
        # series and nothing about which episode is in the box.
        params = {"includeMovie": "true", "includeSeries": "true",
                  "includeEpisode": "true",
                  "includeUnknownMovieItems": "true", "includeUnknownSeriesItems": "true"}
        return self._once("queue", lambda: self._pages(
            "queue", params, page_size=1000, most=5000))

    def health(self) -> list[dict]:
        return self._call("GET", "health") or []

    def profiles(self) -> list[dict]:
        return self._once("profiles",
                          lambda: self._call("GET", "qualityprofile") or [])

    def custom_formats(self) -> list[dict]:
        """The custom formats, or none on a service that has no such thing.

        Sonarr 3 has no custom formats at all and answers 404. That used to
        take the whole per service state down with it, so not one rule ran
        against a Sonarr 3 — for want of a list that is simply empty there.
        """
        def read() -> list[dict]:
            try:
                return self._call("GET", "customformat") or []
            except GoneError:
                return []
        return self._once("formats", read)

    def items(self) -> list[dict]:
        """All movies or all series, depending on the kind."""
        return self._once("items", lambda: self._call(
            "GET", "movie" if self.kind == "radarr" else "series") or [])

    def import_candidates(self, download_id: str | None = None,
                          folder: str | None = None) -> list[dict]:
        params: dict[str, Any] = {"filterExistingFiles": "false"}
        if download_id:
            params["downloadId"] = download_id
        if folder:
            params["folder"] = folder
        return self._call("GET", "manualimport", params=params) or []

    def history(self, page_size: int = 200) -> list[dict]:
        """The most recent history entries, fetched at most once per instance.

        An instance lives for exactly one run, so answering the same question
        twice inside it can only produce the same answer at the cost of another
        request. It was being asked a lot: the shared state asks once, the
        indexer rating asks again, and blocklisting a release that has left the
        queue asked once more **per finding** — ten orphaned files meant ten
        requests for the same five hundred rows.
        """
        if page_size in self._history:
            return self._history[page_size]
        records = (self._call("GET", "history", params={
            "pageSize": page_size, "sortKey": "date",
            "sortDirection": "descending"}) or {}).get("records", [])
        self._history[page_size] = records
        return records

    def forget_history(self) -> None:
        """Ask again next time.

        The memo above is right for a run, which is a snapshot of one moment.
        It is wrong the instant somebody is waiting to see whether a search
        they just started has found anything — there the whole question is
        what has changed since.
        """
        self._history.clear()

    #: Sonarr answers both "what is missing" and "what is below the cutoff"
    #: with bare episodes: a season number, an episode number, and ids. The
    #: series it belongs to and the file that is already there are sent **only
    #: when asked for**, and without them there is nothing to say beyond
    #: "S02E02" and a question mark where the quality should be. Radarr sends
    #: whole movies and ignores both parameters.
    _WANTED_EXTRAS = {"includeSeries": "true", "includeEpisodeFile": "true"}

    #: How much of the wanted lists and the blocklist a pass reads at most.
    WANTED_MOST = 2000
    BLOCKLIST_MOST = 5000

    def missing(self) -> list[dict]:
        """Monitored titles without a file."""
        try:
            return self._pages("wanted/missing", {
                "monitored": "true",
                "sortKey": ("movies.sortTitle" if self.kind == "radarr"
                            else "series.sortTitle"),
                **self._WANTED_EXTRAS,
            }, page_size=250, most=self.WANTED_MOST)
        except GoneError:
            if self.kind != "radarr":
                raise
            return [m for m in self._library_without_wanted()
                    if m.get("monitored") and not m.get("hasFile")
                    ][: self.WANTED_MOST]

    def below_cutoff(self) -> list[dict]:
        try:
            return self._pages("wanted/cutoff", {
                "monitored": "true", **self._WANTED_EXTRAS,
            }, page_size=250, most=self.WANTED_MOST)
        except GoneError:
            if self.kind != "radarr":
                raise
            return [m for m in self._library_without_wanted()
                    if m.get("monitored")
                    and (m.get("movieFile") or {}).get("qualityCutoffNotMet")
                    ][: self.WANTED_MOST]

    def _library_without_wanted(self) -> list[dict]:
        """Radarr 4 has no wanted lists; both are in its movie list.

        The wanted endpoints arrived with Radarr 5 — checked against the API
        description Radarr publishes for 4.7. On 4 the two rules that read them
        found nothing on every deep pass and logged a warning each time. The
        movie list says the same thing, and a deep pass has usually read it
        already.
        """
        return self.items()

    def blocklist(self) -> list[dict]:
        return self._pages("blocklist", {
            "sortKey": "date", "sortDirection": "descending",
        }, page_size=500, most=self.BLOCKLIST_MOST)

    def disk_space(self) -> list[dict]:
        return self._call("GET", "diskspace") or []

    def root_folders(self) -> list[dict]:
        return self._call("GET", "rootfolder") or []

    def files(self, item_ids: list[int]) -> list[dict]:
        """Files for the given items.

        Sonarr takes **one** series per request. Given a comma separated list
        it answers 400, and it did so on every deep pass: the library rules
        that were extended to series in 1.1 never saw a single episode file.
        Measured against a live Sonarr — one id, 200; three ids, 400.

        Radarr carries each movie's file inline in the movie list, which is
        where the library rules read it from, so this is only ever asked on
        Sonarr's behalf. It still works on Radarr, one movie at a time.
        """
        out = []
        key = "movieId" if self.kind == "radarr" else "seriesId"
        path = "moviefile" if self.kind == "radarr" else "episodefile"
        failures = 0
        for item_id in item_ids:
            try:
                out += self._call("GET", path, params={key: item_id}) or []
            except ArrError as e:
                failures += 1
                # One broken series should not hide the rest, and one log
                # line per series would bury everything else in the log.
                if failures == 1:
                    log.warning("File lookup failed: %s", e)
        if failures > 1:
            log.warning("File lookup failed for %d of %d items",
                        failures, len(item_ids))
        return out

    # -- quality profiles and custom formats -----------------------------------
    def quality_profile_schema(self) -> dict:
        """An empty profile, with this service's own ladder already in it.

        Asked for rather than assembled. The numbers behind the quality names
        are not the same in Radarr and Sonarr and have moved between versions,
        so a ladder written down here would be right for one install and
        quietly wrong for the next — and a profile whose cutoff points at a
        quality that does not exist is one the service can never satisfy.
        """
        return self._call("GET", "qualityprofile/schema") or {}

    def custom_format_schema(self) -> list[dict]:
        """A template for every kind of condition this service understands."""
        return self._call("GET", "customformat/schema") or []

    def languages(self) -> list[dict]:
        return self._call("GET", "language") or []

    def quality_definitions(self) -> list[dict]:
        """Minimum, preferred and maximum size per quality, in MB a minute."""
        return self._call("GET", "qualitydefinition") or []

    def history_since(self, since: str) -> list[dict]:
        """Every history entry after an ISO date, unpaged.

        Not filtered by event type on the way in: the numbers behind the types
        are not the same in both applications, so the caller picks by name.
        """
        return self._call("GET", "history/since", params={"date": since}) or []

    def set_quality_profile(self, item_ids: list[int], profile_id: int) -> None:
        """Put these titles on another profile, and do nothing else.

        The editor moves no files and starts no search. What the new profile
        wants is fetched the way anything is — when an indexer next offers it.
        """
        if not item_ids:
            return
        if self.kind == "radarr":
            self._call("PUT", "movie/editor", json={
                "movieIds": list(item_ids), "qualityProfileId": profile_id,
                "moveFiles": False})
        else:
            self._call("PUT", "series/editor", json={
                "seriesIds": list(item_ids), "qualityProfileId": profile_id,
                "moveFiles": False})

    def save_custom_format(self, body: dict,
                           existing_id: int | None = None) -> dict:
        if existing_id:
            return self._call("PUT", f"customformat/{existing_id}",
                              json={**body, "id": existing_id}) or {}
        return self._call("POST", "customformat", json=body) or {}

    def quality_profile(self, profile_id: int) -> dict:
        return self._call("GET", f"qualityprofile/{profile_id}") or {}

    def save_quality_profile(self, body: dict,
                             existing_id: int | None = None) -> dict:
        if existing_id:
            return self._call("PUT", f"qualityprofile/{existing_id}",
                              json={**body, "id": existing_id}) or {}
        return self._call("POST", "qualityprofile", json=body) or {}

    # -- acting ----------------------------------------------------------------
    def remove_from_queue(self, entry_id: int, blocklist: bool = True,
                          search_again: bool = True) -> None:
        """Remove an entry from the queue.

        ``blocklist`` puts the release on the blocklist so it is not grabbed
        again. ``search_again`` triggers an immediate search for an alternative
        (that is ``skipRedownload=false``).
        """
        self._call("DELETE", f"queue/{entry_id}", params={
            "removeFromClient": "true",
            "blocklist": "true" if blocklist else "false",
            "skipRedownload": "false" if search_again else "true",
        })

    def manual_import(self, files: list[dict]) -> int | None:
        return (self._call("POST", "command", json={
            "name": "ManualImport", "files": files, "importMode": "auto"}) or {}).get("id")

    def search(self, item_ids: list[int]) -> int | None:
        """Search again for these titles. Returns the last command id.

        The two applications do not agree on singular or plural here, and an
        unrecognised command name is a server error rather than a refusal, so
        the names live in one table instead of being spelled out twice.

        Sonarr's series search takes **one** series, not a list. That is why
        this loops rather than passing the first id and dropping the rest — a
        rule that acts on four series would otherwise search for one of them
        and report success for all four.
        """
        if not item_ids:
            return None
        if self.kind == "radarr":
            return (self._call("POST", "command", json={
                "name": compat.command_for("radarr", "search"),
                "movieIds": list(item_ids)}) or {}).get("id")
        name = compat.command_for("sonarr", "series_search")
        last = None
        for series_id in item_ids:
            last = (self._call("POST", "command", json={
                "name": name, "seriesId": series_id}) or {}).get("id")
        return last

    def search_episodes(self, episode_ids: list[int]) -> int | None:
        """Search for individual episodes rather than a whole series.

        Sonarr only. Searching the series again for one missing episode makes
        it query every indexer for every episode it already has, which is both
        slow and a good way to run into a daily query limit.
        """
        if self.kind != "sonarr" or not episode_ids:
            return None
        return (self._call("POST", "command", json={
            "name": compat.command_for("sonarr", "search"),
            "episodeIds": list(episode_ids)}) or {}).get("id")

    def episodes(self, series_id: int) -> list[dict]:
        """Every episode of one series, with its file id when it has one."""
        return self._call("GET", "episode", params={"seriesId": series_id}) or []

    def command(self, name: str, **kwargs) -> int | None:
        return (self._call("POST", "command", json={"name": name, **kwargs}) or {}).get("id")

    def command_status(self, command_id: int) -> dict:
        return self._call("GET", f"command/{command_id}") or {}

    def mark_grab_failed(self, history_id: int) -> None:
        """Mark a grab in the history as failed.

        That puts the release on the blocklist even when it has long since left
        the queue.
        """
        self._call("POST", f"history/failed/{history_id}")

    def remove_from_blocklist(self, entry_id: int) -> None:
        self._call("DELETE", f"blocklist/{entry_id}")

    # -- webhook ---------------------------------------------------------------
    WEBHOOK_NAME = "Correctarr"
    # Names used by earlier builds. Found and renamed instead of leaving a
    # duplicate behind.
    LEGACY_NAMES = ("Radarr-Fixer",)

    def webhook(self) -> dict | None:
        names = (self.WEBHOOK_NAME, *self.LEGACY_NAMES)
        for entry in (self._call("GET", "notification") or []):
            if entry.get("name") in names:
                return entry
        return None

    def set_webhook(self, target_url: str) -> str:
        """Create a webhook connection pointing back at us.

        That is what turns a delay of up to a minute into a reaction within
        seconds.
        """
        if not target_url.startswith(("http://", "https://")):
            raise ArrError("The public address must start with http:// or https://")
        template = None
        for schema in (self._call("GET", "notification/schema") or []):
            if schema.get("implementation") == "Webhook":
                template = schema
                break
        if not template:
            raise ArrError(f"{self.name} has no webhook connection type")

        fields = []
        for f in template.get("fields", []):
            value = f.get("value")
            if f.get("name") == "url":
                value = target_url
            elif f.get("name") == "method":
                value = 1                      # POST
            fields.append({"name": f["name"], "value": value})

        body = {
            "name": self.WEBHOOK_NAME,
            "implementation": "Webhook",
            "implementationName": template.get("implementationName", "Webhook"),
            "configContract": template.get("configContract", "WebhookSettings"),
            "fields": fields,
            "tags": [],
            "onGrab": True,
            "onDownload": True,
            "onUpgrade": True,
            "onRename": False,
            "onHealthIssue": True,
            "onHealthRestored": False,
            "onManualInteractionRequired": True,
            "onApplicationUpdate": False,
            "includeHealthWarnings": True,
        }
        # Drop keys this version of the service does not know, otherwise it
        # rejects the whole call.
        allowed = set(template) | {"name", "implementation", "implementationName",
                                   "configContract", "fields", "tags"}
        body = {k: v for k, v in body.items() if k in allowed}

        # forceSave matters more than it looks. Saving a webhook normally makes
        # the service send a test call to the address first and refuse to save
        # if nothing answers — which is exactly the situation while this
        # container is still starting up and registering its own address.
        params = {"forceSave": "true"}
        existing = self.webhook()
        if existing:
            body["id"] = existing["id"]
            self._call("PUT", f"notification/{existing['id']}", json=body,
                       params=params)
            return ("adopted" if existing.get("name") != self.WEBHOOK_NAME
                    else "updated")
        self._call("POST", "notification", json=body, params=params)
        return "created"

    def remove_webhook(self) -> bool:
        existing = self.webhook()
        if not existing:
            return False
        self._call("DELETE", f"notification/{existing['id']}")
        return True
