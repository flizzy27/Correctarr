"""The service clients against recorded and made-up answers.

No test here reaches the network: every request is answered by a function
standing in for the service, which also counts what was asked.
"""
from __future__ import annotations

import httpx
import pytest

from app import connection
from app.arr import Arr, ArrError, GoneError
from app.prowlarr import Prowlarr, ProwlarrError
from app.sab import Sab, SabError


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    waits = []
    monkeypatch.setattr(connection, "sleep", waits.append)
    return waits


class Service:
    """Answers requests the way a service would, and remembers them."""

    def __init__(self, routes: dict | None = None):
        self.routes = dict(routes or {})
        self.asked: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(request)
        key = f"{request.method} {request.url.path}"
        answer = self.routes.get(key, self.routes.get(request.url.path))
        if answer is None:
            return httpx.Response(404, json={"message": "NotFound"})
        if callable(answer):
            return answer(request)
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.asked]


@pytest.fixture
def wire(monkeypatch):
    """Route every client built from here on to a stand-in service."""
    def attach(service: Service):
        original = connection.open_client

        def open_client(base_url, **kwargs):
            client = original(base_url, **kwargs)
            client._transport = httpx.MockTransport(service)
            return client
        monkeypatch.setattr(connection, "open_client", open_client)
        return service
    return attach


def radarr(**kwargs) -> Arr:
    return Arr("radarr", "http://radarr:7878", "the-radarr-key", **kwargs)


# ---------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("typed, expected", [
    ("http://radarr:7878/", "http://radarr:7878"),
    ("  http://radarr:7878  ", "http://radarr:7878"),
    ("http://radarr:7878/api/v3", "http://radarr:7878"),
    ("http://radarr:7878/api/v3/", "http://radarr:7878"),
    ("http://prowlarr:9696/api/v1", "http://prowlarr:9696"),
    ("http://sab:8080/api", "http://sab:8080"),
    ("https://example.com/radarr", "https://example.com/radarr"),
    ("https://example.com/radarr/api/v3", "https://example.com/radarr"),
    ("http://[fd00::5]:7878/", "http://[fd00::5]:7878"),
])
def test_an_address_is_taken_as_typed_minus_what_the_client_adds(typed, expected):
    assert connection.normalise(typed) == expected


def test_a_base_path_is_kept_in_every_request(wire):
    service = wire(Service({"/radarr/api/v3/system/status": {
        "appName": "Radarr", "version": "6.4.4.10704"}}))
    ok, info = Arr("radarr", "https://example.com/radarr/", "key").reachable()
    assert ok, info
    assert service.paths() == ["/radarr/api/v3/system/status"]


def test_an_ipv6_address_is_reachable(wire):
    service = wire(Service({"/api/v3/system/status": {"appName": "Radarr",
                                                      "version": "5.28.0.1"}}))
    ok, _ = Arr("radarr", "http://[fd00::5]:7878", "key").reachable()
    assert ok
    assert service.asked[0].url.host == "fd00::5"


def test_a_malformed_address_is_an_error_of_the_client_not_a_crash():
    arr = Arr("radarr", "http://radarr:notaport", "key")
    ok, info = arr.reachable()
    assert not ok
    assert "not valid" in info


