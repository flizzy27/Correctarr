"""Correctarr — the web application: interface, API and schedule together.

The routes live in :mod:`app.api`, one module per area; what runs on a clock
or on a webhook in :mod:`app.api.jobs`. This puts them together and starts
them.

Behind a reverse proxy
----------------------
``BASE_URL`` sets the sub path the interface is reachable under, for example
``/correctarr``. The interface itself uses relative URLs throughout and works
without that setting as long as the proxy maps to the root. uvicorn runs with
``--proxy-headers`` so ``X-Forwarded-Proto`` is honoured — without it the
service would treat an HTTPS connection as plain and set the session cookie
without ``Secure``.
"""
from __future__ import annotations

import mimetypes
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import auth, i18n
from .api import (
    accounts,
    activity,
    backups,
    core,
    findings,
    insights,
    jobs,
    notify,
    pages,
    profiles,
    rules,
    services,
    settings,
    status,
    storage,
    webhook,
    wizard,
)
from .api.core import BASE, VERSION, log

HERE = Path(__file__).parent


def _seed_services() -> None:
    """On the very first start, take services from the environment.

    Convenience for anyone deploying with compose. The truth lives in the
    database afterwards and is maintained through the interface.
    """
    store = core.store
    if store.services():
        return
    pairs = (("radarr", "RADARR"), ("sonarr", "SONARR"),
             ("sabnzbd", "SAB"), ("prowlarr", "PROWLARR"))
    labels = {"sabnzbd": "SABnzbd", "prowlarr": "Prowlarr"}
    for kind, prefix in pairs:
        url = os.getenv(f"{prefix}_URL")
        api_key = os.getenv(f"{prefix}_API_KEY")
        if not (url and api_key):
            continue
        store.save_service({
            "name": labels.get(kind, kind.capitalize()), "kind": kind,
            "url": url, "api_key": api_key, "enabled": True,
            "webhook": kind in ("radarr", "sonarr")})
        log.info("Adopted the %s service from the environment", kind)


@asynccontextmanager
async def lifespan(_: FastAPI):
    _seed_services()
    core.webhook_token()
    core.store.prune_sessions()
    jobs.schedule()
    jobs.scheduler.start()
    log.info("Correctarr %s ready — %d service(s), auth %s%s",
             VERSION, len(core.store.services()), auth.mode(),
             f", sub path {BASE}" if BASE else "")
    if auth.mode() == "off":
        log.warning("Authentication is disabled (AUTH=off). The interface is "
                    "reachable by anyone who can reach the port.")
    missing = i18n.missing_keys()
    for code, keys in missing.items():
        if keys:
            log.warning("Locale %s is missing %d keys, e.g. %s",
                        code, len(keys), ", ".join(keys[:3]))
    yield
    jobs.scheduler.shutdown(wait=False)


app = FastAPI(title="Correctarr", version=VERSION, lifespan=lifespan,
              root_path=BASE, docs_url=None, redoc_url=None, openapi_url=None)
# The interface is a set of JavaScript modules, and a browser refuses to run
# a module served as anything but JavaScript. The type is looked up in the
# system's own table, and on Windows that table can say text/plain for .js —
# the page then stays blank without a word of explanation.
mimetypes.add_type("text/javascript", ".js")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
app.middleware("http")(core.gatekeeper)
for area in (pages, accounts, webhook, services, status, settings, rules, findings,
             wizard, notify, profiles, storage, activity, insights, backups):
    app.include_router(area.router)
