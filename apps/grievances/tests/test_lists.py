"""List, filter, metadata, summary and timeline tests.

The theme running through these: filters narrow what the access policy already
allowed. A filter must never be able to widen it.
"""
from __future__ import annotations

import pytest

from apps.grievances import enums
from apps.grievances.references import next_reference
from apps.grievances.tests.factories import make_complaint, make_employee, make_org
from apps.grievances.tests.test_intake import as_user

pytestmark = pytest.mark.django_db

URL = "/api/v1/complaints/"


@pytest.fixture
def world():
    """A tenant with a spread of complaints across states and visibilities."""
    org = make_org()
    alice = make_employee(org, "Alice")
    bob = make_employee(org, "Bob", manager=alice)
    carol = make_employee(org, "Carol", manager=alice)
    hr = make_employee(org, "HR User", is_hr=True)

    filed_by_bob = make_complaint(
        org, complainant=bob, respondent=carol, visibility=enums.Visibility.HR
    )
    against_bob = make_complaint(
        org, complainant=carol, respondent=bob, visibility=enums.Visibility.HR,
        state=enums.ComplaintState.UNDER_INVESTIGATION,
    )
    hr_filed = make_complaint(
        org, complainant=bob, respondent=carol, visibility=enums.Visibility.HR,
        state=enums.ComplaintState.RESOLVED, source=enums.ComplaintSource.HR_FOR_EMPLOYEE,
        filed_by=hr,
    )
    lm_only = make_complaint(
        org, complainant=bob, respondent=carol,
        visibility=enums.Visibility.LINE_MANAGER,
    )
    return {
        "org": org, "alice": alice, "bob": bob, "carol": carol, "hr": hr,
        "filed_by_bob": filed_by_bob, "against_bob": against_bob,
        "hr_filed": hr_filed, "lm_only": lm_only,
    }


def ids(response) -> set[str]:
    return {row["id"] for row in response.data["results"]}


# ---------------------------------------------------------------------------
# Employee tabs
# ---------------------------------------------------------------------------

def test_reported_by_me_tab(world):
    response = as_user(world["bob"]).get(URL, {"relation": "reported_by_me"})
    assert response.status_code == 200
    returned = ids(response)
    assert str(world["filed_by_bob"].pk) in returned
    assert str(world["against_bob"].pk) not in returned


def test_against_me_tab(world):
    response = as_user(world["bob"]).get(URL, {"relation": "against_me"})
    assert response.status_code == 200
    assert ids(response) == {str(world["against_bob"].pk)}


def test_against_me_excludes_complaints_not_yet_under_investigation(world):
    """A SUBMITTED complaint against you is still invisible -- spec v4 A1."""
    submitted = make_complaint(
        world["org"], complainant=world["carol"], respondent=world["bob"],
        visibility=enums.Visibility.HR, state=enums.ComplaintState.SUBMITTED,
    )
    response = as_user(world["bob"]).get(URL, {"relation": "against_me"})
    assert str(submitted.pk) not in ids(response)


# ---------------------------------------------------------------------------
# HR console tabs
# ---------------------------------------------------------------------------

def test_by_employees_and_by_hr_tabs(world):
    client = as_user(world["hr"])

    by_employees = client.get(URL, {"source_tab": "employee"})
    assert str(world["filed_by_bob"].pk) in ids(by_employees)
    assert str(world["hr_filed"].pk) not in ids(by_employees)

    by_hr = client.get(URL, {"source_tab": "hr"})
    assert ids(by_hr) == {str(world["hr_filed"].pk)}


def test_hr_lists_never_include_line_manager_only_complaints(world):
    """The headline visibility rule, enforced through the list endpoint."""
    response = as_user(world["hr"]).get(URL)
    assert str(world["lm_only"].pk) not in ids(response)


def test_line_manager_sees_their_reports_complaints(world):
    response = as_user(world["alice"]).get(URL)
    assert str(world["lm_only"].pk) in ids(response)


# ---------------------------------------------------------------------------
# Derived-field filters
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status,expected_key",
    [("Pending", "filed_by_bob"), ("Open", "against_bob"), ("Closed", "hr_filed")],
)
def test_status_filter_maps_back_to_states(world, status, expected_key):
    response = as_user(world["hr"]).get(URL, {"status": status})
    assert response.status_code == 200
    assert str(world[expected_key].pk) in ids(response)


def test_stage_filter(world):
    response = as_user(world["hr"]).get(URL, {"stage": "Investigation"})
    assert ids(response) == {str(world["against_bob"].pk)}


def test_unknown_status_returns_nothing_rather_than_everything(world):
    """Fail closed. An unrecognised filter must not silently disable itself."""
    response = as_user(world["hr"]).get(URL, {"status": "Nonsense"})
    assert response.status_code == 200
    assert response.data["count"] == 0


