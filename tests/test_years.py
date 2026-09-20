"""Reading the year out of a release name, and deciding whether it is wrong.

The cases here are real ones. Every false positive listed below would mean a
good release blocklisted and searched for again — and grabbed, rejected and
searched for again after that, because the replacement has the same problem.
"""
import pytest

from app import years


# ---------------------------------------------------------------------------
# Which numbers in a name could be a release year
# ---------------------------------------------------------------------------
def test_an_ordinary_release_name_yields_its_year():
    assert years.candidates("The.Witch.2015.1080p.BluRay.x264-GROUP",
                            "The Witch") == [2015]


def test_a_title_that_is_a_year_does_not_supply_its_own_release_year():
    # Nothing in this name states when it was released, so there is nothing to
    # disagree with. Reading 1917 as the release year and comparing it to the
    # film's 2019 was the single worst false positive in this area.
    assert years.candidates("1917.German.DL.1080p.BluRay.x264-GROUP", "1917") == []


def test_a_title_that_is_a_year_still_yields_a_real_release_year():
    assert years.candidates("1917.2019.2160p.UHD.BluRay.x265-GROUP", "1917") == [2019]


def test_a_number_in_the_title_is_only_discounted_as_often_as_it_appears():
    assert years.candidates("Blade.Runner.2049.2017.UHD.BluRay-GROUP",
                            "Blade Runner 2049") == [2017]
    assert years.candidates("2012.2009.1080p.BluRay.x264-GROUP", "2012") == [2009]
    assert years.candidates("2001.A.Space.Odyssey.1968.1080p-GROUP",
                            "2001: A Space Odyssey") == [1968]


def test_a_spelled_out_resolution_is_not_a_year():
    # 1920 sits squarely inside any sensible year window.
    assert years.candidates("Some.Film.2018.1920x1080.x264-GROUP", "Some Film") == [2018]


def test_numbers_that_are_not_four_digits_are_ignored():
    assert years.candidates("Some.Film.2018.x264.DD5.1.720p-GROUP", "Some Film") == [2018]


def test_a_longer_run_of_digits_is_not_a_year():
    assert years.candidates("Some.Film.20189.1080p", "Some Film") == []


def test_a_number_outside_the_window_is_not_a_year():
    assert years.candidates("Some.Film.1234.1080p", "Some Film") == []


def test_a_release_with_two_real_years_keeps_both():
    assert years.candidates("Blade.Runner.1982.The.Final.Cut.2007.UHD",
                            "Blade Runner") == [1982, 2007]


def test_nothing_in_nothing_out():
    assert years.candidates("", "") == []
    assert years.candidates(None, None) == []


# ---------------------------------------------------------------------------
# Which years legitimately belong to an item
# ---------------------------------------------------------------------------
def test_the_filed_year_is_always_accepted():
    assert 2019 in years.accepted({"year": 2019})


def test_the_premiere_year_is_accepted():
    # The service keeps this exactly for the festival case, and matching
    # against it is what the service does itself.
    fits = years.accepted({"year": 2016, "secondaryYear": 2015}, tolerance=0)
    assert fits == {2015, 2016}


def test_the_release_dates_the_service_holds_are_accepted():
    fits = years.accepted({"year": 2019, "inCinemas": "2019-12-25T00:00:00Z",
                           "physicalRelease": "2020-03-24T00:00:00Z"}, tolerance=0)
    assert fits == {2019, 2020}


def test_a_window_is_added_unless_it_is_switched_off():
    assert years.accepted({"year": 2019}, tolerance=1) == {2018, 2019, 2020}
    assert years.accepted({"year": 2019}, tolerance=0) == {2019}


def test_an_unreadable_date_is_simply_skipped():
    fits = years.accepted({"year": 2019, "digitalRelease": "", "inCinemas": None,
                           "physicalRelease": "not a date"}, tolerance=0)
    assert fits == {2019}


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------
def test_a_matching_year_is_not_questioned():
    assert years.judge("The.Film.2019.1080p", {"year": 2019, "title": "The Film"}) \
        .wrong is False


def test_a_missing_year_on_the_item_is_not_judged():
    assert years.judge("The.Film.2019.1080p", {"title": "The Film"}).wrong is False


def test_a_missing_year_in_the_name_is_not_judged():
    assert years.judge("The.Film.German.DL.1080p",
                       {"year": 2019, "title": "The Film"}).wrong is False


def test_a_genuinely_different_film_is_questioned():
    verdict = years.judge("The.Film.1998.1080p", {"year": 2019, "title": "The Film"})
    assert verdict.wrong is True
    assert verdict.found == (1998,)
    assert verdict.expected == 2019
    assert verdict.distance == 20
    assert verdict.confidence == 1.0