def test_the_certificate_choice_reaches_the_connection(monkeypatch):
    seen = {}

    def open_client(base_url, **kwargs):
        seen.update(kwargs)
        return httpx.Client(base_url=base_url,
                            transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    monkeypatch.setattr(connection, "open_client", open_client)
    _ = radarr(verify=False).client
    assert seen["verify"] is False
    _ = Sab("http://sab:8080", "key", verify=False).client
    assert seen["verify"] is False
    _ = Prowlarr("http://prowlarr:9696", "key").client
    assert seen["verify"] is True, "checked unless switched off"


# ---------------------------------------------------------------------------
# Redirects
# ---------------------------------------------------------------------------
def test_a_read_follows_a_redirect_on_the_same_host(wire):
    service = wire(Service({
        "/api/v3/health": httpx.Response(
            301, headers={"Location": "http://radarr:7878/radarr/api/v3/health"}),
        "/radarr/api/v3/health": [{"type": "warning"}],
    }))
    assert radarr().health() == [{"type": "warning"}]
    assert service.paths() == ["/api/v3/health", "/radarr/api/v3/health"]


def test_the_key_is_not_carried_to_another_host(wire):
    service = wire(Service({"/api/v3/health": httpx.Response(
        302, headers={"Location": "https://auth.example.com/login?rd=radarr"})}))
    with pytest.raises(ArrError) as caught:
        radarr().health()
    assert len(service.asked) == 1, "never sent to the other host"
    assert "auth.example.com/login" in str(caught.value)


def test_a_change_is_never_turned_into_a_read_by_a_redirect(wire):
    """httpx follows a 301 on a POST as a GET. A command answered that way
    came back as the list of commands, and counted as sent."""
    service = wire(Service({"POST /api/v3/command": httpx.Response(
        301, headers={"Location": "https://radarr:7878/api/v3/command"})}))
    with pytest.raises(ArrError):
        radarr().command("RescanMovie")
    assert [r.method for r in service.asked] == ["POST"]


def test_https_is_not_given_up_for_http(wire):
    wire(Service({"/api/v3/health": httpx.Response(
        301, headers={"Location": "http://radarr:7878/api/v3/health"})}))
    arr = Arr("radarr", "https://radarr:7878", "key")
    with pytest.raises(ArrError):
        arr.health()


def test_sabnzbd_does_not_show_its_key_in_a_redirect_message(wire):
    wire(Service({"/api": httpx.Response(
        302, headers={"Location": "https://other.example.com/api?apikey=the-sab-key-123"})}))
    with pytest.raises(SabError) as caught:
        Sab("http://sab:8080", "the-sab-key-123").status()
    assert "the-sab-key-123" not in str(caught.value)


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------
def test_a_read_is_asked_again_while_the_service_starts(wire, no_waiting):
    answers = iter([httpx.Response(503), httpx.Response(503),
                    httpx.Response(200, json=[])])
    service = wire(Service({"/api/v3/health": lambda r: next(answers)}))
    assert radarr().health() == []
    assert len(service.asked) == 3
    assert no_waiting == list(connection.WAITS)


def test_the_retries_run_out(wire):
    service = wire(Service({"/api/v3/health": httpx.Response(503)}))
    with pytest.raises(ArrError, match="starting up"):
        radarr().health()
    assert len(service.asked) == 1 + len(connection.WAITS)


def test_a_gateway_error_is_asked_about_once_more_and_then_believed(wire, no_waiting):
    service = wire(Service({"/api/v3/health": httpx.Response(502)}))
    with pytest.raises(ArrError, match="502"):
        radarr().health()
    assert len(service.asked) == 2
    assert no_waiting == [connection.WAITS[0]]


def test_a_change_is_not_repeated_on_a_gateway_error(wire):
    service = wire(Service({"DELETE /api/v3/queue/7": httpx.Response(502)}))
    with pytest.raises(ArrError):
        radarr().remove_from_queue(7)
    assert len(service.asked) == 1


def test_a_change_is_repeated_when_the_service_says_it_did_not_take_it(wire):
    answers = iter([httpx.Response(503), httpx.Response(200)])
    service = wire(Service({"DELETE /api/v3/queue/7": lambda r: next(answers)}))
    radarr().remove_from_queue(7)
    assert len(service.asked) == 2


def test_a_timeout_is_not_retried(wire):
    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)
    service = wire(Service({"/api/v3/health": slow}))
    with pytest.raises(ArrError, match="did not answer within"):
        radarr().health()
    assert len(service.asked) == 1


def test_a_dropped_keepalive_connection_is_retried_for_a_read_only(wire):
    def dropped(request):
        raise httpx.RemoteProtocolError("Server disconnected", request=request)
    reads = wire(Service({"/api/v3/health": dropped}))
    with pytest.raises(ArrError, match="dropped the connection"):
        radarr().health()
    assert len(reads.asked) == 1 + len(connection.WAITS)

    changes = wire(Service({"POST /api/v3/command": dropped}))
    with pytest.raises(ArrError):
        radarr().command("RescanMovie")
    assert len(changes.asked) == 1


