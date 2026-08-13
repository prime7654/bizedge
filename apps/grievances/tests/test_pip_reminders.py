"""PIP follow-ups and the reminder command.

The clock is passed in rather than read from ``now()``, so these tests do not
depend on the day they are run. A suite that fails at midnight or on a plane
erodes trust in the whole suite.

The property that matters most: running the command twice must not send twice.
"""
from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.grievances import enums
from apps.grievances.models import PIPFollowUp, PIPPlan
from apps.grievances.services import (
    ServiceError,
    appoint_investigator,
    complete_follow_up,
    due_follow_ups,
    resolve_complaint,
    submit_report,
)
from apps.grievances.tests.factories import make_complaint, make_employee, make_org
from apps.grievances.tests.test_intake import as_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def plan():
    """A resolved complaint carrying a PIP with two scheduled check-ins."""
    org = make_org()
    hr = make_employee(org, "HR User", is_hr=True)
    lead = make_employee(org, "Lead")
    bob = make_employee(org, "Bob")
    carol = make_employee(org, "Carol")

    complaint = make_complaint(
        org, complainant=bob, respondent=carol, visibility=enums.Visibility.HR
    )
    investigation = appoint_investigator(complaint=complaint, actor=hr, lead=lead)
    submit_report(investigation=investigation, actor=lead)
    complaint.refresh_from_db()

    resolve_complaint(
        complaint=complaint, actor=hr,
        decision=enums.InvestigationDecision.SUBSTANTIATED,
        resolution_type=enums.ResolutionType.FORMAL,
        formal_resolution_type=enums.FormalResolutionType.FIRST_WRITTEN_WARNING,
        decision_notes="Upheld.",
        pip={
            "start_date": "2026-09-01",
            "end_date": "2026-12-01",
            "follow_ups": [
                {"scheduled_date": "2026-09-15",
                 "kind": enums.FollowUpKind.TWO_WEEK_CHECKIN},
                {"scheduled_date": "2026-10-15"},
            ],
        },
    )
    return {
        "org": org, "hr": hr, "carol": carol, "bob": bob,
        "plan": PIPPlan.objects.get(employee=carol),
    }


def run_command(**kwargs) -> str:
    out = StringIO()
    call_command("send_due_pip_reminders", stdout=out, **kwargs)
    return out.getvalue()


# ---------------------------------------------------------------------------
# Which check-ins are due
# ---------------------------------------------------------------------------

def test_nothing_is_due_before_the_scheduled_date(plan):
    assert due_follow_ups(on_date="2026-09-01").count() == 0


def test_a_check_in_is_due_on_its_date(plan):
    due = due_follow_ups(on_date="2026-09-15")
    assert due.count() == 1
    assert str(due.first().scheduled_date) == "2026-09-15"


def test_a_missed_day_is_picked_up_later(plan):
    """Scheduled on-or-before, so a skipped run does not lose the reminder."""
    assert due_follow_ups(on_date="2026-09-20").count() == 1


def test_both_check_ins_come_due_by_the_later_date(plan):
    assert due_follow_ups(on_date="2026-10-20").count() == 2


def test_completed_check_ins_are_not_due(plan):
    follow_up = plan["plan"].follow_ups.order_by("scheduled_date").first()
    complete_follow_up(follow_up=follow_up, actor=plan["hr"], outcome_notes="Went well.")
    assert due_follow_ups(on_date="2026-09-20").count() == 0


def test_reminders_can_be_switched_off_per_check_in(plan):
    plan["plan"].follow_ups.update(reminder_enabled=False)
    assert due_follow_ups(on_date="2026-12-31").count() == 0


def test_a_cancelled_plan_stops_reminding(plan):
    plan["plan"].state = enums.PIPState.CANCELLED
    plan["plan"].save(update_fields=["state"])
    assert due_follow_ups(on_date="2026-12-31").count() == 0


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------

def test_command_sends_due_reminders(plan):
    output = run_command(date="2026-09-15")
    assert "Sent 1 reminder" in output

    follow_up = plan["plan"].follow_ups.order_by("scheduled_date").first()
    follow_up.refresh_from_db()
    assert follow_up.reminder_sent_at is not None


def test_running_twice_does_not_remind_twice(plan):
    """The property the whole design turns on."""
    first = run_command(date="2026-09-15")
    second = run_command(date="2026-09-15")

    assert "Sent 1 reminder" in first
    assert "No PIP reminders due" in second


def test_command_is_quiet_when_nothing_is_due(plan):
    assert "No PIP reminders due" in run_command(date="2026-09-01")


