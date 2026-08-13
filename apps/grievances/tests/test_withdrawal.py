"""Withdrawal tests.

Two paths, chosen by how far the case has got. The rule worth stating plainly:
the respondent can never withdraw a complaint about themselves.
"""
from __future__ import annotations

import pytest

from apps.grievances import enums
from apps.grievances.services import (
    ServiceError,
    appoint_investigator,
    invite_collaborator,
    request_information,
    resolve_complaint,
    submit_report,
    withdraw_complaint,
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
    lead = make_employee(org, "Lead")
    bob = make_employee(org, "Bob")
    carol = make_employee(org, "Carol")
    dave = make_employee(org, "Dave")
    complaint = make_complaint(
        org, complainant=bob, respondent=carol, visibility=enums.Visibility.HR
    )
    return {
        "org": org, "hr": hr, "lead": lead, "bob": bob,
        "carol": carol, "dave": dave, "complaint": complaint,
    }


# ---------------------------------------------------------------------------
# Before an investigation
# ---------------------------------------------------------------------------

def test_complainant_can_withdraw_before_investigation(cast):
    withdraw_complaint(complaint=cast["complaint"], actor=cast["bob"])
    cast["complaint"].refresh_from_db()

    assert cast["complaint"].state == enums.ComplaintState.WITHDRAWN
    assert cast["complaint"].status_label == "Closed"
    assert cast["complaint"].withdrawal_requested_by_id == cast["bob"].pk


def test_hr_can_withdraw_on_the_complainants_behalf(cast):
    withdraw_complaint(complaint=cast["complaint"], actor=cast["hr"])
    cast["complaint"].refresh_from_db()
    assert cast["complaint"].state == enums.ComplaintState.WITHDRAWN


def test_reason_is_recorded_on_the_history(cast):
    withdraw_complaint(
        complaint=cast["complaint"], actor=cast["bob"], reason="Resolved between us."
    )
    event = cast["complaint"].events.get(verb=enums.EventVerb.WITHDRAWN)
    assert event.payload["reason"] == "Resolved between us."


def test_absurdly_long_reason_is_truncated(cast):
    withdraw_complaint(
        complaint=cast["complaint"], actor=cast["bob"], reason="x" * 10_000
    )
    event = cast["complaint"].events.get(verb=enums.EventVerb.WITHDRAWN)
    assert len(event.payload["reason"]) <= 2000


# ---------------------------------------------------------------------------
# Who may withdraw
# ---------------------------------------------------------------------------

def test_the_respondent_can_never_withdraw(cast):
    """The failure mode this module exists to prevent."""
    with pytest.raises(ServiceError):
        withdraw_complaint(complaint=cast["complaint"], actor=cast["carol"])

    cast["complaint"].refresh_from_db()
    assert cast["complaint"].state == enums.ComplaintState.SUBMITTED


def test_respondent_who_is_hr_still_cannot_withdraw(cast):
    """Being HR does not let you make a complaint about yourself disappear."""
    hr_respondent = make_employee(cast["org"], "HR Respondent", is_hr=True)
    complaint = make_complaint(
        cast["org"], complainant=cast["bob"], respondent=hr_respondent,
        visibility=enums.Visibility.HR,
    )
    with pytest.raises(ServiceError):
        withdraw_complaint(complaint=complaint, actor=hr_respondent)


def test_unrelated_employee_cannot_withdraw(cast):
    with pytest.raises(ServiceError):
        withdraw_complaint(complaint=cast["complaint"], actor=cast["dave"])


def test_someone_from_another_tenant_cannot_withdraw(cast):
    outsider = make_employee(make_org("Other"), "Outsider", is_hr=True)
    with pytest.raises(ServiceError):
        withdraw_complaint(complaint=cast["complaint"], actor=outsider)


def test_company_filed_complaint_can_only_be_withdrawn_by_hr(cast):
    """No complainant to retract it, so HR is the only route."""
    company = make_complaint(
        cast["org"], complainant=None, filed_by=cast["hr"],
        respondent=cast["carol"], visibility=enums.Visibility.HR,
        source=enums.ComplaintSource.HR_FOR_COMPANY,
    )
    with pytest.raises(ServiceError):
        withdraw_complaint(complaint=company, actor=cast["dave"])

    withdraw_complaint(complaint=company, actor=cast["hr"])
    company.refresh_from_db()
    assert company.state == enums.ComplaintState.WITHDRAWN


# ---------------------------------------------------------------------------
# During an investigation
# ---------------------------------------------------------------------------

def test_withdrawing_mid_investigation_goes_to_hr_not_straight_to_closed(cast):
    """HR keeps the final say -- the company may still need to investigate."""
    appoint_investigator(
        complaint=cast["complaint"], actor=cast["hr"], lead=cast["lead"]
    )
    cast["complaint"].refresh_from_db()

    withdraw_complaint(complaint=cast["complaint"], actor=cast["bob"])
    cast["complaint"].refresh_from_db()

    assert cast["complaint"].state == enums.ComplaintState.AWAITING_DECISION
    assert cast["complaint"].status_label == "Open"
    assert cast["complaint"].withdrawal_requested_at is not None
    assert cast["complaint"].events.filter(
        verb=enums.EventVerb.WITHDRAWAL_REQUESTED
    ).exists()


def test_hr_closes_a_withdrawn_case_with_the_matching_decision(cast):
    investigation = appoint_investigator(
        complaint=cast["complaint"], actor=cast["hr"], lead=cast["lead"]
    )
    cast["complaint"].refresh_from_db()
    withdraw_complaint(complaint=cast["complaint"], actor=cast["bob"])
    cast["complaint"].refresh_from_db()

    resolve_complaint(
        complaint=cast["complaint"], actor=cast["hr"],
        decision=enums.InvestigationDecision.WITHDRAWN_BY_COMPLAINANT,
        resolution_type=enums.ResolutionType.NO_RESOLUTION_REQUIRED,
        decision_notes="Complainant retracted; no further action.",
    )
    cast["complaint"].refresh_from_db()
    assert cast["complaint"].state == enums.ComplaintState.RESOLVED


def test_withdrawal_expires_outstanding_questions(cast):
    investigation = appoint_investigator(
        complaint=cast["complaint"], actor=cast["hr"], lead=cast["lead"]
    )
    cast["complaint"].refresh_from_db()
    collaborator = invite_collaborator(
        investigation=investigation, actor=cast["lead"], employee=cast["dave"]
    )
    info_request = request_information(
        collaborator=collaborator, actor=cast["lead"], prompt="What did you see?"
    )

    withdraw_complaint(complaint=cast["complaint"], actor=cast["bob"])

    info_request.refresh_from_db()
    assert info_request.status == enums.InformationRequestStatus.EXPIRED

    inbox = as_user(cast["dave"]).get("/api/v1/me/information-requests/")
    assert inbox.data == []


# ---------------------------------------------------------------------------
# Nothing left to withdraw
# ---------------------------------------------------------------------------

def test_cannot_withdraw_once_awaiting_a_decision(cast):
    investigation = appoint_investigator(
        complaint=cast["complaint"], actor=cast["hr"], lead=cast["lead"]
    )
    submit_report(investigation=investigation, actor=cast["lead"])
    cast["complaint"].refresh_from_db()

    with pytest.raises(TransitionError):
        withdraw_complaint(complaint=cast["complaint"], actor=cast["bob"])


def test_cannot_withdraw_a_closed_case(cast):
    withdraw_complaint(complaint=cast["complaint"], actor=cast["bob"])
    cast["complaint"].refresh_from_db()

    with pytest.raises(TransitionError):
        withdraw_complaint(complaint=cast["complaint"], actor=cast["bob"])


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

def test_endpoint_withdraws(cast):
    response = as_user(cast["bob"]).post(
        f"{URL}{cast['complaint'].pk}/withdraw/",
        {"reason": "Sorted it directly."}, format="json",
    )
    assert response.status_code == 200, response.data
    assert response.data["state"] == enums.ComplaintState.WITHDRAWN


def test_respondent_gets_404_before_the_investigation_opens(cast):
    """Stronger than a 403.

    A SUBMITTED complaint is invisible to the respondent entirely (A1), so the
    request never reaches the withdraw logic. 403 would confirm the case exists.
    """
    response = as_user(cast["carol"]).post(
        f"{URL}{cast['complaint'].pk}/withdraw/", {}, format="json"
    )
    assert response.status_code == 404


def test_respondent_gets_403_once_they_can_see_the_case(cast):
    """The real test of the guard.

    Once an investigation opens the respondent *can* see the complaint, so the
    request reaches the withdraw logic and must be refused there.
    """
    appoint_investigator(
        complaint=cast["complaint"], actor=cast["hr"], lead=cast["lead"]
    )
    cast["complaint"].refresh_from_db()

    response = as_user(cast["carol"]).post(
        f"{URL}{cast['complaint'].pk}/withdraw/", {}, format="json"
    )
    assert response.status_code == 403

    cast["complaint"].refresh_from_db()
    assert cast["complaint"].state == enums.ComplaintState.UNDER_INVESTIGATION


def test_double_withdrawal_over_http_returns_409(cast):
    client = as_user(cast["bob"])
    url = f"{URL}{cast['complaint'].pk}/withdraw/"

    assert client.post(url, {}, format="json").status_code == 200
    assert client.post(url, {}, format="json").status_code == 409


def test_withdraw_appears_in_available_transitions(cast):
    response = as_user(cast["hr"]).get(f"{URL}{cast['complaint'].pk}/")
    assert "withdraw_before_investigation" in response.data["available_transitions"]