def test_a_sabnzbd_deletion_is_not_repeated(wire):
    def dropped(request):
        raise httpx.ReadError("reset", request=request)
    service = wire(Service({"/api": dropped}))
    with pytest.raises(SabError):
        Sab("http://sab:8080", "key").delete_history_entry("SABnzbd_nzo_1")
    assert len(service.asked) == 1


# ---------------------------------------------------------------------------
# What comes back
# ---------------------------------------------------------------------------
LOGIN_PAGE = "<!DOCTYPE html><html><body><form>Sign in</form></body></html>"


@pytest.mark.parametrize("make", [
    lambda: radarr().health(),
    lambda: Prowlarr("http://prowlarr:9696", "key").indexers(),
    lambda: Sab("http://sab:8080", "key").status(),
])
def test_a_login_page_is_called_a_login_page(wire, make):
    wire(Service({
        "/api/v3/health": httpx.Response(200, text=LOGIN_PAGE,
                                         headers={"Content-Type": "text/html"}),
        "/api/v1/indexer": httpx.Response(200, text=LOGIN_PAGE),
        "/api": httpx.Response(200, text=LOGIN_PAGE,
                               headers={"Content-Type": "text/html; charset=utf-8"}),
    }))
    with pytest.raises((ArrError, ProwlarrError, SabError), match="web page"):
        make()


def test_something_that_is_not_json_says_so(wire):
    wire(Service({"/api/v3/health": httpx.Response(
        200, text="ok", headers={"Content-Type": "text/plain"})}))
    with pytest.raises(ArrError, match="did not answer with JSON"):
        radarr().health()


def test_an_error_page_is_shown_as_text_without_its_markup(wire):
    wire(Service({"/api/v3/health": httpx.Response(
        500, text="<html><body><h1>Internal error</h1></body></html>")}))
    with pytest.raises(ArrError) as caught:
        radarr().health()
    assert "Internal error" in str(caught.value)
    assert "<h1>" not in str(caught.value)


def test_a_certificate_that_is_not_trusted_points_at_the_switch(wire):
    def untrusted(request):
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate "
                                 "verify failed: self-signed certificate",
                                 request=request)
    wire(Service({"/api/v3/system/status": untrusted}))
    ok, info = Arr("radarr", "https://radarr:7878", "key").reachable()
    assert not ok
    assert "self-signed" in info


def test_sabnzbd_behind_a_host_name_it_does_not_know(wire):
    wire(Service({"/api": httpx.Response(
        403, text="Access denied - Hostname verification failed")}))
    with pytest.raises(SabError, match="host_whitelist"):
        Sab("http://sab.example.com", "key").status()


@pytest.mark.parametrize("answer", [
    # SABnzbd 5, measured
    httpx.Response(403, text="API Key Incorrect",
                   headers={"Content-Type": "text/html;charset=utf-8"}),
    # SABnzbd 3 and 4
    httpx.Response(200, json={"status": False, "error": "API Key Incorrect"}),
])
def test_sabnzbd_with_a_wrong_key_says_so(wire, answer):
    wire(Service({"/api": answer}))
    ok, info = Sab("http://sab:8080", "wrong").reachable()
    assert not ok
    assert info == "SABnzbd rejected the API key"


def test_an_answer_that_is_not_the_service_is_not_taken_for_it(wire):
    wire(Service({"/api/v3/system/status": ["not", "a", "status"],
                  "/api/v1/system/status": ["nor", "this"]}))
    assert radarr().reachable()[0] is False
    assert Prowlarr("http://prowlarr:9696", "key").reachable()[0] is False


# ---------------------------------------------------------------------------
# Paging
# ---------------------------------------------------------------------------
def paged(total: int, prefix: str = "row"):
    def answer(request):
        page = int(request.url.params.get("page", 1))
        size = int(request.url.params["pageSize"])
        start = (page - 1) * size
        records = [{"id": i, "title": f"{prefix} {i}"}
                   for i in range(start, min(start + size, total))]
        return httpx.Response(200, json={"page": page, "pageSize": size,
                                         "totalRecords": total, "records": records})
    return answer


def test_a_queue_longer_than_one_page_is_read_in_full(wire):
    service = wire(Service({"/api/v3/queue": paged(2345)}))
    queue = radarr().queue()
    assert len(queue) == 2345
    assert len({entry["id"] for entry in queue}) == 2345
    assert len(service.asked) == 3


