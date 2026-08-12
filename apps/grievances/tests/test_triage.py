"""Triage tests.

Appointing an investigator is the pivotal moment in the lifecycle: several
things change at once and they must all happen together. Most of these assert
on the side effects rather than the response, because the side effects are the
point.
"""
from __future__ import annotations

import pytest

from apps.grievances import enums
from apps.grievances.access import ComplaintAccessPolicy
from apps.grievances.models import Complaint, Investigation
from apps.grievances.services import (
    ConflictOfInterest,
    ServiceError,
    appoint_investigator,
)
from apps.grievances.tests.factories import make_complaint, make_employee, make_org
from apps.grievances.tests.test_intake import as_user
from apps.grievances.transitions import (
    TransitionError,
    available_transitions,
    check,
)

pytestmark = pytest.mark.django_db

URL = "/api/v1/complaints/"


@pytest.fixture
def cast():
    org = make_org()
    alice = make_employee(org, "Alice")
    bob = make_employee(org, "Bob", manager=alice)
    carol = make_employee(org, "Carol", manager=alice)
    hr = make_employee(org, "HR User", is_hr=True)
    other_hr = make_employee(org, "Second HR", is_hr=True)
    dave = make_employee(org, "Dave")
    return {
        "org": org, "alice": alice, "bob": bob, "carol": carol,
        "hr": hr, "other_hr": other_hr, "dave": dave,
    }


@pytest.fixture
def complaint(cast):
    return make_complaint(
        cast["org"], complainant=cast["bob"], respondent=cast["carol"],
        visibility=enums.Visibility.HR,
    )


# ---------------------------------------------------------------------------
# The transition table
# ---------------------------------------------------------------------------

def test_transition_table_rejects_illegal_moves(cast, complaint):
    check(complaint, "appoint_investigator")  # legal from SUBMITTED

    complaint.state = enums.ComplaintState.RESOLVED
    with pytest.raises(TransitionError):
        check(complaint, "appoint_investigator")


def test_there_is_no_path_from_submitted_straight_to_resolved():
    """Q4: every case must carry an investigator before it can close."""
    assert "resolve" not in available_transitions(enums.ComplaintState.SUBMITTED)
    assert "appoint_investigator" in available_transitions(
        enums.ComplaintState.SUBMITTED
    )


def test_available_transitions_are_exposed_to_the_client(cast, complaint):
    response = as_user(cast["hr"]).get(f"{URL}{complaint.pk}/")
    assert response.status_code == 200
    assert "appoint_investigator" in response.data["available_transitions"]


# ---------------------------------------------------------------------------
# Appointment side effects
# ---------------------------------------------------------------------------

def test_appointing_opens_the_case(cast, complaint):
    investigation = appoint_investigator(
        complaint=complaint, actor=cast["hr"], lead=cast["dave"],
        due_date="2099-01-01",
    )
    complaint.refresh_from_db()

    assert complaint.state == enums.ComplaintState.UNDER_INVESTIGATION
    assert complaint.status_label == "Open"
    assert complaint.stage_label == "Investigation"
    assert investigation.round == 1
    assert investigation.lead_id == cast["dave"].pk


def test_due_date_is_set_at_triage_not_before(cast, complaint):
    """B3: HR sets the due date when appointing, so it is null before that."""
    assert complaint.due_date is None

    appoint_investigator(
        complaint=complaint, actor=cast["hr"], lead=cast["dave"],
        due_date="2099-06-30",
    )
    complaint.refresh_from_db()
    assert str(complaint.due_date) == "2099-06-30"


def test_respondent_can_see_the_case_for_the_first_time(cast, complaint):
    """Spec v4 A1. This is the moment the respondent learns anything."""
    assert ComplaintAccessPolicy.can_view(complaint, cast["carol"]) is False

    appoint_investigator(complaint=complaint, actor=cast["hr"], lead=cast["dave"])
    complaint.refresh_from_db()

    assert ComplaintAccessPolicy.can_view(complaint, cast["carol"]) is True
    # But still without learning who filed it.
    assert ComplaintAccessPolicy.should_mask_complainant(complaint, cast["carol"]) is True


def test_deletion_window_closes_permanently(cast, complaint):
    """Q5: no deletion once an investigator is appointed."""
    assert complaint.is_open_for_deletion is True

    appoint_investigator(complaint=complaint, actor=cast["hr"], lead=cast["dave"])
    complaint.refresh_from_db()
    assert complaint.is_open_for_deletion is False


def test_hr_may_appoint_themselves(cast, complaint):
    """Q4: the normal route for a minor complaint."""
    investigation = appoint_investigator(
        complaint=complaint, actor=cast["hr"], lead=cast["hr"]
    )
    assert investigation.lead_id == cast["hr"].pk
    assert investigation.lead_is_hr is True

    event = complaint.events.get(verb=enums.EventVerb.INVESTIGATOR_APPOINTED)
    assert event.payload["self_assigned"] is True


