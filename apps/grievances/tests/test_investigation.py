"""Investigation tests.

The section that matters most is "Collaborator isolation". Someone asked to
give evidence must see the question and nothing else -- not the complaint, not
who filed it, not what anyone else said. A leak there is not a bug report, it
is a grievance-process failure.
"""
from __future__ import annotations

import pytest

from apps.grievances import enums
from apps.grievances.models import (
    InformationRequest,
    InvestigationCollaborator,
)
from apps.grievances.services import (
    ServiceError,
    appoint_investigator,
    invite_collaborator,
    record_meeting,
    remove_collaborator,
    request_information,
    respond_to_request,
    submit_report,
)
from apps.grievances.tests.factories import make_complaint, make_employee, make_org
from apps.grievances.tests.test_intake import as_user
from apps.grievances.transitions import TransitionError

pytestmark = pytest.mark.django_db


@pytest.fixture
def case():
    """An open case with a lead, a complainant, a respondent and a witness."""
    org = make_org()
    hr = make_employee(org, "HR User", is_hr=True)
    lead = make_employee(org, "Lead")
    bob = make_employee(org, "Bob")
    carol = make_employee(org, "Carol")
    witness = make_employee(org, "Witness")
    nosy = make_employee(org, "Nosy")

    complaint = make_complaint(
        org, complainant=bob, respondent=carol, visibility=enums.Visibility.HR
    )
    investigation = appoint_investigator(complaint=complaint, actor=hr, lead=lead)

    return {
        "org": org, "hr": hr, "lead": lead, "bob": bob, "carol": carol,
        "witness": witness, "nosy": nosy,
        "complaint": complaint, "investigation": investigation,
    }


def inv_url(investigation, suffix: str = "") -> str:
    return f"/api/v1/investigations/{investigation.pk}/{suffix}"


# ---------------------------------------------------------------------------
# Collaborator isolation -- the sharpest boundary in the module
# ---------------------------------------------------------------------------

def test_collaborator_inbox_shows_only_the_question(case):
    """A witness sees the prompt, who asked, and the case reference. No more."""
    collaborator = invite_collaborator(
        investigation=case["investigation"], actor=case["lead"],
        employee=case["witness"], role=enums.CollaboratorRole.WITNESS,
    )
    request_information(
        collaborator=collaborator, actor=case["lead"],
        prompt="What did you see on the 3rd?",
    )

    response = as_user(case["witness"]).get("/api/v1/me/information-requests/")
    assert response.status_code == 200
    assert len(response.data) == 1

    payload = response.data[0]
    assert payload["prompt"] == "What did you see on the 3rd?"
    assert payload["complaint_reference"] == case["complaint"].reference

    leaked = {"description", "complainant", "respondent", "visibility",
              "investigation", "collaborator", "responses", "state"}
    assert not (leaked & set(payload)), f"leaked: {leaked & set(payload)}"


def test_collaborator_cannot_read_the_complaint(case):
    invite_collaborator(
        investigation=case["investigation"], actor=case["lead"],
        employee=case["witness"], role=enums.CollaboratorRole.WITNESS,
    )
    response = as_user(case["witness"]).get(
        f"/api/v1/complaints/{case['complaint'].pk}/"
    )
    assert response.status_code == 404


def test_collaborator_cannot_read_the_investigation(case):
    invite_collaborator(
        investigation=case["investigation"], actor=case["lead"],
        employee=case["witness"], role=enums.CollaboratorRole.WITNESS,
    )
    response = as_user(case["witness"]).get(inv_url(case["investigation"]))
    assert response.status_code in (403, 404)


def test_collaborator_cannot_see_another_collaborators_question(case):
    a = invite_collaborator(
        investigation=case["investigation"], actor=case["lead"],
        employee=case["witness"], role=enums.CollaboratorRole.WITNESS,
    )
    b = invite_collaborator(
        investigation=case["investigation"], actor=case["lead"],
        employee=case["nosy"], role=enums.CollaboratorRole.OTHER,
    )
    request_information(collaborator=a, actor=case["lead"], prompt="Question for A")
    request_information(collaborator=b, actor=case["lead"], prompt="Question for B")

    inbox = as_user(case["nosy"]).get("/api/v1/me/information-requests/")
    prompts = [r["prompt"] for r in inbox.data]
    assert prompts == ["Question for B"]


