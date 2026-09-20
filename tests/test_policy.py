"""What a rule is allowed to do, and when it is allowed to do it.

These tests all push in the same direction: a policy that cannot be understood,
or a condition that cannot be evaluated, must end in doing *less*, never more.
Getting this wrong deletes somebody's files.
"""
import pytest

from app import policy
from app.rules import ALL
from app.rules import Finding


def finding(**data):
    return Finding(rule="leftover_files", severity="info", title="x",
                   message="finding.leftover_files", data=data)


ALLOWED = (policy.REPORT, "delete")


# ---------------------------------------------------------------------------
# Reading a stored policy
# ---------------------------------------------------------------------------
def test_nothing_stored_means_the_rules_own_default():
    assert policy.parse({}, ALLOWED, "delete").action == "delete"


def test_an_action_the_rule_cannot_do_falls_back_to_report():
    assert policy.parse({"action": "blocklist"}, ALLOWED, "delete").action == "report"


def test_a_default_the_rule_cannot_do_falls_back_to_report():
    assert policy.parse({}, (policy.REPORT,), "delete").action == "report"


def test_rubbish_instead_of_a_policy_falls_back_to_the_default():
    for rubbish in (None, "delete", 7, [], True):
        assert policy.parse(rubbish, ALLOWED, "delete").action == "delete"


def test_report_is_kept_when_it_was_chosen_explicitly():
    assert policy.parse({"action": "report"}, ALLOWED, "delete").action == "report"


def test_a_condition_the_rule_does_not_have_is_dropped():
    # It could never be met, so keeping it would silently disable the rule for
    # a reason nobody chose.
    chosen = policy.parse({"action": "delete", "min_confidence": 0.9},
                          ALLOWED, "delete", conditions=("max_gb",))
    assert chosen.min_confidence == 0.0


@pytest.mark.parametrize("value", ["", None, "soon", [], {}])
def test_an_unreadable_condition_becomes_no_condition(value):
    chosen = policy.parse({"action": "delete", "min_age_hours": value},
                          ALLOWED, "delete")
    assert chosen.min_age_hours == 0.0


def test_conditions_are_clamped_into_their_range():
    chosen = policy.parse({"action": "delete", "min_age_hours": -5,
                           "max_gb": 10 ** 9, "min_confidence": 4},
                          ALLOWED, "delete")
    assert chosen.min_age_hours == 0.0
    assert chosen.max_gb == policy.LIMITS["max_gb"][1]
    assert chosen.min_confidence == 1.0


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------
def test_report_never_acts():
    verdict = policy.decide(policy.Policy(), finding(gb=1))
    assert verdict.act is False
    assert verdict.reason == "policy.report_only"


def test_an_action_without_conditions_simply_acts():
    verdict = policy.decide(policy.Policy(action="delete"), finding())
    assert verdict.act is True
    assert verdict.action == "delete"


def test_too_young_is_held_back_with_the_numbers_in_the_reason():
    chosen = policy.Policy(action="delete", min_age_hours=6)
    verdict = policy.decide(chosen, finding(age_hours=2.5))
    assert verdict.act is False
    assert verdict.reason == "policy.too_young"
    assert verdict.params == {"age": "2.5", "needed": "6"}


def test_old_enough_acts():
    chosen = policy.Policy(action="delete", min_age_hours=6)
    assert policy.decide(chosen, finding(age_hours=6)).act is True


def test_minutes_are_understood_as_an_age():
    chosen = policy.Policy(action="delete", min_age_hours=1)
    assert policy.decide(chosen, finding(minutes=90)).act is True
    assert policy.decide(chosen, finding(minutes=30)).act is False


def test_an_unanswerable_age_holds_back_rather_than_acting():
    chosen = policy.Policy(action="delete", min_age_hours=6)
    verdict = policy.decide(chosen, finding(gb=1))
    assert verdict.act is False
    assert verdict.reason == "policy.age_unknown"


def test_too_large_is_held_back():
    chosen = policy.Policy(action="delete", max_gb=10)
    verdict = policy.decide(chosen, finding(gb=62.5))
    assert verdict.act is False
    assert verdict.reason == "policy.too_large"
    assert verdict.params == {"size": "62.5", "limit": "10"}


def test_megabytes_are_understood_as_a_size():
    chosen = policy.Policy(action="delete", max_gb=1)
    assert policy.decide(chosen, finding(mb=512)).act is True
    assert policy.decide(chosen, finding(mb=4096)).act is False


