"""Access policy tests.

These are the most important tests in the module. A bug here does not throw an
error -- it quietly shows a grievance to someone who should never have seen it.

The cast, used throughout:

    alice   -- line manager of bob and carol
    bob     -- complainant
    carol   -- respondent, also reports to alice
    hr      -- HR user
    dave    -- unrelated employee, the control
"""
from __future__ import annotations

import pytest

from apps.grievances import enums
from apps.grievances.access import (
    AccessLevel,
    ComplaintAccessPolicy,
    is_orphaned,
    resolve_effective_visibility,
)
from apps.grievances.models import Complaint
from apps.grievances.tests.factories import (
    make_complaint,
    make_employee,
    make_investigation,
    make_org,
)

pytestmark = pytest.mark.django_db

policy = ComplaintAccessPolicy


@pytest.fixture
def cast():
    org = make_org()
    alice = make_employee(org, "Alice")
    bob = make_employee(org, "Bob", manager=alice)
    carol = make_employee(org, "Carol", manager=alice)
    hr = make_employee(org, "HR User", is_hr=True)
    dave = make_employee(org, "Dave")
    return {
        "org": org, "alice": alice, "bob": bob,
        "carol": carol, "hr": hr, "dave": dave,
    }


# ---------------------------------------------------------------------------
# The rule people get wrong: visibility is genuinely three-way
# ---------------------------------------------------------------------------

def test_hr_cannot_see_line_manager_only_complaint(cast):
    """The headline rule. HR does not see LINE_MANAGER complaints."""
    c = make_complaint(
        cast["org"], complainant=cast["bob"], respondent=cast["carol"],
        visibility=enums.Visibility.LINE_MANAGER,
    )
    assert policy.can_view(c, cast["hr"]) is False


def test_line_manager_cannot_see_hr_only_complaint(cast):
    c = make_complaint(
        cast["org"], complainant=cast["bob"], visibility=enums.Visibility.HR
    )
    assert policy.can_view(c, cast["alice"]) is False


def test_both_is_visible_to_hr_and_line_manager(cast):
    c = make_complaint(
        cast["org"], complainant=cast["bob"], visibility=enums.Visibility.BOTH
    )
    assert policy.can_view(c, cast["hr"]) is True
    assert policy.can_view(c, cast["alice"]) is True


def test_unrelated_employee_sees_nothing(cast):
    for visibility in enums.Visibility.values:
        c = make_complaint(
            cast["org"], complainant=cast["bob"], visibility=visibility
        )
        assert policy.can_view(c, cast["dave"]) is False, visibility


# ---------------------------------------------------------------------------
# Respondent access -- spec v4 A1
# ---------------------------------------------------------------------------

def test_respondent_sees_nothing_before_investigation(cast):
    c = make_complaint(
        cast["org"], complainant=cast["bob"], respondent=cast["carol"],
        visibility=enums.Visibility.HR, state=enums.ComplaintState.SUBMITTED,
    )
    assert policy.can_view(c, cast["carol"]) is False


@pytest.mark.parametrize(
    "state",
    [
        enums.ComplaintState.UNDER_INVESTIGATION,
        enums.ComplaintState.AWAITING_DECISION,
        enums.ComplaintState.RESOLVED,
    ],
)
def test_respondent_sees_case_once_investigation_opens(cast, state):
    c = make_complaint(
        cast["org"], complainant=cast["bob"], respondent=cast["carol"],
        visibility=enums.Visibility.HR, state=state,
    )
    assert policy.can_view(c, cast["carol"]) is True


def test_respondent_never_sees_withdrawn_complaint(cast):
    """Withdrawal is only possible pre-investigation, so they never saw it."""
    c = make_complaint(
        cast["org"], complainant=cast["bob"], respondent=cast["carol"],
        visibility=enums.Visibility.HR, state=enums.ComplaintState.WITHDRAWN,
    )
    assert policy.can_view(c, cast["carol"]) is False


def test_complainant_identity_is_masked_from_respondent(cast):
    c = make_complaint(
        cast["org"], complainant=cast["bob"], respondent=cast["carol"],
        state=enums.ComplaintState.UNDER_INVESTIGATION,
    )
    assert policy.should_mask_complainant(c, cast["carol"]) is True

    c.complainant_identity_released = True
    assert policy.should_mask_complainant(c, cast["carol"]) is False


def test_identity_is_not_masked_from_hr_or_complainant(cast):
    c = make_complaint(
        cast["org"], complainant=cast["bob"], respondent=cast["carol"],
        state=enums.ComplaintState.UNDER_INVESTIGATION,
    )
    assert policy.should_mask_complainant(c, cast["hr"]) is False
    assert policy.should_mask_complainant(c, cast["bob"]) is False


# ---------------------------------------------------------------------------
# Q3 hard rule -- the leak this guard exists to close
# ---------------------------------------------------------------------------

