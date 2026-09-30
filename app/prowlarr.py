"""Prowlarr.

Prowlarr manages indexers centrally and keeps count of how often each one was
queried and how often that turned into a grab. Combined with the history from
Radarr and Sonarr — which knows **how good** the grabbed release was — that is
enough to judge which indexer is actually carrying its weight and which one
only burns queries.

Every call here reads. Nothing is changed: a wrongly set indexer priority is
easy to make and hard to notice later.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx

from . import connection

log = logging.getLogger(__name__)


class ProwlarrError(Exception):
    pass


class Prowlarr:
    kind = "prowlarr"

    def __init__(self, url: str, api_key: str, timeout: float = 30.0,
                 name: str = "Prowlarr", verify: bool = True):
        self.url = connection.normalise(url)
        self.api_key = api_key
        self.timeout = timeout
        self.name = name
        self.verify = verify
        self._client: httpx.Client | None = None

    def __repr__(self) -> str:
        return f"<Prowlarr {self.url}>"

    @property
    def client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            self._client = connection.open_client(
                f"{self.url}/api/v1/", timeout=self.timeout, verify=self.verify,
                headers={"X-Api-Key": self.api_key, "Accept": "application/json"})
        return self._client

    def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            self._client.close()

    def _call(self, path: str, **params):
        try:
            client = self.client
        except (ValueError, httpx.InvalidURL) as e:
            raise ProwlarrError(f"The address of {self.name} is not valid: "
                                f"{connection.describe(e)}") from e
        response = connection.request(client, "GET", path.lstrip("/"),
                                      params=params or None, name=self.name,
                                      fail=ProwlarrError, timeout=self.timeout)
        if response.status_code == 401:
            raise ProwlarrError(f"{self.name} rejected the API key")
        if response.status_code >= 400:
            raise ProwlarrError(f"{self.name} {path} returned {response.status_code}")
        return connection.decode(response, name=self.name, what="Prowlarr",
                                 fail=ProwlarrError)

    def reachable(self) -> tuple[bool, str]:
        try:
            status = self._call("system/status")
        except ProwlarrError as e:
            return False, str(e)
        if not isinstance(status, dict):
            return False, f"{self.name} did not answer like Prowlarr does"
        name = status.get("appName") or "Prowlarr"
        if name.lower() != "prowlarr":
            return False, f"{name} is running there, but this entry says Prowlarr"
        return True, f"{name} {status.get('version', '?')}"

    def indexers(self) -> list[dict]:
        return self._call("indexer") or []

    def indexer_status(self) -> list[dict]:
        """Indexers Prowlarr has temporarily disabled because of errors."""
        return self._call("indexerstatus") or []

    def stats(self, days: int = 30) -> list[dict]:
        until = datetime.now(UTC)
        since = until - timedelta(days=days)
        data = self._call("indexerstats",
                          startDate=since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                          endDate=until.strftime("%Y-%m-%dT%H:%M:%SZ")) or {}
        return data.get("indexers") or []

    def health(self) -> list[dict]:
        return self._call("health") or []