def test_collaborator_cannot_answer_someone_elses_question(case):
    a = invite_collaborator(
        investigation=case["investigation"], actor=case["lead"],
        employee=case["witness"], role=enums.CollaboratorRole.WITNESS,
    )
    invite_collaborator(
        investigation=case["investigation"], actor=case["lead"],
        employee=case["nosy"], role=enums.CollaboratorRole.OTHER,
    )
    for_a = request_information(
        collaborator=a, actor=case["lead"], prompt="Only A may answer this"
    )

    response = as_user(case["nosy"]).post(
        f"/api/v1/me/information-requests/{for_a.pk}/respond/",
        {"body": "Not my question"}, format="json",
    )
    # 404 rather than 403 -- a 403 would confirm the id exists.
    assert response.status_code == 404
    assert not for_a.responses.exists()


def test_answering_the_same_question_twice_is_refused(case):
    """Double-submitted answer must not create two responses."""
    collaborator = invite_collaborator(
        investigation=case["investigation"], actor=case["lead"],
        employee=case["witness"], role=enums.CollaboratorRole.WITNESS,
    )
    info_request = request_information(
        collaborator=collaborator, actor=case["lead"], prompt="Q"
    )

    client = as_user(case["witness"])
    url = f"/api/v1/me/information-requests/{info_request.pk}/respond/"

    first = client.post(url, {"body": "My answer"}, format="json")
    assert first.status_code == 201

    second = client.post(url, {"body": "My answer"}, format="json")
    assert second.status_code == 400
    assert info_request.responses.count() == 1


# ---------------------------------------------------------------------------
# Information requests
# ---------------------------------------------------------------------------

def test_asking_again_appends_rather_than_overwriting(case):
    """"Request Additional Information" must preserve what was first asked."""
    collaborator = invite_collaborator(
        investigation=case["investigation"], actor=case["lead"],
        employee=case["witness"], role=enums.CollaboratorRole.WITNESS,
    )
    first = request_information(
        collaborator=collaborator, actor=case["lead"], prompt="First question"
    )
    respond_to_request(info_request=first, actor=case["witness"], body="First answer")
    second = request_information(
        collaborator=collaborator, actor=case["lead"], prompt="Follow-up"
    )

    assert InformationRequest.objects.filter(collaborator=collaborator).count() == 2
    first.refresh_from_db()
    assert first.prompt == "First question"
    assert first.status == enums.InformationRequestStatus.ANSWERED
    assert second.status == enums.InformationRequestStatus.PENDING


@pytest.mark.parametrize("prompt", ["", "   ", "\n\t "])
def test_empty_question_is_refused(case, prompt):
    collaborator = invite_collaborator(
        investigation=case["investigation"], actor=case["lead"],
        employee=case["witness"],
    )
    with pytest.raises(ServiceError):
        request_information(
            collaborator=collaborator, actor=case["lead"], prompt=prompt
        )


def test_overlong_question_is_refused(case):
    collaborator = invite_collaborator(
        investigation=case["investigation"], actor=case["lead"],
        employee=case["witness"],
    )
    with pytest.raises(ServiceError):
        request_information(
            collaborator=collaborator, actor=case["lead"], prompt="x" * 6000
        )


def test_answering_marks_the_request_answered(case):
    collaborator = invite_collaborator(
        investigation=case["investigation"], actor=case["lead"],
        employee=case["witness"],
    )
    info_request = request_information(
        collaborator=collaborator, actor=case["lead"], prompt="Q"
    )
    respond_to_request(info_request=info_request, actor=case["witness"], body="A")

    info_request.refresh_from_db()
    assert info_request.status == enums.InformationRequestStatus.ANSWERED
    assert case["complaint"].events.filter(
        verb=enums.EventVerb.INFORMATION_RECEIVED
    ).exists()


