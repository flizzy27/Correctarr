"""The three pages, the liveness check and the interface strings."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import i18n
from .core import BASE, VERSION, is_set_up, language_for

router = APIRouter()

TEMPLATES = Path(__file__).parent.parent / "templates"

#: The pages are read once and kept. They are three small files and they do
#: not change while the process is running.
_PAGES: dict[str, str] = {}


def _page(name: str) -> HTMLResponse:
    """One of the three pages, with the version written into its asset links.

    The page itself is never cached; the script and the stylesheet beside it
    are, and that is the whole problem this solves. Without a version in the
    link, a browser that has been here before keeps the interface it already
    has — so an update lands, the container restarts, the API changes, and the
    person in front of it is still running last month's script against it.
    Nothing says so; things simply stop working in ways that make no sense.

    A query that changes with the version makes it a different URL, so the
    browser fetches it exactly once per release and caches it happily in
    between.
    """
    if name not in _PAGES:
        text = (TEMPLATES / name).read_text(encoding="utf-8")
        _PAGES[name] = text.replace("__V__", VERSION)
    return HTMLResponse(_PAGES[name],
                        headers={"Cache-Control": "no-store"})


@router.get("/")
def index():
    return _page("index.html")


@router.get("/login")
def login_page():
    if not is_set_up():
        return RedirectResponse(f"{BASE}/setup", status_code=303)
    return _page("login.html")


@router.get("/setup")
def setup_page():
    if is_set_up():
        return RedirectResponse(f"{BASE}/", status_code=303)
    return _page("setup.html")


# ---------------------------------------------------------------------------
# Liveness — deliberately without any outbound call
# ---------------------------------------------------------------------------
@router.get("/api/alive")
def alive():
    """For the container health check.

    Calls nothing outward. The previous check hung off the status endpoint, and
    that queries every configured service — a service that swallows packets
    instead of refusing them would have had the container reported as unhealthy
    while it was working perfectly.
    """
    return {"ok": True, "version": VERSION}


@router.get("/api/language")
def language(request: Request):
    """Strings for the interface, in the language for this request."""
    code = language_for(request)
    return {"language": code, "available": list(i18n.AVAILABLE),
            "names": i18n.LANGUAGE_NAMES, "strings": i18n.bundle(code)}
