"""Intake tests: all four filing routes and the validation rule table.

The validation rules are conditional -- which fields are required depends on
source, subject_type and frequency -- so most of these assert on the error
*key* as well as the rejection, because the frontend attaches errors to named
form fields.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from rest_framework.test import APIClient

from apps.grievances import enums
from apps.grievances.models import Complaint, ComplaintEvent
from apps.grievances.references import next_reference
from apps.grievances.tests.factories import make_employee, make_org

pytestmark = pytest.mark.django_db

URL = "/api/v1/complaints/"


def as_user(employee) -> APIClient:
    """Authenticated client for an employee.

    Idempotent -- a test may call this more than once for the same person.
    """
    username = f"u{employee.pk.hex[:12]}"
    user = User.objects.filter(username=username).first()
    if user is None:
        user = User.objects.create_user(username=username, password="x")  # noqa: S106
        employee.user = user
        employee.save(update_fields=["user"])
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def cast():
    org = make_org()
    alice = make_employee(org, "Alice")
    bob = make_employee(org, "Bob", manager=alice)
    carol = make_employee(org, "Carol", manager=alice)
    hr = make_employee(org, "HR User", is_hr=True)
    return {"org": org, "alice": alice, "bob": bob, "carol": carol, "hr": hr}


def general_payload(**overrides):
    payload = {
        "source": enums.ComplaintSource.SELF,
        "subject_type": enums.SubjectType.GENERAL,
        "complaint_type": enums.ComplaintType.UNSUSTAINABLE_WORKLOAD,
        "description": "Consistently over capacity.",
        "visibility": enums.Visibility.HR,
    }
    payload.update(overrides)
    return payload


def employee_payload(respondent, **overrides):
    payload = {
        "source": enums.ComplaintSource.SELF,
        "subject_type": enums.SubjectType.EMPLOYEE,
        "complaint_type": enums.ComplaintType.THEFT,
        "description": "Property went missing.",
        "visibility": enums.Visibility.HR,
        "respondent": str(respondent.pk),
        "incident_date": "2026-08-01",
        "frequency": enums.Frequency.ONE_TIME,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# The four routes
# ---------------------------------------------------------------------------

def test_employee_files_a_general_complaint(cast):
    response = as_user(cast["bob"]).post(URL, general_payload(), format="json")
    assert response.status_code == 201, response.data

    complaint = Complaint.objects.get(pk=response.data["id"])
    assert complaint.source == enums.ComplaintSource.SELF
    assert complaint.complainant_id == cast["bob"].pk
    assert complaint.filed_by_id == cast["bob"].pk
    assert complaint.respondent_id is None
    assert complaint.state == enums.ComplaintState.SUBMITTED
    assert complaint.reference.startswith("CMP-")


def test_employee_files_about_a_colleague(cast):
    response = as_user(cast["bob"]).post(
        URL, employee_payload(cast["carol"]), format="json"
    )
    assert response.status_code == 201, response.data
    complaint = Complaint.objects.get(pk=response.data["id"])
    assert complaint.respondent_id == cast["carol"].pk


def test_hr_files_on_behalf_of_an_employee(cast):
    payload = employee_payload(
        cast["carol"],
        source=enums.ComplaintSource.HR_FOR_EMPLOYEE,
        complainant=str(cast["bob"].pk),
    )
    response = as_user(cast["hr"]).post(URL, payload, format="json")
    assert response.status_code == 201, response.data

    complaint = Complaint.objects.get(pk=response.data["id"])
    assert complaint.complainant_id == cast["bob"].pk
    assert complaint.filed_by_id == cast["hr"].pk


def test_hr_files_on_behalf_of_the_company(cast):
    payload = employee_payload(
        cast["carol"], source=enums.ComplaintSource.HR_FOR_COMPANY
    )
    response = as_user(cast["hr"]).post(URL, payload, format="json")
    assert response.status_code == 201, response.data

    complaint = Complaint.objects.get(pk=response.data["id"])
    assert complaint.complainant_id is None
    assert complaint.filed_by_id == cast["hr"].pk


def test_non_hr_cannot_file_on_behalf_of_others(cast):
    payload = employee_payload(
        cast["carol"],
        source=enums.ComplaintSource.HR_FOR_EMPLOYEE,
        complainant=str(cast["alice"].pk),
    )
    response = as_user(cast["bob"]).post(URL, payload, format="json")
    assert response.status_code == 400
    assert "source" in response.data


# ---------------------------------------------------------------------------
# The validation rule table
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "missing_field",
    ["respondent", "incident_date", "frequency"],
)
def test_employee_complaint_requires_its_extra_fields(cast, missing_field):
    payload = employee_payload(cast["carol"])
    payload[missing_field] = "" if missing_field == "frequency" else None
    expected_field = missing_field
    response = as_user(cast["bob"]).post(URL, payload, format="json")
    assert response.status_code == 400
    assert expected_field in response.data, response.data


def test_repeat_behaviour_requires_a_count(cast):
    payload = employee_payload(
        cast["carol"], frequency=enums.Frequency.REPEAT_BEHAVIOR
    )
    response = as_user(cast["bob"]).post(URL, payload, format="json")
    assert response.status_code == 400
    assert "occurrence_count" in response.data


def test_general_complaint_rejects_a_respondent(cast):
    payload = general_payload(respondent=str(cast["carol"].pk))
    response = as_user(cast["bob"]).post(URL, payload, format="json")
    assert response.status_code == 400
    assert "respondent" in response.data


def test_others_requires_a_note(cast):
    payload = general_payload(complaint_type=enums.ComplaintType.OTHERS)
    response = as_user(cast["bob"]).post(URL, payload, format="json")
    assert response.status_code == 400
    assert "complaint_type_note" in response.data

    payload["complaint_type_note"] = "Rota fairness"
    ok = as_user(cast["bob"]).post(URL, payload, format="json")
    assert ok.status_code == 201, ok.data


def test_cannot_file_against_yourself(cast):
    payload = employee_payload(cast["bob"])
    response = as_user(cast["bob"]).post(URL, payload, format="json")
    assert response.status_code == 400
    assert "respondent" in response.data


def test_future_incident_date_is_rejected(cast):
    payload = employee_payload(cast["carol"], incident_date="2099-01-01")
    response = as_user(cast["bob"]).post(URL, payload, format="json")
    assert response.status_code == 400
    assert "incident_date" in response.data


def test_cannot_name_someone_from_another_tenant(cast):
    """A valid UUID from another organisation must not resolve."""
    outsider = make_employee(make_org("Other"), "Outsider")
    payload = employee_payload(outsider)
    response = as_user(cast["bob"]).post(URL, payload, format="json")
    assert response.status_code == 400
    assert "respondent" in response.data


# ---------------------------------------------------------------------------
# Intake override -- spec v4 section 2
# ---------------------------------------------------------------------------

def test_complaint_about_your_manager_is_forced_to_hr(cast):
    """Bob reports to Alice and complains about Alice, choosing LINE_MANAGER.

    Sending that to Alice would route the complaint to the person it is about.
    """
    payload = employee_payload(
        cast["alice"], visibility=enums.Visibility.LINE_MANAGER
    )
    response = as_user(cast["bob"]).post(URL, payload, format="json")
    assert response.status_code == 201, response.data

    complaint = Complaint.objects.get(pk=response.data["id"])
    assert complaint.visibility == enums.Visibility.HR
    assert complaint.visibility_requested == enums.Visibility.LINE_MANAGER

    assert complaint.events.filter(
        verb=enums.EventVerb.VISIBILITY_OVERRIDDEN
    ).exists(), "the override must be recorded, not silent"


def test_ordinary_line_manager_complaint_is_not_overridden(cast):
    payload = employee_payload(
        cast["carol"], visibility=enums.Visibility.LINE_MANAGER
    )
    response = as_user(cast["bob"]).post(URL, payload, format="json")
    assert response.status_code == 201

    complaint = Complaint.objects.get(pk=response.data["id"])
    assert complaint.visibility == enums.Visibility.LINE_MANAGER
    assert not complaint.events.filter(
        verb=enums.EventVerb.VISIBILITY_OVERRIDDEN
    ).exists()


# ---------------------------------------------------------------------------
# Witnesses, audit trail, references
# ---------------------------------------------------------------------------

def test_witnesses_are_attached(cast):
    payload = general_payload(
        witnesses=[
            {"witness_type": enums.WitnessType.EMPLOYEE, "employee": str(cast["carol"].pk)},
            {"witness_type": enums.WitnessType.LINE_MANAGER},
        ]
    )
    response = as_user(cast["bob"]).post(URL, payload, format="json")
    assert response.status_code == 201, response.data
    assert Complaint.objects.get(pk=response.data["id"]).witnesses.count() == 2


def test_employee_witness_without_an_employee_is_rejected(cast):
    payload = general_payload(
        witnesses=[{"witness_type": enums.WitnessType.EMPLOYEE}]
    )
    response = as_user(cast["bob"]).post(URL, payload, format="json")
    assert response.status_code == 400


def test_filing_writes_an_audit_row(cast):
    response = as_user(cast["bob"]).post(URL, general_payload(), format="json")
    event = ComplaintEvent.objects.get(
        complaint_id=response.data["id"], verb=enums.EventVerb.FILED
    )
    assert event.actor_id == cast["bob"].pk
    assert event.to_state == enums.ComplaintState.SUBMITTED


def test_references_increment_per_tenant(cast):
    other_org = make_org("Other")
    assert next_reference(cast["org"]).endswith("-00001")
    assert next_reference(cast["org"]).endswith("-00002")
    # A second tenant numbers from 1 again, independently.
    assert next_reference(other_org).endswith("-00001")


def test_references_are_unique_under_repeated_allocation(cast):
    issued = {next_reference(cast["org"]) for _ in range(50)}
    assert len(issued) == 50


# ---------------------------------------------------------------------------
# Reading back what you filed
# ---------------------------------------------------------------------------

def test_respondent_detail_view_hides_the_complainant(cast):
    complaint = Complaint.objects.create(
        organisation=cast["org"],
        reference=next_reference(cast["org"]),
        source=enums.ComplaintSource.SELF,
        subject_type=enums.SubjectType.EMPLOYEE,
        filed_by=cast["bob"],
        complainant=cast["bob"],
        respondent=cast["carol"],
        complaint_type=enums.ComplaintType.THEFT,
        description="Something happened.",
        visibility=enums.Visibility.HR,
        visibility_requested=enums.Visibility.HR,
        state=enums.ComplaintState.UNDER_INVESTIGATION,
    )
    response = as_user(cast["carol"]).get(f"{URL}{complaint.pk}/")
    assert response.status_code == 200
    # Omitted entirely rather than returned as null -- a field that is absent
    # cannot be accidentally reintroduced by a later serializer change.
    for leaked in ("complainant", "witnesses", "attachments", "filed_by", "visibility"):
        assert leaked not in response.data, f"{leaked} leaked to the respondent"


def test_list_only_returns_what_you_may_see(cast):
    as_user(cast["bob"]).post(URL, general_payload(), format="json")
    response = as_user(cast["carol"]).get(URL)
    assert response.status_code == 200
    assert response.data["count"] == 0


def test_anonymous_access_is_refused(cast):
    assert APIClient().get(URL).status_code in (401, 403)
