"""The services Correctarr talks to: Radarr, Sonarr, SABnzbd and Prowlarr."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import connection, i18n
from .. import settings as S
from ..arr import Arr, ArrError
from ..prowlarr import Prowlarr
from ..sab import Sab
from . import core
from .core import BASE, fail, language_for, log, require_user, webhook_token

router = APIRouter()

KINDS = ("radarr", "sonarr", "sabnzbd", "prowlarr")


class ServiceBody(BaseModel):
    id: int | None = None
    name: str = Field(min_length=1, max_length=64)
    kind: str = "radarr"
    url: str = Field(min_length=1, max_length=500)
    api_key: str = Field(default="", max_length=200)
    enabled: bool = True
    webhook: bool = True
    #: False accepts a self-signed certificate for this service.
    verify_tls: bool = True


def _connector(kind: str, url: str, api_key: str, name: str = "",
               timeout: float = 30.0, verify: bool = True):
    if kind == "sabnzbd":
        return Sab(url, api_key, timeout=timeout, name=name or "SABnzbd",
                   verify=verify)
    if kind == "prowlarr":
        return Prowlarr(url, api_key, timeout=timeout, name=name or "Prowlarr",
                        verify=verify)
    return Arr(kind, url, api_key, timeout=timeout, name=name or kind.capitalize(),
               verify=verify)


# A service that is switched off at the far end refuses the connection at once.
# One that is simply gone swallows the packets, and then only the timeout ends
# the wait. Keep that short here: this runs while somebody is looking at a
# loading page.
PROBE_TIMEOUT = 6.0


def _probe(entry: dict) -> tuple[bool | None, str]:
    connector = _connector(entry["kind"], entry["url"], entry["api_key"],
                           entry["name"], timeout=PROBE_TIMEOUT,
                           verify=core.engine.verifies(entry))
    try:
        return connector.reachable()
    except Exception as e:                                  # noqa: BLE001
        return False, str(e)
    finally:
        connector.close()


def probe_all(entries: list[dict]) -> dict[int, tuple[bool | None, str]]:
    """Contact every service at once rather than one after another.

    Measured on four unreachable services: 11.8 seconds in sequence, and the
    overview stayed blank for all of it — every thirty seconds, because the
    page refreshes itself. In parallel it is the slowest single service, not
    the sum of them.
    """
    active = [e for e in entries if e["enabled"]]
    results: dict[int, tuple[bool | None, str]] = {
        e["id"]: (None, "disabled") for e in entries if not e["enabled"]}
    if not active:
        return results
    with ThreadPoolExecutor(max_workers=min(8, len(active))) as pool:
        for entry, outcome in zip(active, pool.map(_probe, active), strict=True):
            results[entry["id"]] = outcome
    return results


def _checked(body: ServiceBody, request: Request) -> ServiceBody:
    """A known kind, a real address, and the stored key where it may be used."""
    if body.kind not in KINDS:
        raise fail(request, 400, "error.unknown_kind", kinds=", ".join(KINDS))
    if not body.url.startswith(("http://", "https://")):
        raise fail(request, 400, "error.url_scheme")
    return _keep_stored_key(body, request)


def _keep_stored_key(body: ServiceBody, request: Request) -> ServiceBody:
    """If the mask came back, keep the stored key — for the same address.

    The key never leaves the server, and it must not leave it this way either:
    an entry whose address is changed and then tested would otherwise send
    the stored key to whatever had been typed in, without anyone having seen
    it. A new address needs the key entered again.
    """
    if body.id and (not body.api_key or body.api_key == S.MASK):
        existing = core.store.service(body.id)
        if existing:
            if connection.address(existing["url"]) != connection.address(body.url):
                raise fail(request, 400, "error.api_key_again")
            body.api_key = existing["api_key"]
    return body


def _note_certificate_choice(service_id: int, verify: bool) -> None:
    store, engine = core.store, core.engine
    unverified = {int(i) for i in (store.get(engine.UNVERIFIED, []) or [])}
    changed = unverified - {service_id} if verify else unverified | {service_id}
    if changed != unverified:
        store.set(engine.UNVERIFIED, sorted(changed))


@router.get("/api/services")
def list_services(_: dict = Depends(require_user)):
    entries = core.store.services()
    probed = probe_all(entries)
    return [{**entry, "enabled": bool(entry["enabled"]),
             "webhook": bool(entry["webhook"]),
             "verify_tls": core.engine.verifies(entry),
             # The key never leaves the server.
             "api_key": S.MASK if entry["api_key"] else "",
             "reachable": probed[entry["id"]][0],
             "info": probed[entry["id"]][1]} for entry in entries]


@router.post("/api/services")
def save_service(body: ServiceBody, request: Request, _: dict = Depends(require_user)):
    body = _checked(body, request)
    if not body.api_key:
        raise fail(request, 400, "error.api_key_missing")

    service_id = core.store.save_service(body.model_dump())
    _note_certificate_choice(service_id, body.verify_tls)
    note = ""
    if body.webhook and body.enabled and body.kind in ("radarr", "sonarr"):
        target = (core.engine.config().get("public_url") or "").rstrip("/")
        if not target:
            note = i18n.t("message.webhook_needs_url", language_for(request))
        else:
            connector = Arr(body.kind, body.url, body.api_key, name=body.name,
                            verify=body.verify_tls)
            try:
                state = connector.set_webhook(
                    f"{target}{BASE}/api/event?token={webhook_token()}")
                note = i18n.t(f"message.webhook_{state}", language_for(request))
            except ArrError as e:
                note = i18n.t("message.webhook_failed", language_for(request), error=str(e))
            finally:
                connector.close()
    return {"ok": True, "id": service_id, "webhook": note}


@router.delete("/api/services/{service_id}")
def delete_service(service_id: int, request: Request, _: dict = Depends(require_user)):
    entry = core.store.service(service_id)
    if not entry:
        raise fail(request, 404, "error.no_such_service")
    if entry["kind"] in ("radarr", "sonarr"):
        connector = Arr(entry["kind"], entry["url"], entry["api_key"], name=entry["name"],
                        verify=core.engine.verifies(entry))
        try:
            connector.remove_webhook()
        except ArrError as e:
            log.info("Could not remove the webhook: %s", e)
        finally:
            connector.close()
    core.store.delete_service(service_id)
    _note_certificate_choice(service_id, True)
    return {"ok": True}


@router.post("/api/services/test")
def test_service(body: ServiceBody, request: Request, _: dict = Depends(require_user)):
    body = _checked(body, request)
    connector = _connector(body.kind, body.url, body.api_key, body.name,
                           verify=body.verify_tls)
    try:
        ok, info = connector.reachable()
    finally:
        connector.close()
    if not ok:
        raise HTTPException(502, info)
    return {"ok": True, "info": info}