def test_paging_stops_at_its_bound(wire):
    service = wire(Service({"/api/v3/blocklist": paged(100_000)}))
    arr = radarr()
    rows = arr.blocklist()
    assert len(rows) == Arr.BLOCKLIST_MOST
    assert len(service.asked) == Arr.BLOCKLIST_MOST // 500


def test_the_oldest_blocklist_entries_are_reached(wire):
    """Newest first and one page of 500: the stale blocklist rule, which looks
    for the old ones, never saw an entry beyond the five hundredth."""
    wire(Service({"/api/v3/blocklist": paged(1200)}))
    assert radarr().blocklist()[-1]["id"] == 1199


def test_a_short_answer_ends_the_paging_even_without_a_total(wire):
    service = wire(Service({"/api/v3/wanted/missing": {"records": [{"id": 1}]}}))
    assert radarr().missing() == [{"id": 1}]
    assert len(service.asked) == 1


# ---------------------------------------------------------------------------
# Asking once
# ---------------------------------------------------------------------------
def test_a_pass_reads_each_list_once(wire):
    service = wire(Service({
        "/api/v3/queue": paged(3),
        "/api/v3/qualityprofile": [{"id": 1}],
        "/api/v3/customformat": [{"id": 2}],
        "/api/v3/movie": [{"id": 3}],
    }))
    arr = radarr()
    for _ in range(3):
        arr.queue(), arr.profiles(), arr.custom_formats(), arr.items()
    assert sorted(service.paths()) == ["/api/v3/customformat", "/api/v3/movie",
                                       "/api/v3/qualityprofile", "/api/v3/queue"]


def test_a_change_makes_the_next_read_ask_again(wire):
    service = wire(Service({"/api/v3/queue": paged(3),
                            "DELETE /api/v3/queue/1": httpx.Response(200)}))
    arr = radarr()
    arr.queue()
    arr.remove_from_queue(1)
    arr.queue()
    assert service.paths().count("/api/v3/queue") == 2


def test_sabnzbd_queue_and_history_are_read_once_per_pass(wire):
    def answer(request):
        mode = request.url.params["mode"]
        if mode == "queue":
            return httpx.Response(200, json={"queue": {"slots": [{"filename": "A"}]}})
        if mode == "history":
            return httpx.Response(200, json={"history": {"slots": [{"name": "B"}]}})
        return httpx.Response(200, json={"status": True})
    service = wire(Service({"/api": answer}))
    sab = Sab("http://sab:8080", "key")
    sab.queue()
    sab.history(200)
    assert sab.known_names() == {"A", "B"}
    assert len(service.asked) == 2
    sab.delete_history_entry("x")
    sab.queue()
    assert len(service.asked) == 4


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------
def test_sonarr_3_has_no_custom_formats_and_that_is_not_an_error(wire):
    wire(Service({"/api/v3/customformat": httpx.Response(404)}))
    assert Arr("sonarr", "http://sonarr:8989", "key").custom_formats() == []


def test_radarr_4_reads_what_is_wanted_from_the_movie_list(wire):
    movies = [
        {"id": 1, "monitored": True, "hasFile": False},
        {"id": 2, "monitored": False, "hasFile": False},
        {"id": 3, "monitored": True, "hasFile": True,
         "movieFile": {"qualityCutoffNotMet": True}},
        {"id": 4, "monitored": True, "hasFile": True,
         "movieFile": {"qualityCutoffNotMet": False}},
    ]
    service = wire(Service({"/api/v3/movie": movies}))
    arr = radarr()
    assert [m["id"] for m in arr.missing()] == [1]
    assert [m["id"] for m in arr.below_cutoff()] == [3]
    assert service.paths().count("/api/v3/movie") == 1


def test_sonarr_without_a_wanted_list_still_reports_the_problem(wire):
    wire(Service({}))
    with pytest.raises(GoneError):
        Arr("sonarr", "http://sonarr:8989", "key").missing()


def test_the_version_is_noted_from_the_header(wire):
    wire(Service({"/api/v3/health": httpx.Response(
        200, json=[], headers={"X-Application-Version": "6.4.4.10704"})}))
    arr = radarr()
    arr.health()
    assert arr.observed.major == 6