# ---------------------------------------------------------------------------
# Collaborators
# ---------------------------------------------------------------------------

def test_inviting_the_same_person_twice_is_safe(case):
    """The unique constraint would otherwise surface as a 500 on a double-click."""
    first = invite_collaborator(
        investigation=case["investigation"], actor=case["lead"],
        employee=case["witness"],
    )
    second = invite_collaborator(
        investigation=case["investigation"], actor=case["lead"],
        employee=case["witness"],
    )
    assert first.pk == second.pk
    assert InvestigationCollaborator.objects.filter(
        investigation=case["investigation"], employee=case["witness"]
    ).count() == 1


def test_cannot_invite_someone_from_another_organisation(case):
    outsider = make_employee(make_org("Other"), "Outsider")
    with pytest.raises(ServiceError):
        invite_collaborator(
            investigation=case["investigation"], actor=case["lead"],
            employee=outsider,
        )


def test_removal_is_soft_and_expires_open_questions(case):
    collaborator = invite_collaborator(
        investigation=case["investigation"], actor=case["lead"],
        employee=case["witness"],
    )
    info_request = request_information(
        collaborator=collaborator, actor=case["lead"], prompt="Q"
    )
    remove_collaborator(collaborator=collaborator, actor=case["lead"])

    collaborator.refresh_from_db()
    info_request.refresh_from_db()
    assert collaborator.status == enums.CollaboratorStatus.REMOVED
    assert info_request.status == enums.InformationRequestStatus.EXPIRED
    # The row survives -- the audit trail needs to show they were involved.
    assert InvestigationCollaborator.objects.filter(pk=collaborator.pk).exists()


def test_removed_collaborator_drops_out_of_their_inbox(case):
    collaborator = invite_collaborator(
        investigation=case["investigation"], actor=case["lead"],
        employee=case["witness"],
    )
    request_information(collaborator=collaborator, actor=case["lead"], prompt="Q")
    remove_collaborator(collaborator=collaborator, actor=case["lead"])

    inbox = as_user(case["witness"]).get("/api/v1/me/information-requests/")
    assert inbox.data == []


def test_cannot_question_a_removed_collaborator(case):
    collaborator = invite_collaborator(
        investigation=case["investigation"], actor=case["lead"],
        employee=case["witness"],
    )
    remove_collaborator(collaborator=collaborator, actor=case["lead"])
    with pytest.raises(ServiceError):
        request_information(
            collaborator=collaborator, actor=case["lead"], prompt="Q"
        )


def test_reinviting_a_removed_collaborator_reinstates_them(case):
    collaborator = invite_collaborator(
        investigation=case["investigation"], actor=case["lead"],
        employee=case["witness"],
    )
    remove_collaborator(collaborator=collaborator, actor=case["lead"])
    again = invite_collaborator(
        investigation=case["investigation"], actor=case["lead"],
        employee=case["witness"],
    )
    assert again.pk == collaborator.pk
    assert again.status == enums.CollaboratorStatus.INVITED


# ---------------------------------------------------------------------------
# Meetings
# ---------------------------------------------------------------------------

def test_meeting_attendee_not_on_the_case_is_added_as_a_collaborator(case):
    """C1: the picker searches the whole organisation."""
    meeting = record_meeting(
        investigation=case["investigation"], actor=case["lead"],
        meeting_date="2026-08-01", findings="They confirmed the timeline.",
        attendee_employees=[case["nosy"]],
    )
    assert meeting.attendees.count() == 1
    assert InvestigationCollaborator.objects.filter(
        investigation=case["investigation"], employee=case["nosy"]
    ).exists()


def test_future_meeting_date_is_refused(case):
    with pytest.raises(ServiceError):
        record_meeting(
            investigation=case["investigation"], actor=case["lead"],
            meeting_date="2099-01-01", findings="Not yet happened",
        )


