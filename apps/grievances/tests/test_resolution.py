"""Decision and resolution tests.

The close is a single transactional call. The most important test here is the
atomicity one: if any part fails, nothing must survive -- a PIP attached to a
complaint that is still open would be worse than a plain error.
"""
from __future__ import annotations

import pytest

from apps.grievances import enums
from apps.grievances.models import Complaint, PIPFollowUp, PIPPlan, Resolution
from apps.grievances.services import (
    ConflictOfInterest,
    ServiceError,
    appoint_investigator,
    resolve_complaint,
    submit_report,
)
from apps.grievances.tests.factories import make_complaint, make_employee, make_org
from apps.grievances.tests.test_intake import as_user
from apps.grievances.transitions import TransitionError

pytestmark = pytest.mark.django_db

URL = "/api/v1/complaints/"


@pytest.fixture
def awaiting():
    """A case that has been investigated and is waiting on HR's decision."""
    org = make_org()
    hr = make_employee(org, "HR User", is_hr=True)
    other_hr = make_employee(org, "Second HR", is_hr=True)
    lead = make_employee(org, "Lead")
    bob = make_employee(org, "Bob")
    carol = make_employee(org, "Carol")

    complaint = make_complaint(
        org, complainant=bob, respondent=carol, visibility=enums.Visibility.HR
    )
    investigation = appoint_investigator(complaint=complaint, actor=hr, lead=lead)
    submit_report(investigation=investigation, actor=lead)
    complaint.refresh_from_db()

    return {
        "org": org, "hr": hr, "other_hr": other_hr, "lead": lead,
        "bob": bob, "carol": carol,
        "complaint": complaint, "investigation": investigation,
    }


