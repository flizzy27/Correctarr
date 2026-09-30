"""How every service connection speaks HTTP.

Radarr, Sonarr, Prowlarr and SABnzbd each have their own client, and each of
them used to make the same decisions separately — and slightly differently.
What they have in common lives here:

**Redirects are followed only where the key stays safe.** A client that follows
every redirect sends the ``X-Api-Key`` header to wherever the redirect points —
httpx strips ``Authorization`` on a change of host, but not a header it does
not know is a secret. It also turns a ``POST`` into a ``GET`` on a 301 or 302,
so a command answered with a redirect was never sent — what arrived instead was
a read of the command list. A redirect is followed only for a read, only
to the same host, and never from https down to http. Anything else is reported
with the address it pointed at, which is almost always what should have been
entered in the first place.

**Only what cannot have happened twice is retried.** A read is retried on a
dropped keep-alive connection and on a gateway or start-up answer. A change is
retried only on 503, which is the service saying it did not take the request
at all. A timeout is never retried: the service is slow, and asking again makes
it slower — and a change that timed out may well have happened.

**A web page is not an answer.** A reverse proxy with a login in front of the
service answers every API call with its sign-in page, status 200. That used to
surface as "did not answer with JSON", which sent people looking at the API key
rather than at the proxy.

**No error message carries a secret.** The text of a transport error is passed
through the same redaction as the log, because it ends up in the log, in the
run record and on the screen.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from .logging_setup import redact

log = logging.getLogger(__name__)

CONNECT_TIMEOUT = 10.0

#: Answers that mean "not now" rather than "no". 503 is also what Radarr and
#: Sonarr send while they boot.
TRANSIENT = frozenset({502, 503, 504})

#: Waits before the second and third attempt. Short on purpose: a pass runs
#: every minute, and a service that is still unwell after this is unwell.
WAITS = (2.0, 4.0)

MAX_REDIRECTS = 3

#: Suffixes people paste along with the address. The clients add the API path
#: themselves, and ``http://host:7878/api/v3`` would become ``/api/v3/api/v3``.
_PASTED_SUFFIXES = ("/api/v3", "/api/v1", "/api")

#: Replaced in tests, so a retry does not cost real seconds.
sleep: Callable[[float], None] = time.sleep


def normalise(url: str) -> str:
    """The address as the clients need it: no trailing slash, no API path.

    A base path stays — ``http://host/radarr`` is how Radarr is reached behind
    a proxy that serves several services under one name.
    """
    text = (url or "").strip().rstrip("/")
    lowered = text.lower()
    for suffix in _PASTED_SUFFIXES:
        if lowered.endswith(suffix):
            text = text[: -len(suffix)].rstrip("/")
            break
    return text


def address(url: str) -> tuple[str, str]:
    """Scheme and host with port — where a key would actually be sent."""
    try:
        parts = urlsplit(normalise(url))
        return parts.scheme.lower(), parts.netloc.lower()
    except ValueError:
        return "", url


def open_client(base_url: str, *, timeout: float, verify: bool = True,
                headers: dict[str, str] | None = None,
                connections: int = 4) -> httpx.Client:
    return httpx.Client(
        base_url=base_url,
        headers=headers or {},
        timeout=httpx.Timeout(timeout, connect=min(CONNECT_TIMEOUT, timeout)),
        follow_redirects=False,
        verify=verify,
        limits=httpx.Limits(max_connections=connections,
                            max_keepalive_connections=max(1, connections // 2)))


def request(client: httpx.Client, method: str, path: str, *, name: str,
            fail: type[Exception], timeout: float,
            idempotent: bool | None = None, **kwargs) -> httpx.Response:
    """Send one request, with the retries and redirects described above.

    ``fail`` is the error type of the calling client, so a caller only ever
    has to catch its own kind of error. ``idempotent`` defaults to "is this a
    read"; SABnzbd changes things through GET and says so explicitly.
    """
    if idempotent is None:
        idempotent = method.upper() == "GET"
    attempt = 0
    while True:
        try:
            response = _send(client, method, path, name=name, fail=fail, **kwargs)
        except httpx.TimeoutException as e:
            raise fail(f"{name} did not answer within {timeout:.0f}s") from e
        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError) as e:
            # A kept-alive connection the other side had already closed. The
            # request never arrived, so a read can simply go again.
            if idempotent and attempt < len(WAITS):
                attempt += 1
                continue
            raise fail(f"{name} dropped the connection: {describe(e)}") from e
        except httpx.RequestError as e:
            raise fail(unreachable(name, e)) from e
        except httpx.InvalidURL as e:
            raise fail(f"The address of {name} is not valid: {describe(e)}") from e

        # A booting service is given the full wait. A proxy that says the
        # service behind it is gone is asked once more and then believed — the
        # overview probes every service on every refresh and should not stand
        # still for six seconds per service that is simply switched off.
        starting = response.status_code == 503
        retry = response.status_code in TRANSIENT and (idempotent or starting)
        if retry and attempt < (len(WAITS) if starting else 1):
            log.info("%s answered %d, asking again shortly", name, response.status_code)
            sleep(WAITS[attempt])
            attempt += 1
            continue
        return response


def _send(client: httpx.Client, method: str, path: str, *, name: str,
          fail: type[Exception], **kwargs) -> httpx.Response:
    response = client.request(method, path, **kwargs)
    hops = 0
    while response.is_redirect:
        target = response.next_request
        source = response.request.url
        if target is None:
            return response
        allowed = (method.upper() == "GET" and hops < MAX_REDIRECTS
                   and target.url.host == source.host
                   and not (source.scheme == "https" and target.url.scheme == "http"))
        if not allowed:
            raise fail(f"{name} redirects to {bare(target.url)} — enter that "
                       f"address instead, or, if it is a login page, the "
                       f"address of {name} itself rather than of the proxy in front")
        hops += 1
        response = client.send(target)
    return response


def decode(response: httpx.Response, *, name: str, what: str,
           fail: type[Exception]) -> Any:
    """The JSON body, or an error that says what came back instead."""
    if not response.content:
        return None
    kind = response.headers.get("content-type", "").lower()
    head = response.content[:64].lstrip().lower()
    if "html" in kind or head.startswith((b"<!doctype", b"<html")):
        raise fail(f"{name} answered with a web page instead of data — is a "
                   f"login page or a reverse proxy in front of it, or is the "
                   f"base path missing from the address?")
    try:
        return response.json()
    except ValueError as e:
        raise fail(f"{name} did not answer with JSON — does the address really "
                   f"point at {what}?") from e


def unreachable(name: str, error: Exception) -> str:
    text = describe(error)
    if "CERTIFICATE_VERIFY_FAILED" in text or "certificate verify failed" in text:
        return (f"{name} presents a certificate that is not trusted. If it is "
                f"self-signed, switch on accepting it for this service.")
    return f"{name} is unreachable: {text}"


def describe(error: Exception) -> str:
    """The text of a transport error, safe to log and to show."""
    return redact(str(error) or type(error).__name__)[:300]


def bare(url: httpx.URL) -> str:
    """An address without its query — SABnzbd's key travels in there."""
    return redact(str(url.copy_with(query=None, fragment=None)))


def short_text(response: httpx.Response, limit: int = 200) -> str:
    """The start of an error body, without markup and without secrets."""
    text = response.text[:2000]
    if "<" in text and ">" in text:
        text = " ".join(part.split(">", 1)[-1] for part in text.split("<"))
    return redact(" ".join(text.split()))[:limit]