def test_type_and_reported_to_filters(world):
    client = as_user(world["hr"])
    assert client.get(URL, {"type": enums.ComplaintType.THEFT}).data["count"] >= 1
    assert client.get(URL, {"type": enums.ComplaintType.SEXUAL_ASSAULT}).data["count"] == 0
    assert client.get(URL, {"reported_to": enums.Visibility.HR}).data["count"] == 3


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def test_search_matches_reference_and_names(world):
    client = as_user(world["hr"])
    by_ref = client.get(URL, {"q": world["filed_by_bob"].reference})
    assert ids(by_ref) == {str(world["filed_by_bob"].pk)}

    by_name = client.get(URL, {"q": "Carol"})
    assert by_name.data["count"] >= 1


def test_search_does_not_match_complaint_descriptions(world):
    """Descriptions hold the substance of an allegation.

    Searching them would let someone probe for content in cases they can only
    partly see.
    """
    world["filed_by_bob"].description = "zzzsecretzzz"
    world["filed_by_bob"].save(update_fields=["description"])
    response = as_user(world["hr"]).get(URL, {"q": "zzzsecretzzz"})
    assert response.data["count"] == 0


def test_filters_cannot_widen_access(world):
    """Carol filters for everything; she still only sees her own cases."""
    response = as_user(world["carol"]).get(URL, {"reported_to": enums.Visibility.HR})
    assert str(world["lm_only"].pk) not in ids(response)
    for row in response.data["results"]:
        assert row["id"] in {
            str(world["filed_by_bob"].pk),
            str(world["against_bob"].pk),
            str(world["hr_filed"].pk),
        }


# ---------------------------------------------------------------------------
# Metadata and summary
# ---------------------------------------------------------------------------

def test_metadata_returns_every_option_set(world):
    response = as_user(world["bob"]).get(f"{URL}metadata/")
    assert response.status_code == 200

    for key in (
        "complaint_types", "visibilities", "investigation_decisions",
        "resolution_types", "formal_resolution_types",
        "informal_resolution_types", "statuses", "stages", "field_rules",
    ):
        assert key in response.data, key

    types = {c["value"] for c in response.data["complaint_types"]}
    assert enums.ComplaintType.OTHERS in types
    assert response.data["field_rules"]["occurrence_count"]["required_when"] == {
        "frequency": [enums.Frequency.REPEAT_BEHAVIOR]
    }


def test_summary_counts_agree_with_the_lists_they_label(world):
    client = as_user(world["hr"])
    summary = client.get(f"{URL}summary/")
    assert summary.status_code == 200

    assert summary.data["total"] == client.get(URL).data["count"]
    assert summary.data["tabs"]["by_hr"] == client.get(
        URL, {"source_tab": "hr"}
    ).data["count"]
    assert summary.data["by_status"]["Closed"] == client.get(
        URL, {"status": "Closed"}
    ).data["count"]


def test_summary_is_scoped_to_the_caller(world):
    """Each person's totals cover their own visible set, nobody else's.

    Worth reading carefully, because the numbers are counter-intuitive: Bob
    sees *more* than HR here. He is involved in all four complaints, while HR
    is excluded from the LINE_MANAGER one. "HR sees everything" is exactly the
    assumption the three-way visibility rule breaks.
    """
    hr_client, bob_client = as_user(world["hr"]), as_user(world["bob"])

    hr_summary = hr_client.get(f"{URL}summary/").data
    bob_summary = bob_client.get(f"{URL}summary/").data

    # Each total matches that person's own list.
    assert hr_summary["total"] == hr_client.get(URL).data["count"]
    assert bob_summary["total"] == bob_client.get(URL).data["count"]

    # HR is excluded from the LINE_MANAGER complaint; Bob, as complainant, is not.
    assert str(world["lm_only"].pk) not in ids(hr_client.get(URL))
    assert str(world["lm_only"].pk) in ids(bob_client.get(URL))
    assert bob_summary["total"] == hr_summary["total"] + 1

    # Carol is party to fewer cases than either.
    carol_summary = as_user(world["carol"]).get(f"{URL}summary/").data
    assert carol_summary["total"] < bob_summary["total"]


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------

def test_timeline_is_available_with_full_access(world):
    response = as_user(world["hr"]).get(f"{URL}{world['against_bob'].pk}/timeline/")
    assert response.status_code == 200
    assert isinstance(response.data, list)


def test_timeline_is_refused_to_the_respondent(world):
    """They can see the allegation; the case history is a different matter."""
    response = as_user(world["bob"]).get(f"{URL}{world['against_bob'].pk}/timeline/")
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Directory lookups
# ---------------------------------------------------------------------------

def test_employee_lookup_is_tenant_scoped(world):
    outsider = make_employee(make_org("Other"), "Outsider")
    response = as_user(world["bob"]).get("/api/v1/employees/", {"q": "Outsider"})
    assert response.status_code == 200
    assert response.data["count"] == 0

    found = as_user(world["bob"]).get("/api/v1/employees/", {"q": "Carol"})
    assert found.data["count"] == 1


def test_employee_lookup_can_exclude_the_caller(world):
    response = as_user(world["bob"]).get("/api/v1/employees/", {"exclude_self": "true"})
    returned = {row["id"] for row in response.data["results"]}
    assert str(world["bob"].pk) not in returned