def test_a_near_miss_is_questioned_but_not_with_confidence():
    verdict = years.judge("The.Film.2016.1080p", {"year": 2019, "title": "The Film"})
    assert verdict.wrong is True
    assert verdict.distance == 2       # 2018 is already accepted by the window
    assert verdict.confidence == 0.1   # barely worth acting on unasked


@pytest.mark.parametrize("name,item", [
    # Festival in one year, cinemas in the next. The service holds the
    # premiere year itself, so nothing here needs a guess.
    ("The.Witch.2015.1080p.BluRay-GROUP",
     {"year": 2016, "secondaryYear": 2015, "title": "The Witch"}),
    ("It.Follows.2014.1080p.BluRay-GROUP",
     {"year": 2015, "secondaryYear": 2014, "title": "It Follows"}),
    ("Nomadland.2020.2160p.WEB-GROUP",
     {"year": 2021, "secondaryYear": 2020, "title": "Nomadland"}),
    # A limited run in late December, wide release and disc the year after.
    ("1917.2020.1080p.BluRay-GROUP",
     {"year": 2019, "title": "1917", "physicalRelease": "2020-03-24"}),
    ("Hidden.Figures.2017.1080p.BluRay-GROUP",
     {"year": 2016, "title": "Hidden Figures", "physicalRelease": "2017-04-11"}),
    # Released at home a year before it reached the West.
    ("Your.Name.2017.1080p.BluRay-GROUP",
     {"year": 2016, "title": "Your Name", "digitalRelease": "2017-04-07"}),
    # And the plain one-year drift the window is there for.
    ("Some.Film.2018.1080p-GROUP", {"year": 2019, "title": "Some Film"}),
])
def test_legitimate_year_differences_are_left_alone(name, item):
    assert years.judge(name, item).wrong is False, name


@pytest.mark.parametrize("name,item", [
    ("1917.German.DL.1080p.BluRay.x264-GROUP", {"year": 2019, "title": "1917"}),
    ("2012.German.DL.1080p.BluRay-GROUP", {"year": 2009, "title": "2012"}),
    ("Blade.Runner.2049.German.DL.2160p-GROUP",
     {"year": 2017, "title": "Blade Runner 2049"}),
    ("Some.Film.1920x1080.x264-GROUP", {"year": 2020, "title": "Some Film"}),
])
def test_a_number_in_the_title_never_becomes_an_accusation(name, item):
    assert years.judge(name, item).wrong is False, name


def test_a_re_release_named_after_its_own_cut_is_not_questioned():
    # The disc carries the year of the cut, which the service holds as a
    # release date on the same entry.
    verdict = years.judge("Blade.Runner.1982.The.Final.Cut.2007.UHD-GROUP",
                          {"year": 1982, "title": "Blade Runner"})
    assert verdict.wrong is False


def test_the_message_names_the_nearest_miss_not_the_first_number():
    verdict = years.judge("The.Film.1998.2005.1080p",
                          {"year": 2019, "title": "The Film"})
    assert verdict.distance == 13, "2005 is nearer than 1998"


# ---------------------------------------------------------------------------
# A series is not "from" a year — it runs
# ---------------------------------------------------------------------------
def _running(**over):
    base = {"title": "The Series", "year": 2015, "firstAired": "2015-03-04",
            "seasons": [], "ended": False}
    base.update(over)
    return base


def test_a_current_episode_of_a_running_series_is_not_a_different_programme():
    """Daily programmes are named by date outright.

    ``Show.Name.2024.03.04.1080p.WEB`` held against the year the series is
    filed under reads as a different programme — and the rule that compares
    them blocklists and searches again by default, so a show running longer
    than a year had its episodes thrown away as fast as they arrived.
    """
    from datetime import UTC, datetime
    this_year = datetime.now(UTC).year
    name = f"The.Series.{this_year}.03.04.1080p.WEB"
    assert years.judge(name, _running()).wrong is False


def test_a_season_pack_from_a_later_year_is_accepted():
    assert years.judge("The.Series.S05.2020.1080p", _running()).wrong is False


def test_a_series_that_ended_does_not_accept_a_later_year():
    ended = _running(ended=True, lastAired="2018-05-05")
    assert years.judge("The.Series.S09E01.2021.1080p", ended).wrong is True


def test_a_series_that_ended_without_a_last_date_keeps_the_benefit_of_the_doubt():
    """Narrowing the span on a guess is how the problem started."""
    ended = _running(ended=True)
    assert years.judge("The.Series.S09E01.2021.1080p", ended).wrong is False


def test_a_film_is_still_judged_the_way_it_always_was():
    film = {"title": "The Film", "year": 2018}
    assert years.judge("The.Film.1999.1080p", film).wrong is True
    assert years.judge("The.Film.2018.1080p", film).wrong is False