def test_appointment_writes_an_audit_row(cast, complaint):
    appoint_investigator(complaint=complaint, actor=cast["hr"], lead=cast["dave"])
    event = complaint.events.get(verb=enums.EventVerb.INVESTIGATOR_APPOINTED)

    assert event.actor_id == cast["hr"].pk
    assert event.from_state == enums.ComplaintState.SUBMITTED
    assert event.to_state == enums.ComplaintState.UNDER_INVESTIGATION
    assert event.payload["lead_id"] == str(cast["dave"].pk)


# ---------------------------------------------------------------------------
# Collaborator seeding
# ---------------------------------------------------------------------------

def test_complainant_is_seeded_as_a_collaborator(cast, complaint):
    investigation = appoint_investigator(
        complaint=complaint, actor=cast["hr"], lead=cast["dave"]
    )
    roles = {
        c.employee_id: c.role for c in investigation.collaborators.all()
    }
    assert roles[cast["bob"].pk] == enums.CollaboratorRole.COMPLAINANT


def test_respondent_is_not_auto_seeded(cast, complaint):
    """Involving the respondent is the lead's judgement, not automatic."""
    investigation = appoint_investigator(
        complaint=complaint, actor=cast["hr"], lead=cast["dave"]
    )
    seeded = {c.employee_id for c in investigation.collaborators.all()}
    assert cast["carol"].pk not in seeded


def test_named_witnesses_are_seeded(cast, complaint):
    complaint.witnesses.create(
        witness_type=enums.WitnessType.EMPLOYEE,
        employee=cast["dave"],
        added_by=cast["bob"],
    )
    investigation = appoint_investigator(
        complaint=complaint, actor=cast["hr"], lead=cast["hr"]
    )
    roles = {c.employee_id: c.role for c in investigation.collaborators.all()}
    assert roles[cast["dave"].pk] == enums.CollaboratorRole.WITNESS


def test_line_manager_witness_resolves_to_the_current_manager(cast, complaint):
    complaint.witnesses.create(
        witness_type=enums.WitnessType.LINE_MANAGER, added_by=cast["bob"]
    )
    investigation = appoint_investigator(
        complaint=complaint, actor=cast["hr"], lead=cast["hr"]
    )
    seeded = {c.employee_id for c in investigation.collaborators.all()}
    assert cast["alice"].pk in seeded


def test_respondent_named_as_a_witness_is_not_seeded(cast, complaint):
    """Never quietly enrol someone as a witness against themselves."""
    complaint.witnesses.create(
        witness_type=enums.WitnessType.EMPLOYEE,
        employee=cast["carol"],
        added_by=cast["bob"],
    )
    investigation = appoint_investigator(
        complaint=complaint, actor=cast["hr"], lead=cast["hr"]
    )
    seeded = {c.employee_id for c in investigation.collaborators.all()}
    assert cast["carol"].pk not in seeded


# ---------------------------------------------------------------------------
# Conflict of interest
# ---------------------------------------------------------------------------

def test_hr_cannot_triage_a_complaint_about_themselves(cast):
    """Being HR does not make you neutral about a case you are named in."""
    about_hr = make_complaint(
        cast["org"], complainant=cast["bob"], respondent=cast["hr"],
        visibility=enums.Visibility.HR,
    )
    with pytest.raises(ConflictOfInterest):
        appoint_investigator(
            complaint=about_hr, actor=cast["hr"], lead=cast["dave"]
        )

    # A different HR colleague can.
    appoint_investigator(
        complaint=about_hr, actor=cast["other_hr"], lead=cast["dave"]
    )


def test_hr_cannot_triage_a_complaint_they_filed(cast):
    filed_by_hr = make_complaint(
        cast["org"], complainant=cast["hr"], respondent=cast["carol"],
        visibility=enums.Visibility.HR,
    )
    with pytest.raises(ConflictOfInterest):
        appoint_investigator(
            complaint=filed_by_hr, actor=cast["hr"], lead=cast["dave"]
        )


def test_the_respondent_cannot_be_appointed_as_investigator(cast, complaint):
    with pytest.raises(ConflictOfInterest):
        appoint_investigator(
            complaint=complaint, actor=cast["hr"], lead=cast["carol"]
        )


def test_the_complainant_cannot_investigate_their_own_complaint(cast, complaint):
    with pytest.raises(ConflictOfInterest):
        appoint_investigator(
            complaint=complaint, actor=cast["hr"], lead=cast["bob"]
        )


