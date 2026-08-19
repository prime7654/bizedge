"""MAKAY employee-app (`app_api`) tests.

The compatibility layer only translates, so these assert two things: that the
wire shape matches the frontend spec (category, lowercase enums, camelCase rows,
{data,total} envelope, 422 errors), and -- more importantly -- that translating
never loosens a domain guarantee (access policy, respondent masking, the intake
override, tenant isolation).

Roughly one test per handled mapping or failure mode. Break a guard in
serializers.py/views.py/mappings.py and one of these should go red; a guard with
no failing test is decoration.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from apps.directory.models import Department
from apps.grievances import enums
from apps.grievances.models import Complaint
from apps.grievances.tests.factories import make_complaint, make_employee, make_org

pytestmark = pytest.mark.django_db

BASE = "/api/v1/app"


def as_user(employee) -> APIClient:
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
    dave = make_employee(org, "Dave", manager=alice)
    hr = make_employee(org, "HR User", is_hr=True)
    return {
        "org": org, "alice": alice, "bob": bob,
        "carol": carol, "dave": dave, "hr": hr,
    }


def general_body(**overrides):
    body = {
        "category": "general",
        "complaint_type": "Theft",          # label round-trip, not the enum value
        "description": "Consistently over capacity.",
        "visibility": "hr",                 # lowercase token
    }
    body.update(overrides)
    return body


def employee_body(respondent, **overrides):
    body = {
        "category": "employee",
        "complaint_type": "Sexual harassment",
        "description": "Property went missing.",
        "visibility": "both",
        "respondent_id": str(respondent.pk),
        "incident_date": "2026-08-01",
        "frequency": "one_time",
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def test_complaint_types_returns_labelled_options(cast):
    r = as_user(cast["bob"]).get(f"{BASE}/complaints/types?category=general")
    assert r.status_code == 200, r.data
    assert r.data["category"] == "general"
    values = {t["value"] for t in r.data["types"]}
    assert enums.ComplaintType.THEFT in values
    assert all({"value", "label"} <= set(t) for t in r.data["types"])


def test_employee_lookup_shape_and_filters(cast):
    r = as_user(cast["bob"]).get(f"{BASE}/employees")
    assert r.status_code == 200
    assert {"data", "total", "page", "pageSize"} <= set(r.data)
    assert r.data["total"] == 5
    row = r.data["data"][0]
    assert {"id", "full_name", "avatar_url", "role_title", "department"} == set(row)

    excluded = as_user(cast["bob"]).get(f"{BASE}/employees?exclude_id={cast['bob'].pk}")
    assert excluded.data["total"] == 4

    searched = as_user(cast["bob"]).get(f"{BASE}/employees?search=Alice")
    assert [e["full_name"] for e in searched.data["data"]] == ["Alice"]


def test_departments_returns_flat_name_list(cast):
    Department.objects.create(organisation=cast["org"], name="Software & Tech")
    Department.objects.create(organisation=cast["org"], name="Accounting")
    r = as_user(cast["bob"]).get(f"{BASE}/departments")
    assert r.status_code == 200
    assert r.data == {"data": ["Accounting", "Software & Tech"]}


# ---------------------------------------------------------------------------
# Create -- the vocabulary translation
# ---------------------------------------------------------------------------

def test_create_general_maps_category_and_enums(cast):
    r = as_user(cast["bob"]).post(f"{BASE}/complaints", general_body(), format="json")
    assert r.status_code == 201, r.data
    assert r.data["category"] == "general"
    assert r.data["status"] == "pending"
    assert r.data["stage"] is None
    assert r.data["decision"] is None

    c = Complaint.objects.get(pk=r.data["id"])
    assert c.source == enums.ComplaintSource.SELF        # employee app always SELF
    assert c.subject_type == enums.SubjectType.GENERAL
    assert c.complainant_id == cast["bob"].pk
    assert c.complaint_type == enums.ComplaintType.THEFT  # label -> value
    assert c.visibility == enums.Visibility.HR            # "hr" -> HR


def test_create_employee_path_maps_everything(cast):
    body = employee_body(
        cast["carol"],
        frequency="repeat_behavior",
        occurrence_count=3,
        witness_ids=[str(cast["alice"].pk)],
    )
    r = as_user(cast["bob"]).post(f"{BASE}/complaints", body, format="json")
    assert r.status_code == 201, r.data

    c = Complaint.objects.get(pk=r.data["id"])
    assert c.subject_type == enums.SubjectType.EMPLOYEE
    assert c.respondent_id == cast["carol"].pk
    assert c.complaint_type == enums.ComplaintType.SEXUAL_HARASSMENT
    assert c.frequency == enums.Frequency.REPEAT_BEHAVIOR
    assert c.occurrence_count == 3
    assert c.visibility == enums.Visibility.BOTH
    assert c.witnesses.count() == 1


def test_others_falls_back_to_the_description_for_its_note(cast):
    body = general_body(complaint_type="Others", description="Rota fairness issue")
    r = as_user(cast["bob"]).post(f"{BASE}/complaints", body, format="json")
    assert r.status_code == 201, r.data
    c = Complaint.objects.get(pk=r.data["id"])
    assert c.complaint_type == enums.ComplaintType.OTHERS
    assert c.complaint_type_note == "Rota fairness issue"


def test_intake_override_still_fires_through_the_app(cast):
    """Bob complains about his own manager Alice as LINE_MANAGER -> forced to HR.

    The override lives in the service; this proves the app path does not bypass
    it and that the audit row is still written.
    """
    body = employee_body(cast["alice"], visibility="line_manager")
    r = as_user(cast["bob"]).post(f"{BASE}/complaints", body, format="json")
    assert r.status_code == 201, r.data
    c = Complaint.objects.get(pk=r.data["id"])
    assert c.visibility == enums.Visibility.HR
    assert c.visibility_requested == enums.Visibility.LINE_MANAGER
    assert c.events.filter(verb=enums.EventVerb.VISIBILITY_OVERRIDDEN).exists()


# ---------------------------------------------------------------------------
# Create -- 422 validation envelope
# ---------------------------------------------------------------------------

def test_missing_complaint_type_is_a_422_keyed_error(cast):
    body = general_body()
    del body["complaint_type"]
    r = as_user(cast["bob"]).post(f"{BASE}/complaints", body, format="json")
    assert r.status_code == 422
    assert "complaint_type" in r.data["errors"]


def test_employee_path_missing_respondent_is_422(cast):
    body = employee_body(cast["carol"])
    del body["respondent_id"]
    r = as_user(cast["bob"]).post(f"{BASE}/complaints", body, format="json")
    assert r.status_code == 422
    assert "respondent_id" in r.data["errors"]


def test_cannot_file_against_yourself(cast):
    r = as_user(cast["bob"]).post(
        f"{BASE}/complaints", employee_body(cast["bob"]), format="json"
    )
    assert r.status_code == 422
    assert "respondent_id" in r.data["errors"]


def test_cross_tenant_respondent_does_not_resolve(cast):
    outsider = make_employee(make_org("Other"), "Outsider")
    r = as_user(cast["bob"]).post(
        f"{BASE}/complaints", employee_body(outsider), format="json"
    )
    assert r.status_code == 422
    assert "respondent_id" in r.data["errors"]


def test_unknown_visibility_token_is_422(cast):
    r = as_user(cast["bob"]).post(
        f"{BASE}/complaints", general_body(visibility="everyone"), format="json"
    )
    assert r.status_code == 422
    assert "visibility" in r.data["errors"]


def test_bad_document_is_rejected_and_nothing_is_saved(cast):
    body = general_body()
    body["documents"] = SimpleUploadedFile(
        "malware.exe", b"nope", content_type="application/x-msdownload"
    )
    r = as_user(cast["bob"]).post(f"{BASE}/complaints", body, format="multipart")
    assert r.status_code == 422
    assert "documents" in r.data["errors"]
    assert Complaint.objects.count() == 0


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

def test_list_rows_are_camelcase_and_enveloped(cast):
    as_user(cast["bob"]).post(f"{BASE}/complaints", general_body(), format="json")
    r = as_user(cast["bob"]).get(f"{BASE}/complaints?view=reported_by_me")
    assert r.status_code == 200
    assert {"data", "total", "page", "pageSize"} <= set(r.data)
    row = r.data["data"][0]
    assert set(row) == {
        "id", "dateReported", "complaintType", "filedAgainst",
        "status", "stage", "decision",
    }
    assert row["complaintType"] == "Theft"
    assert row["filedAgainst"] is None
    assert row["status"] == "pending"
    assert row["stage"] is None


def test_against_me_tab_uses_the_respondent_route(cast):
    make_complaint(
        cast["org"], complainant=cast["bob"], respondent=cast["carol"],
        visibility=enums.Visibility.HR, state=enums.ComplaintState.UNDER_INVESTIGATION,
    )
    r = as_user(cast["carol"]).get(f"{BASE}/complaints?view=against_me")
    assert r.status_code == 200
    assert r.data["total"] == 1


def test_unknown_status_filter_returns_nothing_not_everything(cast):
    as_user(cast["bob"]).post(f"{BASE}/complaints", general_body(), format="json")
    good = as_user(cast["bob"]).get(f"{BASE}/complaints?view=reported_by_me&status=pending")
    assert good.data["total"] == 1
    typo = as_user(cast["bob"]).get(f"{BASE}/complaints?view=reported_by_me&status=opne")
    assert typo.data["total"] == 0


def test_list_only_shows_what_the_policy_allows(cast):
    as_user(cast["bob"]).post(f"{BASE}/complaints", general_body(), format="json")
    r = as_user(cast["dave"]).get(f"{BASE}/complaints?view=reported_by_me")
    assert r.data["total"] == 0


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------

def test_detail_full_shape_for_the_complainant(cast):
    c = make_complaint(
        cast["org"], complainant=cast["bob"], visibility=enums.Visibility.HR,
    )
    r = as_user(cast["bob"]).get(f"{BASE}/complaints/{c.pk}")
    assert r.status_code == 200
    assert r.data["scope"] == "general"
    assert r.data["reportedTo"] == {"id": None, "name": "HR", "role": "HR"}
    assert r.data["documents"] == []


def test_detail_reported_to_resolves_the_line_manager(cast):
    c = make_complaint(
        cast["org"], complainant=cast["bob"], respondent=cast["carol"],
        visibility=enums.Visibility.LINE_MANAGER,
    )
    r = as_user(cast["bob"]).get(f"{BASE}/complaints/{c.pk}")
    assert r.data["reportedTo"]["name"] == cast["alice"].full_name
    assert r.data["reportedTo"]["role"] == "Line Manager"


def test_respondent_detail_is_restricted(cast):
    c = make_complaint(
        cast["org"], complainant=cast["bob"], respondent=cast["carol"],
        visibility=enums.Visibility.HR, state=enums.ComplaintState.UNDER_INVESTIGATION,
    )
    r = as_user(cast["carol"]).get(f"{BASE}/complaints/{c.pk}")
    assert r.status_code == 200
    assert r.data["status"] == "open"
    # Routing and evidence are hidden from the person complained about.
    for hidden in ("reportedTo", "documents"):
        assert hidden not in r.data, f"{hidden} leaked to the respondent"


def test_detail_404_for_someone_with_no_access(cast):
    c = make_complaint(
        cast["org"], complainant=cast["bob"], visibility=enums.Visibility.HR,
    )
    # Dave is unrelated and not HR: the case must be indistinguishable from
    # one that does not exist -- 404, never 403.
    r = as_user(cast["dave"]).get(f"{BASE}/complaints/{c.pk}")
    assert r.status_code == 404


def test_anonymous_access_is_refused(cast):
    assert APIClient().get(f"{BASE}/complaints?view=reported_by_me").status_code in (401, 403)
