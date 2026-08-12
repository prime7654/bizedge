"""Write operations on complaints.

Every state change lives here, never in a serializer or a view. Each service
does the same four things in one transaction: validate the guard, mutate,
write the audit row, emit notifications. Keeping that shape uniform is what
makes the audit trail trustworthy -- there is no path that changes a complaint
without recording it.
"""
from __future__ import annotations

from datetime import date

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date

from apps.grievances import enums, notifications, transitions
from apps.grievances.access import resolve_effective_visibility
from apps.grievances.events import record_event
from apps.grievances.models import (
    Complaint,
    ComplaintWitness,
    Investigation,
    InvestigationCollaborator,
)
from apps.grievances.references import next_reference


class ServiceError(Exception):
    """A guard rejected the operation. Views translate this to 4xx."""


@transaction.atomic
def file_complaint(
    *,
    organisation,
    filed_by,
    source: str,
    subject_type: str,
    complaint_type: str,
    description: str,
    visibility: str,
    complainant=None,
    respondent=None,
    complaint_type_note: str = "",
    frequency: str = "",
    occurrence_count: int | None = None,
    incident_date=None,
    witnesses: list[dict] | None = None,
    request=None,
) -> Complaint:
    """Create a complaint.

    Assumes the payload has already been validated by the serializer -- this is
    the transactional side, not a second validation layer. The one piece of
    policy applied here is the intake override, because it must happen whether
    the complaint arrives through the API, the admin, or a data import.
    """
    effective_visibility = resolve_effective_visibility(
        requested=visibility,
        subject_type=subject_type,
        complainant=complainant,
        respondent=respondent,
    )

    complaint = Complaint.objects.create(
        organisation=organisation,
        reference=next_reference(organisation),
        source=source,
        subject_type=subject_type,
        filed_by=filed_by,
        complainant=complainant,
        respondent=respondent,
        complaint_type=complaint_type,
        complaint_type_note=complaint_type_note,
        frequency=frequency,
        occurrence_count=occurrence_count,
        incident_date=incident_date,
        description=description,
        visibility=effective_visibility,
        visibility_requested=visibility,
        state=enums.ComplaintState.SUBMITTED,
        created_by=filed_by,
    )

    for witness in witnesses or []:
        ComplaintWitness.objects.create(
            complaint=complaint,
            witness_type=witness["witness_type"],
            employee=witness.get("employee"),
            department=witness.get("department"),
            added_by=filed_by,
        )

    record_event(
        complaint,
        verb=enums.EventVerb.FILED,
        actor=filed_by,
        to_state=enums.ComplaintState.SUBMITTED,
        payload={"source": source, "visibility": effective_visibility},
        request=request,
    )

    # Record the override separately. It is a decision the system made on the
    # complainant's behalf, and they should be able to see that it happened.
    if effective_visibility != visibility:
        record_event(
            complaint,
            verb=enums.EventVerb.VISIBILITY_OVERRIDDEN,
            actor=filed_by,
            payload={
                "requested": visibility,
                "effective": effective_visibility,
                "reason": "complaint_is_about_the_complainants_line_manager",
            },
            request=request,
        )

    notifications.notify(
        notifications.COMPLAINT_FILED,
        complaint,
        _initial_recipients(complaint),
        reference=complaint.reference,
    )

    return complaint


def _initial_recipients(complaint: Complaint) -> list:
    """Who hears about a newly filed complaint.

    Follows the effective visibility, so a LINE_MANAGER complaint does not
    notify HR. Respondents are never notified at filing -- spec v4 A1, they
    learn nothing until an investigation opens.
    """
    recipients: list = []

    if complaint.visibility in (enums.Visibility.HR, enums.Visibility.BOTH):
        from apps.directory.models import Employee  # local: swappable model

        recipients.extend(
            Employee.objects.filter(
                organisation_id=complaint.organisation_id, is_hr=True, is_active=True
            )
        )

    if complaint.visibility in (enums.Visibility.LINE_MANAGER, enums.Visibility.BOTH):
        complainant = complaint.complainant
        manager = getattr(complainant, "line_manager", None) if complainant else None
        # Never notify the person the complaint is about.
        if manager is not None and manager.pk != complaint.respondent_id:
            recipients.append(manager)

    return recipients


# ---------------------------------------------------------------------------
# Triage
# ---------------------------------------------------------------------------


class ConflictOfInterest(ServiceError):
    """The actor is too close to the case to take this action."""


def _coerce_date(value) -> date | None:
    """Accept a date or an ISO string.

    Services are called from the API, the admin, management commands and data
    imports. Only the first of those guarantees a parsed date, so normalising
    here is cheaper than trusting every caller.
    """
    if value is None or isinstance(value, date):
        return value
    if isinstance(value, str):
        parsed = parse_date(value.strip())
        if parsed is None:
            raise ServiceError(f"{value!r} is not a valid date (expected YYYY-MM-DD).")
        return parsed
    raise ServiceError(f"Unsupported date value: {value!r}")


