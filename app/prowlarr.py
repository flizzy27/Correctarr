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

log = logging.getLogger(__name__)


class ProwlarrError(Exception):
    pass


class Prowlarr:
    kind = "prowlarr"

    def __init__(self, url: str, api_key: str, timeout: float = 30.0,
                 name: str = "Prowlarr"):
        self.url = (url or "").rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.name = name
        self._client: httpx.Client | None = None

    def __repr__(self) -> str:
        return f"<Prowlarr {self.url}>"

    @property
    def client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(
                base_url=f"{self.url}/api/v1/",
                headers={"X-Api-Key": self.api_key, "Accept": "application/json"},
                timeout=httpx.Timeout(self.timeout, connect=10.0),
                follow_redirects=True,
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=2))
        return self._client

    def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            self._client.close()

    def _call(self, path: str, **params):
        try:
            response = self.client.get(path.lstrip("/"), params=params or None)
        except httpx.TimeoutException as e:
            raise ProwlarrError(f"{self.name} did not answer within "
                                f"{self.timeout:.0f}s") from e
        except httpx.RequestError as e:
            raise ProwlarrError(f"{self.name} is unreachable: {e}") from e
        if response.status_code == 401:
            raise ProwlarrError(f"{self.name} rejected the API key")
        if response.status_code >= 400:
            raise ProwlarrError(f"{self.name} {path} returned {response.status_code}")
        try:
            return response.json()
        except ValueError as e:
            raise ProwlarrError(f"{self.name} did not answer with JSON — does the "
                                f"address really point at Prowlarr?") from e

    def reachable(self) -> tuple[bool, str]:
        try:
            status = self._call("system/status")
        except ProwlarrError as e:
            return False, str(e)
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