def test_empty_findings_are_refused(case):
    with pytest.raises(ServiceError):
        record_meeting(
            investigation=case["investigation"], actor=case["lead"],
            meeting_date="2026-08-01", findings="   ",
        )


# ---------------------------------------------------------------------------
# Report submission
# ---------------------------------------------------------------------------

def test_submitting_hands_the_case_back_to_hr(case):
    submit_report(investigation=case["investigation"], actor=case["lead"])

    case["complaint"].refresh_from_db()
    case["investigation"].refresh_from_db()
    assert case["complaint"].state == enums.ComplaintState.AWAITING_DECISION
    assert case["complaint"].stage_label == "Resolution"
    assert case["investigation"].state == enums.InvestigationState.REPORT_SUBMITTED
    assert case["investigation"].report_submitted_at is not None


def test_submitting_twice_is_refused(case):
    submit_report(investigation=case["investigation"], actor=case["lead"])
    with pytest.raises(TransitionError):
        submit_report(investigation=case["investigation"], actor=case["lead"])


def test_double_submission_over_http_returns_409(case):
    client = as_user(case["lead"])
    url = inv_url(case["investigation"], "submit-report/")

    assert client.post(url).status_code == 200
    assert client.post(url).status_code == 409


def test_submission_expires_outstanding_questions(case):
    collaborator = invite_collaborator(
        investigation=case["investigation"], actor=case["lead"],
        employee=case["witness"],
    )
    info_request = request_information(
        collaborator=collaborator, actor=case["lead"], prompt="Q"
    )
    submit_report(investigation=case["investigation"], actor=case["lead"])

    info_request.refresh_from_db()
    assert info_request.status == enums.InformationRequestStatus.EXPIRED


def test_an_investigation_that_found_nothing_can_still_be_submitted(case):
    """No minimum quota of meetings or notes. Finding nothing is a real outcome."""
    submit_report(investigation=case["investigation"], actor=case["lead"])
    case["complaint"].refresh_from_db()
    assert case["complaint"].state == enums.ComplaintState.AWAITING_DECISION


def test_nothing_can_be_added_after_the_report_is_in(case):
    collaborator = invite_collaborator(
        investigation=case["investigation"], actor=case["lead"],
        employee=case["witness"],
    )
    submit_report(investigation=case["investigation"], actor=case["lead"])
    case["investigation"].refresh_from_db()

    with pytest.raises(ServiceError):
        request_information(
            collaborator=collaborator, actor=case["lead"], prompt="One more thing"
        )
    with pytest.raises(ServiceError):
        record_meeting(
            investigation=case["investigation"], actor=case["lead"],
            meeting_date="2026-08-01", findings="Late addition",
        )


# ---------------------------------------------------------------------------
# Who may drive the investigation
# ---------------------------------------------------------------------------

def test_hr_can_read_but_not_drive(case):
    """Taking over means reassigning the case, which is auditable."""
    client = as_user(case["hr"])
    assert client.get(inv_url(case["investigation"])).status_code == 200

    write = client.post(
        inv_url(case["investigation"], "notes/"), {"body": "HR note"}, format="json"
    )
    assert write.status_code == 403


def test_lead_can_drive(case):
    response = as_user(case["lead"]).post(
        inv_url(case["investigation"], "notes/"),
        {"body": "Spoke to the complainant."}, format="json",
    )
    assert response.status_code == 201


def test_respondent_cannot_reach_the_investigation(case):
    response = as_user(case["carol"]).get(inv_url(case["investigation"]))
    assert response.status_code in (403, 404)


def test_unrelated_employee_cannot_reach_the_investigation(case):
    response = as_user(case["nosy"]).get(inv_url(case["investigation"]))
    assert response.status_code in (403, 404)


def test_cross_tenant_investigation_is_not_found(case):
    outsider = make_employee(make_org("Other"), "Outsider", is_hr=True)
    response = as_user(outsider).get(inv_url(case["investigation"]))
    assert response.status_code == 404
