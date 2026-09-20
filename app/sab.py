"""SABnzbd.

Mostly needed for one question: does the download client still know about this
folder? Age alone is not proof — SABnzbd creates the folder when a download is
*queued*, not when it starts. With a long queue a folder can sit untouched for
hours and be perfectly fine.

SABnzbd wants its API key in the URL. No URL from this module may ever reach a
log; ``logging_setup`` redacts whatever slips through anyway.
"""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


class SabError(Exception):
    pass


class Sab:
    kind = "sabnzbd"

    def __init__(self, url: str, api_key: str, timeout: float = 20.0,
                 name: str = "SABnzbd"):
        self.url = (url or "").rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.name = name
        self._client: httpx.Client | None = None

    def __repr__(self) -> str:
        return f"<Sab {self.url}>"

    @property
    def client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(
                base_url=self.url,
                timeout=httpx.Timeout(self.timeout, connect=10.0),
                follow_redirects=True,
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=2))
        return self._client

    def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            self._client.close()

    def _call(self, mode: str, **params) -> dict:
        query = {"mode": mode, "output": "json", "apikey": self.api_key, **params}
        try:
            response = self.client.get("/api", params=query)
        except httpx.TimeoutException as e:
            raise SabError(f"{self.name} did not answer within "
                           f"{self.timeout:.0f}s") from e
        except httpx.RequestError as e:
            raise SabError(f"{self.name} is unreachable: {e}") from e
        # The URL is deliberately not logged — it contains the key.
        log.debug("%s %s -> %s", self.name, mode, response.status_code)
        if response.status_code >= 400:
            raise SabError(f"{self.name} {mode} returned {response.status_code}")
        try:
            data = response.json()
        except ValueError as e:
            raise SabError(f"{self.name} did not answer with JSON — does the "
                           f"address really point at SABnzbd?") from e
        if isinstance(data, dict) and data.get("status") is False and data.get("error"):
            message = str(data["error"])
            if "key" in message.lower():
                raise SabError(f"{self.name} rejected the API key")
            raise SabError(message)
        return data

    def reachable(self) -> tuple[bool, str]:
        try:
            version = self._call("version")
            # 'version' answers without a valid key. Only a protected call
            # proves the key is right.
            self._call("queue", limit=1)
            return True, f"SABnzbd {version.get('version', '?')}"
        except SabError as e:
            return False, str(e)

    # -- reading ---------------------------------------------------------------
    def known_names(self) -> set[str]:
        """Every name currently tracked — queue and recent history."""
        names: set[str] = set()
        for slot in (self._call("queue", limit=500).get("queue", {}).get("slots") or []):
            for key in ("filename", "nzb_name", "name"):
                if slot.get(key):
                    names.add(str(slot[key]))
        try:
            for slot in self.history(200):
                for key in ("name", "nzb_name", "storage"):
                    if slot.get(key):
                        names.add(str(slot[key]).rsplit("/", 1)[-1])
        except SabError as e:
            # History is a bonus, the queue is the requirement.
            log.warning("Could not read history from %s: %s", self.name, e)
        return names

    def status(self) -> dict:
        return self._call("status").get("status", {})

    def queue(self) -> dict:
        return self._call("queue", limit=500).get("queue", {})

    def history(self, limit: int = 200) -> list[dict]:
        return self._call("history", limit=limit).get("history", {}).get("slots") or []

    def warnings(self) -> list[dict]:
        data = self._call("warnings")
        raw = data.get("warnings", data)
        if not isinstance(raw, list):
            return []
        out = []
        for entry in raw:
            if isinstance(entry, dict):
                out.append({"kind": entry.get("type", "WARNING"),
                            "text": str(entry.get("text", "")),
                            "at": entry.get("time"), "source": entry.get("origin")})
            else:
                out.append({"kind": "WARNING", "text": str(entry),
                            "at": None, "source": None})
        return out

    def failed(self, limit: int = 100) -> list[dict]:
        return [s for s in self.history(limit)
                if str(s.get("status", "")).lower() == "failed"]

    # -- acting ----------------------------------------------------------------
    def clear_warnings(self) -> None:
        self._call("warnings", name="clear")

    def delete_history_entry(self, nzo_id: str, with_files: bool = True) -> None:
        self._call("history", name="delete", value=nzo_id,
                   del_files=1 if with_files else 0)

    def resume(self) -> None:
        self._call("resume")

    def pause(self) -> None:
        self._call("pause")