def test_lead_must_be_in_the_same_organisation(cast, complaint):
    outsider = make_employee(make_org("Other"), "Outsider")
    with pytest.raises(ServiceError):
        appoint_investigator(
            complaint=complaint, actor=cast["hr"], lead=outsider
        )


# ---------------------------------------------------------------------------
# Double submission
# ---------------------------------------------------------------------------

def test_appointing_twice_raises_rather_than_creating_a_second_round(cast, complaint):
    """The modal is slow; the button gets clicked twice."""
    appoint_investigator(complaint=complaint, actor=cast["hr"], lead=cast["dave"])
    complaint.refresh_from_db()

    with pytest.raises(TransitionError):
        appoint_investigator(complaint=complaint, actor=cast["hr"], lead=cast["dave"])

    assert Investigation.objects.filter(complaint=complaint).count() == 1


def test_double_submission_returns_409_not_500(cast, complaint):
    client = as_user(cast["hr"])
    payload = {"lead": str(cast["dave"].pk), "due_date": "2099-01-01"}

    first = client.post(f"{URL}{complaint.pk}/appoint-investigator/", payload, format="json")
    assert first.status_code == 200, first.data

    second = client.post(f"{URL}{complaint.pk}/appoint-investigator/", payload, format="json")
    assert second.status_code == 409


# ---------------------------------------------------------------------------
# Endpoint permissions
# ---------------------------------------------------------------------------

def test_endpoint_appoints_and_returns_the_updated_complaint(cast, complaint):
    response = as_user(cast["hr"]).post(
        f"{URL}{complaint.pk}/appoint-investigator/",
        {"lead": "self", "due_date": "2099-03-01"},
        format="json",
    )
    assert response.status_code == 200, response.data
    assert response.data["state"] == enums.ComplaintState.UNDER_INVESTIGATION
    assert response.data["investigation"]["lead"]["id"] == str(cast["hr"].pk)
    assert response.data["due_date"] == "2099-03-01"


def test_non_hr_cannot_appoint(cast, complaint):
    response = as_user(cast["bob"]).post(
        f"{URL}{complaint.pk}/appoint-investigator/",
        {"lead": str(cast["dave"].pk)},
        format="json",
    )
    assert response.status_code == 403


def test_line_manager_cannot_appoint(cast):
    """Deferred, not implemented: who acts on LINE_MANAGER cases is open."""
    lm_case = make_complaint(
        cast["org"], complainant=cast["bob"], respondent=cast["carol"],
        visibility=enums.Visibility.LINE_MANAGER,
    )
    response = as_user(cast["alice"]).post(
        f"{URL}{lm_case.pk}/appoint-investigator/",
        {"lead": str(cast["dave"].pk)},
        format="json",
    )
    assert response.status_code == 403


def test_past_due_date_is_rejected(cast, complaint):
    response = as_user(cast["hr"]).post(
        f"{URL}{complaint.pk}/appoint-investigator/",
        {"lead": "self", "due_date": "2020-01-01"},
        format="json",
    )
    assert response.status_code == 400
    assert "due_date" in response.data


def test_unknown_lead_is_rejected(cast, complaint):
    response = as_user(cast["hr"]).post(
        f"{URL}{complaint.pk}/appoint-investigator/",
        {"lead": "00000000-0000-0000-0000-000000000000"},
        format="json",
    )
    assert response.status_code == 400
    assert "lead" in response.data


def test_investigations_endpoint_is_refused_to_the_respondent(cast, complaint):
    appoint_investigator(complaint=complaint, actor=cast["hr"], lead=cast["dave"])
    response = as_user(cast["carol"]).get(f"{URL}{complaint.pk}/investigations/")
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Row locking
# ---------------------------------------------------------------------------

def test_appointment_locks_the_row_before_checking_state(cast, complaint):
    """Two HR users must not both be able to appoint.

    The real protection is ``SELECT ... FOR UPDATE``: without it, both callers
    read SUBMITTED, both pass the guard, and the case ends up with two
    investigation rounds.

    That race cannot be reproduced here -- the test database is SQLite and the
    suite is single-threaded -- so this asserts the lock is *requested*. It is
    a white-box test on purpose: it exists to fail loudly if someone removes
    the lock while refactoring, which is otherwise a silent regression that
    only shows up under production concurrency.
    """
    from unittest.mock import patch

    real_manager = Complaint.objects

    with patch.object(
        type(real_manager), "select_for_update", wraps=real_manager.select_for_update
    ) as locked:
        appoint_investigator(
            complaint=complaint, actor=cast["hr"], lead=cast["dave"]
        )

    assert locked.called, (
        "appoint_investigator must lock the complaint row before checking its "
        "state, or two concurrent callers can both open the same case"
    )
