"""Keeping up with services that change without announcing it.

Each of these is a real difference between the two applications, or a rename
they have already announced. None of them would fail loudly: a wrong command
name is a server error nobody sees, and a history filter built on numbers
returns the wrong rows with a perfectly normal-looking 200.
"""
from app import compat


# ---------------------------------------------------------------------------
# Fields that are being renamed
# ---------------------------------------------------------------------------
def test_a_field_is_found_under_its_current_name():
    assert compat.field({"sizeleft": 42}, "sizeleft") == 42


def test_a_field_is_found_under_its_announced_name():
    assert compat.field({"sizeLeft": 42}, "sizeleft") == 42


def test_a_field_with_no_aliases_is_read_as_given():
    assert compat.field({"title": "x"}, "title") == "x"
    assert compat.field({}, "title", "fallback") == "fallback"


def test_a_number_that_is_not_one_does_not_propagate():
    assert compat.number({"sizeleft": "nonsense"}, "sizeleft") == 0.0
    assert compat.number({"sizeleft": None}, "sizeleft") == 0.0
    assert compat.number({}, "sizeleft", 7.0) == 7.0


def test_a_number_arriving_as_a_string_is_still_a_number():
    assert compat.number({"sizeleft": "1024"}, "sizeleft") == 1024.0


# ---------------------------------------------------------------------------
# History events
# ---------------------------------------------------------------------------
def test_a_grab_is_recognised_by_name():
    assert compat.event_is({"eventType": "grabbed"}, compat.GRABBED)
    assert compat.event_is({"eventType": "Grabbed"}, compat.GRABBED)


def test_a_grab_is_recognised_by_the_number_both_applications_agree_on():
    assert compat.event_is({"eventType": 1}, compat.GRABBED)


def test_a_different_event_is_not_a_grab():
    assert not compat.event_is({"eventType": "downloadFailed"}, compat.GRABBED)
    assert not compat.event_is({"eventType": 4}, compat.GRABBED)
    assert not compat.event_is({}, compat.GRABBED)


def test_the_numbers_that_disagree_between_applications_are_not_used():
    # 6 means "file deleted" in one and "file renamed" in the other, so no
    # kind here may claim it. Matching it would return the wrong rows in one
    # of the two applications, quietly.
    for kind in (compat.GRABBED, compat.IMPORTED, compat.FAILED):
        assert 6 not in kind[1:]
        assert 7 not in kind[1:]


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def test_the_update_check_is_spelled_differently_in_each_application():
    # Not a typo in either place, and an unknown name is a server error rather
    # than a refusal — so it cannot be discovered by trying.
    assert compat.command_for("radarr", "check_update") == "ApplicationCheckUpdate"
    assert compat.command_for("sonarr", "check_update") == "ApplicationUpdateCheck"


def test_every_application_has_the_commands_this_program_uses():
    for kind in ("radarr", "sonarr"):
        for purpose in ("search", "refresh", "rescan", "check_update"):
            assert compat.command_for(kind, purpose), f"{kind}/{purpose}"


def test_an_unknown_purpose_gives_nothing_rather_than_a_guess():
    assert compat.command_for("radarr", "make_coffee") is None
    assert compat.command_for("plex", "search") is None


# ---------------------------------------------------------------------------
# What the responses say about the service
# ---------------------------------------------------------------------------
def test_the_version_is_picked_up_from_a_header():
    observed = compat.Observed()
    observed.note("queue", {compat.VERSION_HEADER: "5.14.0.9383"})
    assert observed.version == "5.14.0.9383"
    assert observed.major == 5
    assert observed.minor == 14


def test_a_replaced_call_is_remembered_and_counted():
    observed = compat.Observed()
    for _ in range(3):
        observed.note("queue?page=1", {compat.DEPRECATION_HEADER: "true"})
    assert observed.deprecated == {"queue": 3}, "the query string is not part of it"


def test_an_ordinary_response_leaves_no_warning_behind():
    observed = compat.Observed()
    observed.note("queue", {compat.VERSION_HEADER: "5.0.0.1"})
    assert observed.deprecated == {}


def test_an_unknown_version_counts_as_too_old():
    # The cautious direction: anything guarded by a version check is skipped
    # rather than attempted against a service that may not support it.
    assert compat.Observed().at_least(4) is False
    assert compat.Observed(version="5.14.0.1").at_least(4) is True
    assert compat.Observed(version="4.0.0.1").at_least(5) is False
    assert compat.Observed(version="4.7.0.1").at_least(4, 9) is False
    assert compat.Observed(version="4.9.0.1").at_least(4, 9) is True


def test_a_version_that_makes_no_sense_does_not_throw():
    observed = compat.Observed(version="not a version")
    assert observed.major == 0
    assert observed.at_least(1) is False