def _assert_no_conflict(complaint: Complaint, actor, lead) -> None:
    """Refuse triage by, or appointment of, anyone party to the complaint.

    Being HR does not make you neutral about a complaint you are named in.
    Nothing in the product prevents an HR user opening a case about
    themselves, so it is prevented here.

    Deliberately covers the actor *and* the proposed lead: appointing the
    respondent as their own investigator is the same problem wearing a
    different hat.
    """
    parties = {
        pk
        for pk in (complaint.complainant_id, complaint.respondent_id)
        if pk is not None
    }

    if actor is not None and actor.pk in parties:
        raise ConflictOfInterest(
            "You are named in this complaint and cannot triage it. "
            "Ask another HR colleague to take it."
        )
    if lead is not None and lead.pk in parties:
        raise ConflictOfInterest(
            "That person is named in this complaint and cannot investigate it."
        )


@transaction.atomic
def appoint_investigator(
    *,
    complaint: Complaint,
    actor,
    lead,
    due_date: date | None = None,
    request=None,
) -> Investigation:
    """Open a case by appointing an investigation lead.

    The pivotal moment in the lifecycle. Several things change at once, and
    they must all happen together or not at all:

    * the complaint moves to UNDER_INVESTIGATION
    * ``due_date`` is set -- HR sets it here, per B3, and it is null before
    * the respondent can see the case for the first time (spec v4 A1)
    * the deletion window closes permanently (Q5)
    * collaborators are seeded from the complainant and named witnesses
    * the lead is notified with a link straight to the case

    Per Q4 an HR user may appoint themselves, which is how a minor complaint is
    handled without pulling in a separate investigator. Per B4 there is no
    acceptance step -- the appointment takes effect immediately.

    Idempotent by state: a second call on an already-open complaint raises
    rather than creating a second round. That matters because this sits behind
    a slow modal and is a natural double-click.
    """
    # Lock first, then check. Reading the state before locking means two
    # concurrent callers can both see SUBMITTED and both proceed.
    complaint = Complaint.objects.select_for_update().get(pk=complaint.pk)
    transition = transitions.check(complaint, "appoint_investigator")

    if lead is None:
        raise ServiceError("Select who will investigate this complaint.")
    if lead.organisation_id != complaint.organisation_id:
        raise ServiceError("That person is not in this organisation.")

    _assert_no_conflict(complaint, actor, lead)

    due_date = _coerce_date(due_date)
    previous_state = complaint.state
    investigation = Investigation.objects.create(
        complaint=complaint,
        round=1,
        lead=lead,
        lead_is_hr=bool(getattr(lead, "is_hr", False)),
        invited_by=actor,
        invited_at=timezone.now(),
        start_date=timezone.localdate(),
        state=enums.InvestigationState.IN_PROGRESS,
    )

    complaint.state = transition.target
    complaint.due_date = due_date
    complaint.updated_by = actor
    complaint.save(update_fields=["state", "due_date", "updated_by", "updated_at"])

    _seed_collaborators(investigation, complaint, invited_by=actor)

    record_event(
        complaint,
        verb=enums.EventVerb.INVESTIGATOR_APPOINTED,
        actor=actor,
        from_state=previous_state,
        to_state=complaint.state,
        payload={
            "lead_id": str(lead.pk),
            "lead_name": lead.full_name,
            "self_assigned": actor is not None and actor.pk == lead.pk,
            "due_date": due_date.isoformat() if due_date else None,
        },
        request=request,
    )

    notifications.notify(
        notifications.INVESTIGATOR_APPOINTED,
        complaint,
        [lead],
        reference=complaint.reference,
        due_date=due_date,
    )

    return investigation


def _seed_collaborators(investigation, complaint, *, invited_by) -> None:
    """Pre-populate the collaborator list.

    The complainant and any named witnesses are on the case from the start --
    the lead should not have to re-add people the complaint already names.

    The respondent is deliberately *not* seeded. Whether to involve them, and
    when, is the lead's judgement call, not an automatic consequence of the
    case opening.
    """
    seeded: set = set()

    if complaint.complainant_id:
        InvestigationCollaborator.objects.create(
            investigation=investigation,
            employee_id=complaint.complainant_id,
            role=enums.CollaboratorRole.COMPLAINANT,
            invited_by=invited_by,
            invited_at=timezone.now(),
            status=enums.CollaboratorStatus.ACTIVE,
        )
        seeded.add(complaint.complainant_id)

    for witness in complaint.witnesses.select_related("employee"):
        employee_id = witness.employee_id

        # "My line manager" resolves at seeding time to whoever that is now.
        if witness.witness_type == enums.WitnessType.LINE_MANAGER:
            complainant = complaint.complainant
            employee_id = getattr(complainant, "line_manager_id", None)

        # Departments name a group, not a person -- nothing to seed.
        if employee_id is None or employee_id in seeded:
            continue
        # Never quietly enrol the respondent as a witness against themselves.
        if employee_id == complaint.respondent_id:
            continue

        InvestigationCollaborator.objects.create(
            investigation=investigation,
            employee_id=employee_id,
            role=enums.CollaboratorRole.WITNESS,
            invited_by=invited_by,
            invited_at=timezone.now(),
            status=enums.CollaboratorStatus.INVITED,
        )
        seeded.add(employee_id)