def test_an_unanswerable_size_holds_back_rather_than_acting():
    chosen = policy.Policy(action="delete", max_gb=10)
    assert policy.decide(chosen, finding(age_hours=99)).reason == "policy.size_unknown"


def test_not_confident_enough_is_held_back():
    chosen = policy.Policy(action="delete", min_confidence=0.9)
    verdict = policy.decide(chosen, finding(confidence=0.55))
    assert verdict.act is False
    assert verdict.reason == "policy.not_confident_enough"
    assert verdict.params == {"confidence": "55%", "needed": "90%"}


def test_confident_enough_acts():
    chosen = policy.Policy(action="delete", min_confidence=0.9)
    assert policy.decide(chosen, finding(confidence=0.92)).act is True


def test_every_condition_has_to_hold_not_just_one():
    chosen = policy.Policy(action="delete", min_age_hours=6, max_gb=10)
    assert policy.decide(chosen, finding(age_hours=99, gb=40)).act is False
    assert policy.decide(chosen, finding(age_hours=1, gb=1)).act is False
    assert policy.decide(chosen, finding(age_hours=99, gb=1)).act is True


def test_a_held_back_verdict_still_names_the_action_it_would_have_taken():
    chosen = policy.Policy(action="delete", max_gb=1)
    assert policy.decide(chosen, finding(gb=9)).action == "delete"


# ---------------------------------------------------------------------------
# Checking what the interface sends
# ---------------------------------------------------------------------------
def test_a_submitted_action_has_to_be_one_the_rule_offers():
    with pytest.raises(ValueError, match="action_not_allowed"):
        policy.validate({"action": "blocklist"}, ALLOWED)


def test_a_submitted_condition_has_to_be_one_the_rule_has():
    with pytest.raises(ValueError, match="condition_not_allowed"):
        policy.validate({"action": "delete", "min_confidence": 0.5},
                        ALLOWED, ("max_gb",))


def test_a_submitted_condition_has_to_be_a_number():
    with pytest.raises(ValueError, match="not_a_number"):
        policy.validate({"action": "delete", "max_gb": "lots"}, ALLOWED)


def test_a_submitted_condition_has_to_be_in_range():
    with pytest.raises(ValueError, match="out_of_range"):
        policy.validate({"action": "delete", "max_gb": -1}, ALLOWED)


def test_a_valid_policy_comes_back_cleaned():
    assert policy.validate({"action": "delete", "max_gb": "12"}, ALLOWED) == {
        "action": "delete", "max_gb": 12.0}


# ---------------------------------------------------------------------------
# Upgrading from the previous switch
# ---------------------------------------------------------------------------
def test_a_rule_that_was_fixing_keeps_fixing():
    assert policy.from_legacy_switch(True, "delete") == {"action": "delete"}


def test_a_rule_that_was_only_reporting_keeps_reporting():
    assert policy.from_legacy_switch(False, "delete") == {"action": "report"}


# ---------------------------------------------------------------------------
# The rules themselves
# ---------------------------------------------------------------------------
def test_every_rule_can_always_be_set_to_report_only():
    for rule in ALL:
        assert policy.REPORT in rule.actions, rule.name


def test_every_rule_offers_only_known_actions_and_conditions():
    for rule in ALL:
        assert set(rule.actions) <= set(policy.ACTIONS), rule.name
        assert set(rule.conditions) <= set(policy.CONDITIONS), rule.name


def test_every_default_is_an_action_the_rule_offers():
    for rule in ALL:
        assert rule.default_action in rule.actions, rule.name


def test_a_rule_with_nothing_but_report_does_not_claim_to_act():
    for rule in ALL:
        if rule.actions == (policy.REPORT,):
            assert rule.modifies is False, rule.name
            assert rule.deletes is False, rule.name


def test_a_rule_that_deletes_says_so():
    deleting = {r.name for r in ALL if r.deletes}
    assert deleting == {"unpack_failed", "leftover_files",
                        "downloader_stale_entry"}


def test_no_rule_deletes_without_being_able_to_wait_first():
    # Anything irreversible has to be able to carry a "wait this long" or a
    # size limit, otherwise there is no way to make it cautious.
    for rule in ALL:
        if rule.deletes:
            assert rule.conditions, rule.name


def test_an_engine_handler_exists_for_every_action_a_rule_offers():
    from app.engine import Engine
    for rule in ALL:
        for action in rule.actions:
            if action == policy.REPORT:
                continue
            assert hasattr(Engine, "_act_" + action), f"{rule.name}: {action}"