def test_dry_run_sends_nothing_and_stamps_nothing(plan):
    output = run_command(date="2026-09-15", dry_run=True)
    assert "would remind" in output

    follow_up = plan["plan"].follow_ups.order_by("scheduled_date").first()
    follow_up.refresh_from_db()
    assert follow_up.reminder_sent_at is None


def test_limit_caps_the_batch(plan):
    output = run_command(date="2026-10-20", limit=1)
    assert "Sent 1 reminder" in output
    assert PIPFollowUp.objects.filter(reminder_sent_at__isnull=False).count() == 1


def test_a_backlog_clears_over_successive_runs(plan):
    run_command(date="2026-10-20", limit=1)
    run_command(date="2026-10-20", limit=1)
    assert PIPFollowUp.objects.filter(reminder_sent_at__isnull=True).count() == 0


@pytest.mark.parametrize("bad", ["not-a-date", "2026-13-45", ""])
def test_invalid_date_argument_is_refused(plan, bad):
    with pytest.raises(CommandError):
        run_command(date=bad)


def test_zero_limit_is_refused(plan):
    with pytest.raises(CommandError):
        run_command(date="2026-09-15", limit=0)


# ---------------------------------------------------------------------------
# Completing a check-in
# ---------------------------------------------------------------------------

def test_completing_records_the_outcome(plan):
    follow_up = plan["plan"].follow_ups.order_by("scheduled_date").first()
    complete_follow_up(
        follow_up=follow_up, actor=plan["hr"], outcome_notes="Improvement noted."
    )
    follow_up.refresh_from_db()
    assert follow_up.completed_at is not None
    assert follow_up.outcome_notes == "Improvement noted."


def test_completing_the_last_check_in_closes_the_plan(plan):
    for follow_up in plan["plan"].follow_ups.all():
        complete_follow_up(follow_up=follow_up, actor=plan["hr"])

    plan["plan"].refresh_from_db()
    assert plan["plan"].state == enums.PIPState.COMPLETED


def test_completing_twice_is_refused(plan):
    """Overwriting would quietly lose the first outcome."""
    follow_up = plan["plan"].follow_ups.order_by("scheduled_date").first()
    complete_follow_up(follow_up=follow_up, actor=plan["hr"], outcome_notes="First")

    with pytest.raises(ServiceError):
        complete_follow_up(follow_up=follow_up, actor=plan["hr"], outcome_notes="Second")

    follow_up.refresh_from_db()
    assert follow_up.outcome_notes == "First"


def test_the_employee_under_the_plan_cannot_complete_their_own_check_in(plan):
    follow_up = plan["plan"].follow_ups.order_by("scheduled_date").first()
    with pytest.raises(ServiceError):
        complete_follow_up(follow_up=follow_up, actor=plan["carol"])


def test_someone_from_another_tenant_cannot_complete_it(plan):
    outsider = make_employee(make_org("Other"), "Outsider", is_hr=True)
    follow_up = plan["plan"].follow_ups.order_by("scheduled_date").first()
    with pytest.raises(ServiceError):
        complete_follow_up(follow_up=follow_up, actor=outsider)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

def test_hr_can_list_pips(plan):
    response = as_user(plan["hr"]).get("/api/v1/pips/")
    assert response.status_code == 200
    assert response.data["count"] == 1
    assert len(response.data["results"][0]["follow_ups"]) == 2


def test_non_hr_cannot_list_pips(plan):
    assert as_user(plan["bob"]).get("/api/v1/pips/").status_code == 403


def test_pips_are_tenant_scoped(plan):
    outsider = make_employee(make_org("Other"), "Outsider", is_hr=True)
    response = as_user(outsider).get("/api/v1/pips/")
    assert response.data["count"] == 0


def test_complete_endpoint(plan):
    follow_up = plan["plan"].follow_ups.order_by("scheduled_date").first()
    response = as_user(plan["hr"]).post(
        f"/api/v1/pips/{plan['plan'].pk}/follow-ups/{follow_up.pk}/complete/",
        {"outcome_notes": "On track."}, format="json",
    )
    assert response.status_code == 200, response.data
    assert response.data["completed_at"] is not None


def test_complete_endpoint_rejects_a_check_in_from_another_plan(plan):
    response = as_user(plan["hr"]).post(
        f"/api/v1/pips/{plan['plan'].pk}/follow-ups/"
        "00000000-0000-0000-0000-000000000000/complete/",
        {}, format="json",
    )
    assert response.status_code == 404