def test_respondent_never_gains_access_as_line_manager(cast):
    """Alice manages Bob. Bob complains about Alice.

    Alice must not read a complaint about herself through the line-manager
    route. Before the investigation opens she sees nothing at all.
    """
    c = make_complaint(
        cast["org"], complainant=cast["bob"], respondent=cast["alice"],
        visibility=enums.Visibility.BOTH,
    )
    assert policy.can_view(c, cast["alice"]) is False
    assert policy.can_view(c, cast["hr"]) is True


def test_respondent_who_is_also_line_manager_gets_restricted_access_only(cast):
    """The case the guard actually exists for.

    Once the investigation opens, Alice can see the complaint -- but as the
    respondent, not as the line manager. She must not get the full view, and
    the complainant must stay masked.
    """
    c = make_complaint(
        cast["org"], complainant=cast["bob"], respondent=cast["alice"],
        visibility=enums.Visibility.BOTH,
        state=enums.ComplaintState.UNDER_INVESTIGATION,
    )
    assert policy.can_view(c, cast["alice"]) is True
    assert policy.access_level(c, cast["alice"]) is AccessLevel.RESTRICTED
    assert policy.should_mask_complainant(c, cast["alice"]) is True

    # Contrast: a line manager who is not the respondent gets the full view.
    other = make_complaint(
        cast["org"], complainant=cast["bob"], respondent=cast["carol"],
        visibility=enums.Visibility.BOTH,
        state=enums.ComplaintState.UNDER_INVESTIGATION,
    )
    assert policy.access_level(other, cast["alice"]) is AccessLevel.FULL


def test_hr_who_is_the_respondent_gets_no_privileged_access(cast):
    """Being HR must not grant a better view of a complaint about yourself.

    Regression: `visible_queryset` previously matched the HR role branch for a
    respondent, so an HR user saw a SUBMITTED complaint about themselves in a
    list while the detail endpoint refused it.
    """
    submitted = make_complaint(
        cast["org"], complainant=cast["bob"], respondent=cast["hr"],
        visibility=enums.Visibility.HR, state=enums.ComplaintState.SUBMITTED,
    )
    assert policy.can_view(submitted, cast["hr"]) is False
    assert not policy.visible_queryset(
        Complaint.objects.all(), cast["hr"]
    ).filter(pk=submitted.pk).exists()

    opened = make_complaint(
        cast["org"], complainant=cast["bob"], respondent=cast["hr"],
        visibility=enums.Visibility.HR,
        state=enums.ComplaintState.UNDER_INVESTIGATION,
    )
    assert policy.access_level(opened, cast["hr"]) is AccessLevel.RESTRICTED
    assert policy.should_mask_complainant(opened, cast["hr"]) is True


def test_manager_who_later_becomes_respondent_loses_access(cast):
    """Access is recomputed on every read, so it revokes as well as grants."""
    c = make_complaint(
        cast["org"], complainant=cast["bob"], respondent=cast["carol"],
        visibility=enums.Visibility.BOTH,
    )
    assert policy.can_view(c, cast["alice"]) is True

    c.respondent = cast["alice"]
    c.save(update_fields=["respondent"])
    c.refresh_from_db()
    assert policy.can_view(c, cast["alice"]) is False


def test_access_follows_the_current_manager(cast):
    """Q3: access follows the employee's current manager, not a frozen one."""
    c = make_complaint(
        cast["org"], complainant=cast["bob"], visibility=enums.Visibility.LINE_MANAGER
    )
    assert policy.can_view(c, cast["alice"]) is True

    new_manager = make_employee(cast["org"], "Erin")
    cast["bob"].line_manager = new_manager
    cast["bob"].save(update_fields=["line_manager"])
    c.refresh_from_db()

    assert policy.can_view(c, cast["alice"]) is False
    assert policy.can_view(c, new_manager) is True


# ---------------------------------------------------------------------------
# Orphan fallback -- otherwise a complaint becomes invisible to everyone
# ---------------------------------------------------------------------------

def test_complaint_with_no_manager_falls_back_to_hr(cast):
    loner = make_employee(cast["org"], "Loner")  # no line manager
    c = make_complaint(
        cast["org"], complainant=loner, visibility=enums.Visibility.LINE_MANAGER
    )
    assert is_orphaned(c) is True
    assert policy.can_view(c, cast["hr"]) is True


def test_complaint_about_the_manager_falls_back_to_hr(cast):
    c = make_complaint(
        cast["org"], complainant=cast["bob"], respondent=cast["alice"],
        visibility=enums.Visibility.LINE_MANAGER,
    )
    assert is_orphaned(c) is True
    assert policy.can_view(c, cast["alice"]) is False
    assert policy.can_view(c, cast["hr"]) is True


def test_non_orphaned_line_manager_complaint_stays_hidden_from_hr(cast):
    c = make_complaint(
        cast["org"], complainant=cast["bob"], respondent=cast["carol"],
        visibility=enums.Visibility.LINE_MANAGER,
    )
    assert is_orphaned(c) is False
    assert policy.can_view(c, cast["hr"]) is False


