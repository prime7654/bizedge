"""Reopen and deletion tests.

The load-bearing assertion in this file is that reopening leaves the previous
round completely untouched. A grievance record is evidence; rewriting an
earlier finding is a materially different act from changing your mind.
"""
from __future__ import annotations

import pytest

from apps.grievances import enums
from apps.grievances.models import Complaint, ComplaintEvent, Investigation, Resolution
from apps.grievances.services import (
    ConflictOfInterest,
    ServiceError,
    appoint_investigator,
    delete_complaint,
    file_complaint,
    reopen_complaint,
    resolve_complaint,
    submit_report,
)
from apps.grievances.tests.factories import make_complaint, make_employee, make_org
from apps.grievances.tests.test_intake import as_user
from apps.grievances.transitions import TransitionError

pytestmark = pytest.mark.django_db

URL = "/api/v1/complaints/"


@pytest.fixture
def cast():
    org = make_org()
    hr = make_employee(org, "HR User", is_hr=True)
    other_hr = make_employee(org, "Second HR", is_hr=True)
    lead = make_employee(org, "Lead")
    lead2 = make_employee(org, "Second Lead")
    bob = make_employee(org, "Bob")
    carol = make_employee(org, "Carol")
    return {
        "org": org, "hr": hr, "other_hr": other_hr, "lead": lead,
        "lead2": lead2, "bob": bob, "carol": carol,
    }


@pytest.fixture
def resolved(cast):
    complaint = make_complaint(
        cast["org"], complainant=cast["bob"], respondent=cast["carol"],
        visibility=enums.Visibility.HR,
    )
    investigation = appoint_investigator(
        complaint=complaint, actor=cast["hr"], lead=cast["lead"]
    )
    submit_report(investigation=investigation, actor=cast["lead"])
    complaint.refresh_from_db()
    resolve_complaint(
        complaint=complaint, actor=cast["hr"],
        decision=enums.InvestigationDecision.UNSUBSTANTIATED,
        resolution_type=enums.ResolutionType.NO_RESOLUTION_REQUIRED,
        decision_notes="Insufficient evidence on the first pass.",
    )
    complaint.refresh_from_db()
    return {"complaint": complaint, "investigation": investigation}


# ---------------------------------------------------------------------------
# Reopen
# ---------------------------------------------------------------------------

def test_reopening_starts_a_new_round(cast, resolved):
    investigation = reopen_complaint(
        complaint=resolved["complaint"], actor=cast["hr"], lead=cast["lead2"]
    )
    resolved["complaint"].refresh_from_db()

    assert resolved["complaint"].state == enums.ComplaintState.UNDER_INVESTIGATION
    assert investigation.round == 2
    assert investigation.lead_id == cast["lead2"].pk
    assert Investigation.objects.filter(complaint=resolved["complaint"]).count() == 2


def test_the_previous_round_is_left_completely_untouched(cast, resolved):
    """The whole reason reopen creates a new row rather than reusing one."""
    first = resolved["investigation"]
    first.refresh_from_db()  # the fixture's in-memory copy predates submit_report
    original = {
        "state": first.state,
        "lead_id": first.lead_id,
        "submitted_at": first.report_submitted_at,
    }
    first_resolution = Resolution.objects.get(investigation=first)
    original_notes = first_resolution.decision_notes
    original_decision = first_resolution.decision

    reopen_complaint(
        complaint=resolved["complaint"], actor=cast["hr"], lead=cast["lead2"]
    )

    first.refresh_from_db()
    first_resolution.refresh_from_db()
    assert first.state == original["state"]
    assert first.lead_id == original["lead_id"]
    assert first.report_submitted_at == original["submitted_at"]
    assert first_resolution.decision_notes == original_notes
    assert first_resolution.decision == original_decision


def test_a_second_decision_does_not_replace_the_first(cast, resolved):
    reopen_complaint(
        complaint=resolved["complaint"], actor=cast["hr"], lead=cast["lead2"]
    )
    resolved["complaint"].refresh_from_db()
    second = resolved["complaint"].investigations.order_by("-round").first()
    submit_report(investigation=second, actor=cast["lead2"])
    resolved["complaint"].refresh_from_db()

    resolve_complaint(
        complaint=resolved["complaint"], actor=cast["hr"],
        decision=enums.InvestigationDecision.SUBSTANTIATED,
        resolution_type=enums.ResolutionType.FORMAL,
        formal_resolution_type=enums.FormalResolutionType.FIRST_WRITTEN_WARNING,
        decision_notes="New evidence came to light.",
    )

    decisions = list(
        Resolution.objects.filter(complaint=resolved["complaint"])
        .order_by("decided_at")
        .values_list("decision", flat=True)
    )
    assert decisions == [
        enums.InvestigationDecision.UNSUBSTANTIATED,
        enums.InvestigationDecision.SUBSTANTIATED,
    ]


def test_cannot_reopen_a_case_that_is_not_closed(cast):
    open_case = make_complaint(
        cast["org"], complainant=cast["bob"], visibility=enums.Visibility.HR
    )
    with pytest.raises(TransitionError):
        reopen_complaint(
            complaint=open_case, actor=cast["hr"], lead=cast["lead"]
        )


def test_reopening_twice_is_refused(cast, resolved):
    reopen_complaint(
        complaint=resolved["complaint"], actor=cast["hr"], lead=cast["lead2"]
    )
    resolved["complaint"].refresh_from_db()
    with pytest.raises(TransitionError):
        reopen_complaint(
            complaint=resolved["complaint"], actor=cast["hr"], lead=cast["lead2"]
        )


