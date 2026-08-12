"""Who can see which complaint.

Spec v4 section 2 and section 5. This module is the single source of truth for
visibility. Views, serializers and services must all defer to it -- nowhere
else in the codebase should decide whether someone may see a complaint.

Two entry points that must always agree:

* :meth:`ComplaintAccessPolicy.can_view` -- one complaint, one person.
* :meth:`ComplaintAccessPolicy.visible_queryset` -- filter a list.

They are separate because one runs in Python and the other in SQL, and that
duplication is the risk this module exists to contain: if they drift, a list
endpoint starts returning complaints the detail endpoint would refuse. There
is a test asserting they agree across the whole matrix. Change one, change the
other, and run that test.

The rule people find surprising: **HR does not see LINE_MANAGER complaints.**
That is deliberate and confirmed by Product. Any "all complaints" metric will
legitimately under-report.
"""
from __future__ import annotations

from enum import Enum

from django.db.models import F, Q, QuerySet

from apps.grievances import enums
from apps.grievances.models import Complaint

#: States in which the respondent may see a complaint filed against them.
#: Spec v4 A1: nothing is visible to them until HR opens an investigation.
#: WITHDRAWN is excluded because a complaint can only be withdrawn while
#: SUBMITTED, so the respondent never saw it in the first place.
RESPONDENT_VISIBLE_STATES = frozenset(
    {
        enums.ComplaintState.UNDER_INVESTIGATION,
        enums.ComplaintState.AWAITING_DECISION,
        enums.ComplaintState.RESOLVED,
    }
)

#: Visibility values that grant a line manager access.
LINE_MANAGER_VISIBILITIES = frozenset(
    {enums.Visibility.LINE_MANAGER, enums.Visibility.BOTH}
)

#: Visibility values that grant HR access directly.
HR_VISIBILITIES = frozenset({enums.Visibility.HR, enums.Visibility.BOTH})


# ---------------------------------------------------------------------------
# Orphan detection
# ---------------------------------------------------------------------------
#
# A LINE_MANAGER complaint can end up visible to nobody:
#
#   * the complainant has no manager on record (senior staff, vacancy)
#   * HR filed on behalf of the company, so there is no complainant at all
#   * the manager later became the respondent, and the Q3 guard excludes them
#   * the employee is somehow their own manager (bad org-chart data)
#
# Without a fallback the complaint silently becomes invisible to every human in
# the system. Rule: if nobody can see it via the line-manager route, HR sees it.

#: SQL form of the orphan test.
ORPHANED_Q = (
    Q(complainant__isnull=True)
    | Q(complainant__line_manager__isnull=True)
    | Q(complainant__line_manager=F("respondent"))
    | Q(complainant__line_manager=F("complainant"))
)


def is_orphaned(complaint: Complaint) -> bool:
    """Python form of :data:`ORPHANED_Q`. Keep the two in step."""
    complainant = complaint.complainant
    if complainant is None:
        return True

    manager_id = complainant.line_manager_id
    if manager_id is None:
        return True
    if complaint.respondent_id is not None and manager_id == complaint.respondent_id:
        return True
    if manager_id == complainant.pk:
        return True
    return False


def resolve_effective_visibility(
    *,
    requested: str,
    subject_type: str,
    complainant,
    respondent,
) -> str:
    """Apply the intake override from spec v4 section 2.

    A complaint about your own line manager must not route to that manager.
    When someone files against their manager and picks LINE_MANAGER, the
    complaint is forced to HR.

    BOTH needs no override: the respondent guard already excludes the manager,
    leaving HR with access.

    Callers should store the original selection in ``visibility_requested`` and
    the return value in ``visibility``. Audit needs to show what the complainant
    actually chose, not only what the system did with it.
    """
    if requested != enums.Visibility.LINE_MANAGER:
        return requested
    if subject_type != enums.SubjectType.EMPLOYEE:
        return requested
    if complainant is None or respondent is None:
        return requested
    if complainant.line_manager_id and complainant.line_manager_id == respondent.pk:
        return enums.Visibility.HR
    return requested


class AccessLevel(str, Enum):
    """How much of a complaint someone may see.

    Views must branch on this rather than on "is this person HR?". A
    respondent who also happens to be HR or the complainant's line manager
    still gets RESTRICTED -- being the subject of a complaint never earns you
    a better view of it.
    """

    NONE = "NONE"
    RESTRICTED = "RESTRICTED"
    FULL = "FULL"