# ---------------------------------------------------------------------------
# Intake override
# ---------------------------------------------------------------------------

def test_intake_override_forces_hr_when_complaint_is_about_your_manager(cast):
    effective = resolve_effective_visibility(
        requested=enums.Visibility.LINE_MANAGER,
        subject_type=enums.SubjectType.EMPLOYEE,
        complainant=cast["bob"],
        respondent=cast["alice"],
    )
    assert effective == enums.Visibility.HR


def test_intake_override_leaves_other_selections_alone(cast):
    unchanged = resolve_effective_visibility(
        requested=enums.Visibility.LINE_MANAGER,
        subject_type=enums.SubjectType.EMPLOYEE,
        complainant=cast["bob"],
        respondent=cast["carol"],
    )
    assert unchanged == enums.Visibility.LINE_MANAGER

    both = resolve_effective_visibility(
        requested=enums.Visibility.BOTH,
        subject_type=enums.SubjectType.EMPLOYEE,
        complainant=cast["bob"],
        respondent=cast["alice"],
    )
    assert both == enums.Visibility.BOTH


# ---------------------------------------------------------------------------
# Other roles
# ---------------------------------------------------------------------------

def test_investigation_lead_sees_the_case_regardless_of_visibility(cast):
    c = make_complaint(
        cast["org"], complainant=cast["bob"], visibility=enums.Visibility.HR
    )
    assert policy.can_view(c, cast["dave"]) is False

    make_investigation(c, cast["dave"])
    c.refresh_from_db()
    assert policy.can_view(c, cast["dave"]) is True


def test_complainant_always_sees_their_own_complaint(cast):
    for visibility in enums.Visibility.values:
        c = make_complaint(
            cast["org"], complainant=cast["bob"], visibility=visibility
        )
        assert policy.can_view(c, cast["bob"]) is True, visibility


def test_hr_filing_for_company_is_visible_to_hr(cast):
    c = make_complaint(
        cast["org"], complainant=None, filed_by=cast["hr"],
        respondent=cast["carol"], visibility=enums.Visibility.HR,
        source=enums.ComplaintSource.HR_FOR_COMPANY,
    )
    assert policy.can_view(c, cast["hr"]) is True


def test_cross_tenant_access_is_refused(cast):
    """Checked before anything that could grant access."""
    other_org = make_org("Other")
    outsider = make_employee(other_org, "Outsider", is_hr=True)
    c = make_complaint(
        cast["org"], complainant=cast["bob"], visibility=enums.Visibility.BOTH
    )
    assert policy.can_view(c, outsider) is False


def test_no_employee_profile_sees_nothing(cast):
    c = make_complaint(cast["org"], complainant=cast["bob"])
    assert policy.can_view(c, None) is False


# ---------------------------------------------------------------------------
# The test that matters most
# ---------------------------------------------------------------------------

def test_object_and_queryset_rules_never_disagree(cast):
    """can_view() and visible_queryset() must return the same set.

    They are separate implementations -- one Python, one SQL -- so they can
    drift. Drift means a list endpoint returning rows the detail endpoint would
    refuse, which is exactly the leak this policy exists to prevent.

    Builds every combination of visibility, state and respondent shape, then
    checks both paths agree for every actor.
    """
    org = cast["org"]
    people = [cast["alice"], cast["bob"], cast["carol"], cast["hr"], cast["dave"]]

    loner = make_employee(org, "Loner")
    complainants = [cast["bob"], loner]
    # HR and dave included deliberately: an HR user who is the respondent
    # is exactly the shape that exposed the object/queryset divergence.
    respondents = [None, cast["carol"], cast["alice"], cast["hr"], cast["dave"]]

    built = 0
    for visibility in enums.Visibility.values:
        for state in enums.ComplaintState.values:
            for complainant in complainants:
                for respondent in respondents:
                    if respondent is not None and respondent.pk == complainant.pk:
                        continue
                    make_complaint(
                        org, complainant=complainant, respondent=respondent,
                        visibility=visibility, state=state,
                    )
                    built += 1

    assert built > 50, "matrix too small to be meaningful"

    # Give one complaint an investigation lead so that route is covered too.
    lead_case = Complaint.objects.filter(visibility=enums.Visibility.HR).first()
    make_investigation(lead_case, cast["dave"])

    all_complaints = list(Complaint.objects.all())

    for person in people:
        by_object = {c.pk for c in all_complaints if policy.can_view(c, person)}
        by_queryset = set(
            policy.visible_queryset(
                Complaint.objects.all(), person
            ).values_list("pk", flat=True)
        )

        only_object = by_object - by_queryset
        only_queryset = by_queryset - by_object

        assert not only_queryset, (
            f"{person.full_name}: visible_queryset() returned "
            f"{len(only_queryset)} complaint(s) can_view() refuses -- this is a leak"
        )
        assert not only_object, (
            f"{person.full_name}: can_view() allows {len(only_object)} "
            f"complaint(s) visible_queryset() hides -- rows will go missing"
        )
