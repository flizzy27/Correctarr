"""Radarr and Sonarr.

Both speak the same v3 API but differ in their nouns (movie/series,
movieId/seriesId). This class hides that so the rules do not have to care.

The HTTP connection is built once per instance and kept open. Previously every
single request created a fresh client with its own TCP and TLS handshake — with
more than twenty requests per run on a 60 second schedule, that adds up.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from . import compat

log = logging.getLogger(__name__)

# The only kinds the constructor accepts. An unknown value would be a bug in
# the store and should surface early, not on the first request.
KINDS = ("radarr", "sonarr")


class ArrError(Exception):
    """The service is unreachable or refused the request."""


class Arr:
    def __init__(self, kind: str, url: str, api_key: str,
                 timeout: float = 30.0, name: str = "",
                 service_id: int | None = None):
        if kind not in KINDS:
            raise ValueError(f"Unknown kind: {kind}")
        self.kind = kind
        self.url = (url or "").rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
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

    def __repr__(self) -> str:                      # never includes the key
        return f"<Arr {self.kind} {self.url}>"

    # -- plumbing --------------------------------------------------------------
    @property
    def client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(
                base_url=f"{self.url}/api/v3/",
                headers={"X-Api-Key": self.api_key, "Accept": "application/json"},
                timeout=httpx.Timeout(self.timeout, connect=10.0),
                follow_redirects=True,
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4))
        return self._client

    def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            self._client.close()

    def _call(self, method: str, path: str, retries: int = 2,
              **kwargs) -> Any:
        path = path.lstrip("/")
        started = time.monotonic()
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.TimeoutException as e:
            raise ArrError(f"{self.name} did not answer within "
                           f"{self.timeout:.0f}s") from e
        except httpx.RequestError as e:
            raise ArrError(f"{self.name} is unreachable: {e}") from e
        log.debug("%s %s %s -> %s in %.0fms", self.name, method, path,
                  response.status_code, (time.monotonic() - started) * 1000)
        self.observed.note(path, response.headers)

        if response.status_code == compat.STARTING_UP and retries > 0:
            # Not an outage. The service says so itself while it boots, and a
            # restart of the host otherwise produces a frightening error on
            # every single rule at once.
            log.info("%s is still starting up, asking again", self.name)
            time.sleep(2.0)
            return self._call(method, path, retries=retries - 1, **kwargs)
        if response.status_code == compat.STARTING_UP:
            raise ArrError(f"{self.name} is still starting up")
        if response.status_code == 401:
            raise ArrError(f"{self.name} rejected the API key")
        if response.status_code == 404:
            raise ArrError(f"{self.name} does not know {path} — "
                           f"is the address and version right?")
        if response.status_code >= 400:
            raise ArrError(f"{self.name} {method} {path} returned "
                           f"{response.status_code}: {response.text[:200]}")
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as e:
            raise ArrError(f"{self.name} did not answer with JSON — does the "
                           f"address really point at {self.kind}?") from e

    def reachable(self) -> tuple[bool, str]:
        try:
            status = self._call("GET", "system/status")
        except ArrError as e:
            return False, str(e)
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
        params = {"pageSize": 1000, "includeMovie": "true", "includeSeries": "true",
                  "includeEpisode": "true",
                  "includeUnknownMovieItems": "true", "includeUnknownSeriesItems": "true"}
        return (self._call("GET", "queue", params=params) or {}).get("records", [])

    def health(self) -> list[dict]:
        return self._call("GET", "health") or []

    def profiles(self) -> list[dict]:
        return self._call("GET", "qualityprofile") or []

    def custom_formats(self) -> list[dict]:
        return self._call("GET", "customformat") or []

    def items(self) -> list[dict]:
        """All movies or all series, depending on the kind."""
        return self._call("GET", "movie" if self.kind == "radarr" else "series") or []

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

    #: Sonarr answers both "what is missing" and "what is below the cutoff"
    #: with bare episodes: a season number, an episode number, and ids. The
    #: series it belongs to and the file that is already there are sent **only
    #: when asked for**, and without them there is nothing to say beyond
    #: "S02E02" and a question mark where the quality should be. Radarr sends
    #: whole movies and ignores both parameters.
    _WANTED_EXTRAS = {"includeSeries": "true", "includeEpisodeFile": "true"}

    def missing(self, page: int = 1, page_size: int = 200) -> list[dict]:
        """Monitored titles without a file."""
        return (self._call("GET", "wanted/missing", params={
            "page": page, "pageSize": page_size, "monitored": "true",
            "sortKey": "movies.sortTitle" if self.kind == "radarr" else "series.sortTitle",
            **self._WANTED_EXTRAS,
        }) or {}).get("records", [])

    def below_cutoff(self, page: int = 1, page_size: int = 200) -> list[dict]:
        return (self._call("GET", "wanted/cutoff", params={
            "page": page, "pageSize": page_size, "monitored": "true",
            **self._WANTED_EXTRAS,
        }) or {}).get("records", [])

    def blocklist(self, page_size: int = 500) -> list[dict]:
        return (self._call("GET", "blocklist", params={
            "pageSize": page_size, "sortKey": "date",
            "sortDirection": "descending"}) or {}).get("records", [])

    def disk_space(self) -> list[dict]:
        return self._call("GET", "diskspace") or []

    def root_folders(self) -> list[dict]:
        return self._call("GET", "rootfolder") or []

    def files(self, item_ids: list[int]) -> list[dict]:
        """Files for the given items. The services cap the query length, so
        this goes in chunks."""
        out = []
        key = "movieId" if self.kind == "radarr" else "seriesId"
        path = "moviefile" if self.kind == "radarr" else "episodefile"
        for start in range(0, len(item_ids), 30):
            chunk = ",".join(str(i) for i in item_ids[start:start + 30])
            try:
                out += self._call("GET", path, params={key: chunk}) or []
            except ArrError as e:
                log.warning("File lookup failed: %s", e)
        return out

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