class ComplaintAccessPolicy:
    """Read access to complaints.

    All methods take an ``Employee``, never a ``User``. Views resolve the
    employee profile first; anonymous or profile-less users see nothing.
    """

    # -- object level ------------------------------------------------------

    @staticmethod
    def can_view(complaint: Complaint, employee) -> bool:
        """Return True if ``employee`` may see ``complaint`` at all.

        Seeing a complaint is not the same as seeing all of it -- a respondent
        gets a restricted view. Use :meth:`should_mask_complainant` alongside
        this.
        """
        if employee is None:
            return False

        # Tenant boundary first. A cross-tenant read is a serious incident, so
        # it is checked before anything that could grant access.
        if complaint.organisation_id != employee.organisation_id:
            return False

        # The person who raised it, and whoever submitted the form for them.
        if complaint.complainant_id == employee.pk:
            return True
        if complaint.filed_by_id == employee.pk:
            return True

        # The person it is about -- but only once HR has opened an
        # investigation, and never before.
        #
        # This deliberately short-circuits: being the respondent caps your
        # access, it never adds to it. An HR user or line manager who is the
        # subject of a complaint gets respondent-level access to it and
        # nothing more. `visible_queryset` mirrors this exactly -- change one,
        # change the other.
        if complaint.respondent_id == employee.pk:
            return complaint.state in RESPONDENT_VISIBLE_STATES

        # The investigation lead, for any round.
        if complaint.investigations.filter(lead_id=employee.pk).exists():
            return True

        if employee.is_hr:
            if complaint.visibility in HR_VISIBILITIES:
                return True
            # Orphan fallback: a LINE_MANAGER complaint nobody else can see.
            return (
                complaint.visibility == enums.Visibility.LINE_MANAGER
                and is_orphaned(complaint)
            )

        # Line manager route.
        if complaint.visibility in LINE_MANAGER_VISIBILITIES:
            complainant = complaint.complainant
            if complainant is None:
                return False
            if complainant.line_manager_id != employee.pk:
                return False
            # Q3 hard rule: a respondent never gains line-manager visibility of
            # a complaint against themselves.
            if complaint.respondent_id == employee.pk:
                return False
            return True

        return False

    @classmethod
    def access_level(cls, complaint: Complaint, employee) -> "AccessLevel":
        """How much of ``complaint`` ``employee`` may see.

        Prefer this over calling :meth:`can_view` and then choosing a
        serializer by role -- that is how a respondent who is also HR ends up
        with the full payload.
        """
        if not cls.can_view(complaint, employee):
            return AccessLevel.NONE
        if complaint.respondent_id == employee.pk:
            return AccessLevel.RESTRICTED
        return AccessLevel.FULL

    @staticmethod
    def should_mask_complainant(complaint: Complaint, employee) -> bool:
        """Return True if the complainant's identity must be hidden.

        Spec v4 A1: the respondent sees the allegation and the date but not who
        filed it, unless HR has explicitly released the identity.
        """
        if employee is None:
            return True
        if complaint.respondent_id != employee.pk:
            return False
        return not complaint.complainant_identity_released

    # -- queryset level ----------------------------------------------------

    @staticmethod
    def visible_queryset(queryset: QuerySet[Complaint], employee) -> QuerySet[Complaint]:
        """Filter ``queryset`` to what ``employee`` may see.

        Must produce exactly the set of rows :meth:`can_view` would approve.
        Call this in ``get_queryset()``; never hand a view an unfiltered
        ``Complaint.objects.all()``.
        """
        if employee is None:
            return queryset.none()

        queryset = queryset.filter(organisation_id=employee.organisation_id)

        if employee.is_hr:
            role_based = Q(visibility__in=list(HR_VISIBILITIES)) | (
                Q(visibility=enums.Visibility.LINE_MANAGER) & ORPHANED_Q
            )
        else:
            role_based = (
                Q(visibility__in=list(LINE_MANAGER_VISIBILITIES))
                & Q(complainant__line_manager_id=employee.pk)
                & ~Q(respondent_id=employee.pk)
            )

        # Mirror of the respondent short-circuit in can_view(). Being the
        # respondent caps access; every other route is suppressed for them.
        # Without this a respondent who is also HR sees, in a list, a complaint
        # about themselves that the detail endpoint would refuse.
        respondent_route = Q(
            respondent_id=employee.pk, state__in=list(RESPONDENT_VISIBLE_STATES)
        )
        other_routes = (
            Q(complainant_id=employee.pk)
            | Q(filed_by_id=employee.pk)
            | Q(investigations__lead_id=employee.pk)
            | role_based
        ) & ~Q(respondent_id=employee.pk)

        # distinct() because the investigations join can duplicate rows when a
        # complaint has been reopened and has more than one round.
        return queryset.filter(respondent_route | other_routes).distinct()


def employee_for(user):
    """Resolve the Employee profile for a Django user, or None.

    Anonymous users and users without a profile see nothing. Returning None
    rather than raising keeps the policy total -- every caller handles it the
    same way.
    """
    if user is None or not user.is_authenticated:
        return None
    return getattr(user, "employee_profile", None)
