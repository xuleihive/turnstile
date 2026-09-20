"""Attribution read off a subscription, checked against the shapes a real install actually has.

The names below are the shapes observed on the install this was built for -- 275 APIM
subscriptions, of which 105 carry an `ownerId`, 73 have an email somewhere in the display name,
and 167 have neither. The 52 people holding two keys each are all in that last group, which is
why `person_group` has to work without an email and `derive_owner` has to be willing to return
nothing.
"""

from __future__ import annotations

from turnstile_core.domain.application_attribution import (
    OwnerDerivation,
    department_for_owner,
    derive_owner,
    person_group,
)


class TestPersonGroup:
    def test_a_usage_suffix_does_not_make_a_second_person(self) -> None:
        assert person_group("Cecilia Ying") == person_group("Cecilia Ying - Databricks")
        assert person_group("a-zagainov-IT") == person_group("a-zagainov-IT - Databricks")
        assert person_group("dongyuli-IT") == person_group("dongyuli-IT - Databricks")

    def test_case_differences_are_the_same_person(self) -> None:
        assert person_group("David-IT") == person_group("david-IT - Databricks")

    def test_an_email_groups_by_the_email(self) -> None:
        assert person_group("Subscription for e.kirilin@insilicomedicine.com") == (
            "e.kirilin@insilicomedicine.com"
        )
        assert person_group("a.aiginin@insilicomedicine.com - IT Databricks Claude API") == (
            "a.aiginin@insilicomedicine.com"
        )

    def test_different_suffixes_stay_different_people(self) -> None:
        assert person_group("a-zagainov-IT") != person_group("a-zagainov-Finance")

    def test_a_name_that_is_only_a_usage_marker_groups_nothing(self) -> None:
        assert person_group("Subscription for ") is None


class TestDeriveOwner:
    def test_apim_owner_wins_over_the_name(self) -> None:
        result = derive_owner(
            "Subscription for e.kirilin - Foundry",
            apim_owner_email="e.kirilin@insilicomedicine.com",
        )
        assert result == OwnerDerivation(
            owner_id="e.kirilin@insilicomedicine.com",
            owner_source="apim",
            person_group="e.kirilin@insilicomedicine.com",
        )

    def test_both_keys_of_one_apim_owner_reach_the_same_person(self) -> None:
        first = derive_owner(
            "Subscription for m.malkov@insilicomedicine.com",
            apim_owner_email="m.malkov@insilicomedicine.com",
        )
        second = derive_owner(
            "Subscription for m.malkov - Foundry",
            apim_owner_email="m.malkov@insilicomedicine.com",
        )
        assert first.owner_id == second.owner_id
        assert first.person_group == second.person_group

    def test_an_email_in_the_name_is_taken_but_marked_as_derived(self) -> None:
        result = derive_owner("a.aiginin@insilicomedicine.com - IT Databricks Claude API")
        assert result.owner_id == "a.aiginin@insilicomedicine.com"
        assert result.owner_source == "derived"

    def test_a_name_without_an_email_yields_no_owner_but_still_groups(self) -> None:
        result = derive_owner("dongyuli-IT - Databricks")
        assert result.owner_id is None
        assert result.owner_source is None
        assert result.person_group == "dongyuli-it"

    def test_no_address_is_invented_for_a_nameless_holder(self) -> None:
        for name in ("AIClaw", "APIM Content Monitor (internal)", "Alex-Z"):
            assert derive_owner(name).owner_id is None

    def test_an_apim_owner_without_an_at_is_not_an_owner(self) -> None:
        result = derive_owner("dongyuli-IT", apim_owner_email="user-dongyuli-it")
        assert result.owner_id is None


class TestDepartmentForOwner:
    def test_a_second_key_inherits_the_department_of_the_first(self) -> None:
        existing = [{"owner_id": None, "person_group": "dongyuli-it",
                     "department_id": "department-bioinfo"}]
        assert department_for_owner(None, "dongyuli-it", existing) == "department-bioinfo"

    def test_the_owner_email_matches_across_differently_named_keys(self) -> None:
        existing = [{"owner_id": "m.malkov@insilicomedicine.com", "person_group": "x",
                     "department_id": "department-platform"}]
        assert department_for_owner(
            "M.Malkov@insilicomedicine.com", "other", existing
        ) == "department-platform"

    def test_disagreement_leaves_it_blank_rather_than_picking_one(self) -> None:
        existing = [
            {"owner_id": None, "person_group": "dongyuli-it",
             "department_id": "department-bioinfo"},
            {"owner_id": None, "person_group": "dongyuli-it",
             "department_id": "department-platform"},
        ]
        assert department_for_owner(None, "dongyuli-it", existing) is None

    def test_an_unassigned_existing_key_carries_nothing(self) -> None:
        existing = [{"owner_id": None, "person_group": "dongyuli-it", "department_id": None}]
        assert department_for_owner(None, "dongyuli-it", existing) is None

    def test_a_stranger_inherits_nothing(self) -> None:
        existing = [{"owner_id": None, "person_group": "someone-else",
                     "department_id": "department-bioinfo"}]
        assert department_for_owner(None, "dongyuli-it", existing) is None
