"""SABnzbd.

Mostly needed for one question: does the download client still know about this
folder? Age alone is not proof — SABnzbd creates the folder when a download is
*queued*, not when it starts. With a long queue a folder can sit untouched for
hours and be perfectly fine.

SABnzbd wants its API key in the URL. No URL from this module may ever reach a
log; ``logging_setup`` redacts whatever slips through anyway.

Everything SABnzbd does is a GET, the changes included. Which of them may be
asked twice is therefore said per call rather than read off the method: a read
is retried on a dropped connection, a deletion is not.

Checked against the API of 3.7, 4.5 and the current development branch: every
mode and parameter used here exists in all three. One thing moved underneath:
since 4.2 deleting a history entry *archives* it unless told otherwise. That is
kept on purpose — the entry leaves the history either way, and an archived one
can still be recovered.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from . import connection

log = logging.getLogger(__name__)


class SabError(Exception):
    pass


class Sab:
    kind = "sabnzbd"

    def __init__(self, url: str, api_key: str, timeout: float = 20.0,
                 name: str = "SABnzbd", verify: bool = True):
        self.url = connection.normalise(url)
        self.api_key = api_key
        self.timeout = timeout
        self.name = name
        self.verify = verify
        self._client: httpx.Client | None = None
        #: The queue and the history, read once until something is changed.
        #: The download client state asked for both, and the list of names it
        #: knows asked for both again — two requests per pass for nothing.
        self._read: dict[str, Any] = {}

    def __repr__(self) -> str:
        return f"<Sab {self.url}>"

    @property
    def client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            self._client = connection.open_client(
                self.url, timeout=self.timeout, verify=self.verify,
                headers={"Accept": "application/json"})
        return self._client

    def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            self._client.close()

    def _call(self, mode: str, *, changes: bool = False, **params) -> dict:
        if changes:
            self._read.clear()
        query = {"mode": mode, "output": "json", "apikey": self.api_key, **params}
        try:
            client = self.client
        except (ValueError, httpx.InvalidURL) as e:
            raise SabError(f"The address of {self.name} is not valid: "
                           f"{connection.describe(e)}") from e
        response = connection.request(client, "GET", "api", params=query,
                                      name=self.name, fail=SabError,
                                      timeout=self.timeout, idempotent=not changes)
        # The URL is deliberately not logged — it contains the key.
        log.debug("%s %s -> %s", self.name, mode, response.status_code)
        if response.status_code in (401, 403):
            # SABnzbd 5 refuses a wrong key with 403 and a line of plain text,
            # "API Key Incorrect" — measured; older versions answered 200 with
            # the same words in JSON, handled below. Any other 403 is a name
            # it does not know (host_whitelist) or an address outside
            # inet_exposure.
            if "key" in response.text[:200].lower():
                raise SabError(f"{self.name} rejected the API key")
            raise SabError(f"{self.name} refused access (403). If it is reached "
                           f"by a host name, add that name to host_whitelist in "
                           f"its special settings.")
        if response.status_code >= 400:
            raise SabError(f"{self.name} {mode} returned {response.status_code}")
        data = connection.decode(response, name=self.name, what="SABnzbd",
                                 fail=SabError)
        if not isinstance(data, dict):
            raise SabError(f"{self.name} did not answer like SABnzbd does")
        if data.get("status") is False and data.get("error"):
            message = str(data["error"])
            if "key" in message.lower():
                raise SabError(f"{self.name} rejected the API key")
            raise SabError(message[:200])
        return data

    def _once(self, key: str, read) -> Any:
        if key not in self._read:
            self._read[key] = read()
        return self._read[key]

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
        for slot in (self.queue().get("slots") or []):
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
        # skip_dashboard is not cosmetic: without it the status call resolves
        # the public IPv4 and IPv6 address and does a DNS lookup, every time.
        # On a one-minute schedule that is a lot of pointless outbound traffic.
        return self._call("status", skip_dashboard=1).get("status", {})

    def queue(self) -> dict:
        return self._once("queue", lambda: self._call("queue", limit=500).get("queue") or {})

    def history(self, limit: int = 200) -> list[dict]:
        return self._once(f"history:{limit}", lambda: (
            self._call("history", limit=limit).get("history") or {}).get("slots") or [])

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
        self._call("warnings", name="clear", changes=True)

    def delete_history_entry(self, nzo_id: str, with_files: bool = True) -> None:
        self._call("history", name="delete", value=nzo_id,
                   del_files=1 if with_files else 0, changes=True)

    def resume(self) -> None:
        self._call("resume", changes=True)

    def resume_job(self, nzo_id: str) -> None:
        """Resume one job in the queue, leaving every other pause alone."""
        self._call("queue", name="resume", value=nzo_id, changes=True)

    def pause(self) -> None:
        self._call("pause", changes=True)