def formal_payload(**overrides):
    payload = {
        "decision": enums.InvestigationDecision.SUBSTANTIATED,
        "resolution_type": enums.ResolutionType.FORMAL,
        "formal_resolution_type": enums.FormalResolutionType.FIRST_WRITTEN_WARNING,
        "decision_notes": "Upheld after reviewing the evidence.",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# The close
# ---------------------------------------------------------------------------

def test_resolving_closes_the_case(awaiting):
    resolve_complaint(
        complaint=awaiting["complaint"], actor=awaiting["hr"], **formal_payload()
    )
    awaiting["complaint"].refresh_from_db()

    assert awaiting["complaint"].state == enums.ComplaintState.RESOLVED
    assert awaiting["complaint"].status_label == "Closed"
    assert Resolution.objects.filter(complaint=awaiting["complaint"]).count() == 1


def test_resolution_records_who_decided_and_why(awaiting):
    resolution = resolve_complaint(
        complaint=awaiting["complaint"], actor=awaiting["hr"], **formal_payload()
    )
    assert resolution.decided_by_id == awaiting["hr"].pk
    assert resolution.decision_notes.startswith("Upheld")

    event = awaiting["complaint"].events.get(verb=enums.EventVerb.RESOLVED)
    assert event.to_state == enums.ComplaintState.RESOLVED
    assert event.payload["decision"] == enums.InvestigationDecision.SUBSTANTIATED


def test_cannot_resolve_before_the_report_is_in(awaiting):
    org = awaiting["org"]
    fresh = make_complaint(
        org, complainant=awaiting["bob"], respondent=awaiting["carol"],
        visibility=enums.Visibility.HR,
    )
    with pytest.raises(TransitionError):
        resolve_complaint(complaint=fresh, actor=awaiting["hr"], **formal_payload())


def test_resolving_twice_is_refused(awaiting):
    resolve_complaint(
        complaint=awaiting["complaint"], actor=awaiting["hr"], **formal_payload()
    )
    with pytest.raises(TransitionError):
        resolve_complaint(
            complaint=awaiting["complaint"], actor=awaiting["hr"], **formal_payload()
        )


def test_double_submission_over_http_returns_409(awaiting):
    client = as_user(awaiting["hr"])
    url = f"{URL}{awaiting['complaint'].pk}/resolution/"

    assert client.post(url, formal_payload(), format="json").status_code == 200
    assert client.post(url, formal_payload(), format="json").status_code == 409


def test_hr_named_in_the_complaint_cannot_decide_it(awaiting):
    """The same conflict rule that applies at triage applies at the decision."""
    org = awaiting["org"]
    about_hr = make_complaint(
        org, complainant=awaiting["bob"], respondent=awaiting["hr"],
        visibility=enums.Visibility.HR,
    )
    investigation = appoint_investigator(
        complaint=about_hr, actor=awaiting["other_hr"], lead=awaiting["lead"]
    )
    submit_report(investigation=investigation, actor=awaiting["lead"])
    about_hr.refresh_from_db()

    with pytest.raises(ConflictOfInterest):
        resolve_complaint(complaint=about_hr, actor=awaiting["hr"], **formal_payload())

    resolve_complaint(
        complaint=about_hr, actor=awaiting["other_hr"], **formal_payload()
    )


# ---------------------------------------------------------------------------
# Formal / informal rules
# ---------------------------------------------------------------------------

def test_informal_resolution(awaiting):
    resolution = resolve_complaint(
        complaint=awaiting["complaint"], actor=awaiting["hr"],
        decision=enums.InvestigationDecision.PARTIALLY_SUBSTANTIATED,
        resolution_type=enums.ResolutionType.INFORMAL,
        informal_resolution_type=enums.InformalResolutionType.MEDIATION,
        decision_notes="Both parties agreed to mediation.",
    )
    assert resolution.formal_resolution_type == ""
    assert resolution.informal_resolution_type == enums.InformalResolutionType.MEDIATION


def test_no_resolution_required(awaiting):
    resolution = resolve_complaint(
        complaint=awaiting["complaint"], actor=awaiting["hr"],
        decision=enums.InvestigationDecision.UNSUBSTANTIATED,
        resolution_type=enums.ResolutionType.NO_RESOLUTION_REQUIRED,
        decision_notes="No evidence supported the allegation.",
    )
    assert resolution.formal_resolution_type == ""
    assert resolution.informal_resolution_type == ""


@pytest.mark.parametrize(
    "kwargs",
    [
        # formal with no sub-type
        {"resolution_type": enums.ResolutionType.FORMAL,
         "formal_resolution_type": ""},
        # formal carrying an informal sub-type
        {"resolution_type": enums.ResolutionType.FORMAL,
         "formal_resolution_type": "",
         "informal_resolution_type": enums.InformalResolutionType.COACHING},
        # informal with no sub-type
        {"resolution_type": enums.ResolutionType.INFORMAL,
         "formal_resolution_type": ""},
        # "no resolution" carrying a sub-type
        {"resolution_type": enums.ResolutionType.NO_RESOLUTION_REQUIRED,
         "formal_resolution_type": enums.FormalResolutionType.SUSPENSION},
    ],
)
def test_mismatched_resolution_shapes_are_refused(awaiting, kwargs):
    """These mirror DB check constraints. Reaching the constraint means a 500."""
    payload = formal_payload(**kwargs)
    with pytest.raises(ServiceError):
        resolve_complaint(
            complaint=awaiting["complaint"], actor=awaiting["hr"], **payload
        )


def test_others_requires_a_note(awaiting):
    payload = formal_payload(
        formal_resolution_type=enums.FormalResolutionType.OTHERS
    )
    with pytest.raises(ServiceError):
        resolve_complaint(
            complaint=awaiting["complaint"], actor=awaiting["hr"], **payload
        )

    payload["resolution_note"] = "Redeployed to another team."
    resolve_complaint(
        complaint=awaiting["complaint"], actor=awaiting["hr"], **payload
    )


@pytest.mark.parametrize("notes", ["", "   ", "\n"])
def test_decision_notes_are_required(awaiting, notes):
    with pytest.raises(ServiceError):
        resolve_complaint(
            complaint=awaiting["complaint"], actor=awaiting["hr"],
            **formal_payload(decision_notes=notes),
        )


# ---------------------------------------------------------------------------
# PIP
# ---------------------------------------------------------------------------

def test_pip_is_created_with_training_and_follow_ups(awaiting):
    from apps.directory.models import Training

    training = Training.objects.create(
        organisation=awaiting["org"], name="Respect at work"
    )
    resolve_complaint(
        complaint=awaiting["complaint"], actor=awaiting["hr"],
        **formal_payload(),
        pip={
            "start_date": "2026-09-01",
            "end_date": "2026-11-30",
            "trainings": [training],
            "follow_ups": [
                {"scheduled_date": "2026-09-15",
                 "kind": enums.FollowUpKind.TWO_WEEK_CHECKIN},
                {"scheduled_date": "2026-10-15"},
            ],
        },
    )
    plan = PIPPlan.objects.get(employee=awaiting["carol"])
    assert plan.state == enums.PIPState.ACTIVE
    assert plan.training_assignments.count() == 1
    assert plan.follow_ups.count() == 2


def test_pip_needs_a_respondent(awaiting):
    """A general complaint has nobody for a PIP to apply to."""
    org = awaiting["org"]
    general = make_complaint(
        org, complainant=awaiting["bob"], visibility=enums.Visibility.HR
    )
    investigation = appoint_investigator(
        complaint=general, actor=awaiting["hr"], lead=awaiting["lead"]
    )
    submit_report(investigation=investigation, actor=awaiting["lead"])
    general.refresh_from_db()

    with pytest.raises(ServiceError):
        resolve_complaint(
            complaint=general, actor=awaiting["hr"], **formal_payload(),
            pip={"start_date": "2026-09-01", "end_date": "2026-10-01"},
        )


def test_pip_cannot_end_before_it_starts(awaiting):
    with pytest.raises(ServiceError):
        resolve_complaint(
            complaint=awaiting["complaint"], actor=awaiting["hr"],
            **formal_payload(),
            pip={"start_date": "2026-11-01", "end_date": "2026-09-01"},
        )


def test_absurdly_long_pip_is_refused(awaiting):
    """Almost always a mistyped year."""
    with pytest.raises(ServiceError):
        resolve_complaint(
            complaint=awaiting["complaint"], actor=awaiting["hr"],
            **formal_payload(),
            pip={"start_date": "2026-09-01", "end_date": "2126-09-01"},
        )


def test_follow_up_before_the_pip_starts_is_refused(awaiting):
    with pytest.raises(ServiceError):
        resolve_complaint(
            complaint=awaiting["complaint"], actor=awaiting["hr"],
            **formal_payload(),
            pip={
                "start_date": "2026-09-01", "end_date": "2026-11-01",
                "follow_ups": [{"scheduled_date": "2026-08-01"}],
            },
        )


def test_training_from_another_tenant_is_refused(awaiting):
    from apps.directory.models import Training

    foreign = Training.objects.create(organisation=make_org("Other"), name="Foreign")
    with pytest.raises(ServiceError):
        resolve_complaint(
            complaint=awaiting["complaint"], actor=awaiting["hr"],
            **formal_payload(),
            pip={
                "start_date": "2026-09-01", "end_date": "2026-11-01",
                "trainings": [foreign],
            },
        )


# ---------------------------------------------------------------------------
# Atomicity -- the reason this is one call
# ---------------------------------------------------------------------------

def test_a_failure_mid_close_leaves_nothing_behind(awaiting):
    """The whole point of doing this in one transaction.

    A bad follow-up date is caught after the Resolution and PIPPlan rows have
    already been written. If the transaction did not roll back, the case would
    be left with a decision recorded, a half-built plan, and a complaint still
    sitting in AWAITING_DECISION.
    """
    complaint = awaiting["complaint"]

    with pytest.raises(ServiceError):
        resolve_complaint(
            complaint=complaint, actor=awaiting["hr"], **formal_payload(),
            pip={
                "start_date": "2026-09-01", "end_date": "2026-11-01",
                "follow_ups": [
                    {"scheduled_date": "2026-09-15"},
                    {"scheduled_date": "2020-01-01"},  # invalid, fails last
                ],
            },
        )

    complaint.refresh_from_db()
    assert complaint.state == enums.ComplaintState.AWAITING_DECISION
    assert not Resolution.objects.filter(complaint=complaint).exists()
    assert not PIPPlan.objects.filter(employee=awaiting["carol"]).exists()
    assert not PIPFollowUp.objects.exists()


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

def test_endpoint_closes_the_case(awaiting):
    response = as_user(awaiting["hr"]).post(
        f"{URL}{awaiting['complaint'].pk}/resolution/",
        formal_payload(), format="json",
    )
    assert response.status_code == 200, response.data
    assert response.data["state"] == enums.ComplaintState.RESOLVED
    assert response.data["resolution"]["decision"] == (
        enums.InvestigationDecision.SUBSTANTIATED
    )


def test_endpoint_rejects_a_mismatched_shape_with_field_errors(awaiting):
    response = as_user(awaiting["hr"]).post(
        f"{URL}{awaiting['complaint'].pk}/resolution/",
        formal_payload(formal_resolution_type=""), format="json",
    )
    assert response.status_code == 400
    assert "formal_resolution_type" in response.data


def test_non_hr_cannot_resolve(awaiting):
    response = as_user(awaiting["bob"]).post(
        f"{URL}{awaiting['complaint'].pk}/resolution/",
        formal_payload(), format="json",
    )
    assert response.status_code == 403


def test_investigation_lead_cannot_resolve(awaiting):
    """The lead investigates; HR decides."""
    response = as_user(awaiting["lead"]).post(
        f"{URL}{awaiting['complaint'].pk}/resolution/",
        formal_payload(), format="json",
    )
    assert response.status_code == 403


def test_respondent_cannot_read_the_decision(awaiting):
    resolve_complaint(
        complaint=awaiting["complaint"], actor=awaiting["hr"], **formal_payload()
    )
    response = as_user(awaiting["carol"]).get(
        f"{URL}{awaiting['complaint'].pk}/resolutions/"
    )
    assert response.status_code == 403


def test_resolution_locks_the_complaint_before_checking_state(awaiting):
    """Same reasoning as the triage lock test.

    Two HR users deciding at once would otherwise both pass the guard and both
    write a Resolution. The race is not reproducible single-threaded on SQLite,
    so this asserts the lock is requested -- enough to fail loudly if someone
    removes it while refactoring.
    """
    from unittest.mock import patch

    manager = Complaint.objects
    with patch.object(
        type(manager), "select_for_update", wraps=manager.select_for_update
    ) as locked:
        resolve_complaint(
            complaint=awaiting["complaint"], actor=awaiting["hr"], **formal_payload()
        )
    assert locked.called, (
        "resolve_complaint must lock the complaint row before checking state"
    )