def test_hr_named_in_the_complaint_cannot_reopen_it(cast, resolved):
    resolved["complaint"].respondent = cast["hr"]
    resolved["complaint"].save(update_fields=["respondent"])
    with pytest.raises(ConflictOfInterest):
        reopen_complaint(
            complaint=resolved["complaint"], actor=cast["hr"], lead=cast["lead2"]
        )


def test_reopen_requires_a_lead(cast, resolved):
    with pytest.raises(ServiceError):
        reopen_complaint(
            complaint=resolved["complaint"], actor=cast["hr"], lead=None
        )


def test_reopen_endpoint(cast, resolved):
    response = as_user(cast["hr"]).post(
        f"{URL}{resolved['complaint'].pk}/reopen/",
        {"lead": "self", "reason": "Further evidence submitted."},
        format="json",
    )
    assert response.status_code == 200, response.data
    assert response.data["state"] == enums.ComplaintState.UNDER_INVESTIGATION
    assert response.data["investigation"]["round"] == 2


def test_double_reopen_over_http_returns_409(cast, resolved):
    client = as_user(cast["hr"])
    url = f"{URL}{resolved['complaint'].pk}/reopen/"
    assert client.post(url, {"lead": "self"}, format="json").status_code == 200
    assert client.post(url, {"lead": "self"}, format="json").status_code == 409


def test_non_hr_cannot_reopen(cast, resolved):
    response = as_user(cast["bob"]).post(
        f"{URL}{resolved['complaint'].pk}/reopen/", {"lead": "self"}, format="json"
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------

@pytest.fixture
def hr_filed(cast):
    """A complaint HR created, which is the only deletable kind."""
    return file_complaint(
        organisation=cast["org"],
        filed_by=cast["hr"],
        source=enums.ComplaintSource.HR_FOR_EMPLOYEE,
        subject_type=enums.SubjectType.EMPLOYEE,
        complaint_type=enums.ComplaintType.THEFT,
        description="Filed in error.",
        visibility=enums.Visibility.HR,
        complainant=cast["bob"],
        respondent=cast["carol"],
        frequency=enums.Frequency.ONE_TIME,
        incident_date="2026-08-01",
    )


def test_creator_can_delete_before_an_investigator_is_appointed(cast, hr_filed):
    delete_complaint(complaint=hr_filed, actor=cast["hr"])

    assert not Complaint.objects.filter(pk=hr_filed.pk).exists()
    # The row survives -- this is a soft delete.
    assert Complaint.all_objects.filter(pk=hr_filed.pk).exists()
    restored = Complaint.all_objects.get(pk=hr_filed.pk)
    assert restored.deleted_by_id == cast["hr"].pk


def test_a_different_hr_user_cannot_delete_it(cast, hr_filed):
    with pytest.raises(ServiceError):
        delete_complaint(complaint=hr_filed, actor=cast["other_hr"])
    assert Complaint.objects.filter(pk=hr_filed.pk).exists()


def test_the_complainant_cannot_delete_it(cast, hr_filed):
    with pytest.raises(ServiceError):
        delete_complaint(complaint=hr_filed, actor=cast["bob"])


def test_deletion_closes_once_an_investigator_is_appointed(cast, hr_filed):
    appoint_investigator(complaint=hr_filed, actor=cast["hr"], lead=cast["lead"])
    hr_filed.refresh_from_db()

    with pytest.raises(ServiceError):
        delete_complaint(complaint=hr_filed, actor=cast["hr"])
    assert Complaint.objects.filter(pk=hr_filed.pk).exists()


def test_refused_attempts_are_recorded(cast, hr_filed):
    """A pattern of trying to delete other people's complaints should be visible.

    Regression: the refusal event was originally written inside the same
    ``atomic`` block that then raised, so it was rolled back and no record of
    the attempt survived.
    """
    with pytest.raises(ServiceError):
        delete_complaint(complaint=hr_filed, actor=cast["other_hr"])

    event = ComplaintEvent.objects.get(
        complaint=hr_filed, verb=enums.EventVerb.DELETE_ATTEMPTED
    )
    assert event.actor_id == cast["other_hr"].pk
    assert event.payload["allowed"] is False


def test_the_audit_trail_survives_deletion(cast, hr_filed):
    delete_complaint(complaint=hr_filed, actor=cast["hr"])
    assert ComplaintEvent.objects.filter(
        complaint_id=hr_filed.pk, verb=enums.EventVerb.DELETED
    ).exists()
    assert ComplaintEvent.objects.filter(
        complaint_id=hr_filed.pk, verb=enums.EventVerb.FILED
    ).exists()


def test_a_deleted_complaint_disappears_from_lists(cast, hr_filed):
    client = as_user(cast["hr"])
    before = client.get(URL).data["count"]

    delete_complaint(complaint=hr_filed, actor=cast["hr"])

    after = client.get(URL).data["count"]
    assert after == before - 1
    assert client.get(f"{URL}{hr_filed.pk}/").status_code == 404


def test_delete_endpoint(cast, hr_filed):
    response = as_user(cast["hr"]).delete(f"{URL}{hr_filed.pk}/")
    assert response.status_code == 204
    assert not Complaint.objects.filter(pk=hr_filed.pk).exists()


def test_delete_endpoint_refuses_a_non_creator_with_403(cast, hr_filed):
    response = as_user(cast["other_hr"]).delete(f"{URL}{hr_filed.pk}/")
    assert response.status_code == 403
    assert Complaint.objects.filter(pk=hr_filed.pk).exists()
