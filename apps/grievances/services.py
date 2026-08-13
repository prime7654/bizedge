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
    InformationRequest,
    InformationResponse,
    Investigation,
    InvestigationCollaborator,
    InvestigationMeeting,
    PIPFollowUp,
    PIPPlan,
    PIPTrainingAssignment,
    Resolution,
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


# ---------------------------------------------------------------------------
# Investigation
# ---------------------------------------------------------------------------

#: Bounds on free text. Long enough for a real answer, short enough that a
#: paste-bomb cannot fill the database or the audit trail.
MAX_PROMPT_CHARS = 5_000
MAX_RESPONSE_CHARS = 20_000
MAX_FINDINGS_CHARS = 50_000


def _require_text(value: str, field: str, limit: int) -> str:
    """Validate free text at the service boundary, not only in the serializer.

    Services are reachable from the admin and management commands, which do not
    run serializer validation.
    """
    value = (value or "").strip()
    if not value:
        raise ServiceError(f"{field} cannot be empty.")
    if len(value) > limit:
        raise ServiceError(
            f"{field} is too long ({len(value)} characters, limit {limit})."
        )
    return value


@transaction.atomic
def invite_collaborator(
    *,
    investigation: Investigation,
    actor,
    employee,
    role: str = enums.CollaboratorRole.OTHER,
    request=None,
) -> InvestigationCollaborator:
    """Add someone to an investigation.

    Idempotent by design. A duplicate invite returns the existing collaborator
    rather than raising, because the unique constraint would otherwise surface
    as a 500 on a double-click, and re-inviting someone is a harmless intent.
    Re-inviting a previously removed collaborator reactivates them.
    """
    if employee is None:
        raise ServiceError("Select someone to invite.")
    if employee.organisation_id != investigation.complaint.organisation_id:
        raise ServiceError("That person is not in this organisation.")
    if role not in enums.CollaboratorRole.values:
        raise ServiceError(f"Unknown collaborator role: {role!r}")

    existing = InvestigationCollaborator.objects.filter(
        investigation=investigation, employee=employee
    ).first()
    if existing is not None:
        if existing.status == enums.CollaboratorStatus.REMOVED:
            existing.status = enums.CollaboratorStatus.INVITED
            existing.invited_by = actor
            existing.invited_at = timezone.now()
            existing.save(update_fields=["status", "invited_by", "invited_at"])
            record_event(
                investigation.complaint,
                verb=enums.EventVerb.COLLABORATOR_INVITED,
                actor=actor,
                payload={"employee_id": str(employee.pk), "reinstated": True},
                request=request,
            )
        return existing

    collaborator = InvestigationCollaborator.objects.create(
        investigation=investigation,
        employee=employee,
        role=role,
        invited_by=actor,
        invited_at=timezone.now(),
        status=enums.CollaboratorStatus.INVITED,
    )

    record_event(
        investigation.complaint,
        verb=enums.EventVerb.COLLABORATOR_INVITED,
        actor=actor,
        payload={"employee_id": str(employee.pk), "role": role},
        request=request,
    )
    notifications.notify(
        notifications.COLLABORATOR_INVITED,
        investigation.complaint,
        [employee],
        reference=investigation.complaint.reference,
    )
    return collaborator


@transaction.atomic
def remove_collaborator(
    *, collaborator: InvestigationCollaborator, actor, request=None
) -> InvestigationCollaborator:
    """Take someone off an investigation.

    Soft removal. The audit trail has to show who was involved even after they
    were taken off, and their answers stay on the record -- evidence already
    given does not become un-given.

    Any question still outstanding for them is expired, so it stops appearing
    in an inbox they can no longer act on.
    """
    collaborator.status = enums.CollaboratorStatus.REMOVED
    collaborator.save(update_fields=["status"])

    expired = InformationRequest.objects.filter(
        collaborator=collaborator, status=enums.InformationRequestStatus.PENDING
    ).update(status=enums.InformationRequestStatus.EXPIRED)

    record_event(
        collaborator.investigation.complaint,
        verb=enums.EventVerb.COLLABORATOR_REMOVED,
        actor=actor,
        payload={
            "employee_id": str(collaborator.employee_id),
            "expired_requests": expired,
        },
        request=request,
    )
    return collaborator


@transaction.atomic
def request_information(
    *,
    collaborator: InvestigationCollaborator,
    actor,
    prompt: str,
    due_at=None,
    request=None,
) -> InformationRequest:
    """Put a written question to a collaborator.

    Always creates a new request rather than editing the last one, so
    "Request Additional Information" produces a dated thread. Overwriting would
    destroy the record of what was originally asked.
    """
    investigation = collaborator.investigation
    if investigation.state != enums.InvestigationState.IN_PROGRESS:
        raise ServiceError(
            "This investigation is no longer in progress, so no further "
            "information can be requested."
        )
    if collaborator.status == enums.CollaboratorStatus.REMOVED:
        raise ServiceError(
            "That person has been removed from this investigation. "
            "Re-invite them first."
        )

    prompt = _require_text(prompt, "The question", MAX_PROMPT_CHARS)

    info_request = InformationRequest.objects.create(
        investigation=investigation,
        collaborator=collaborator,
        prompt=prompt,
        requested_by=actor,
        due_at=due_at,
        status=enums.InformationRequestStatus.PENDING,
    )

    if collaborator.status == enums.CollaboratorStatus.INVITED:
        collaborator.status = enums.CollaboratorStatus.ACTIVE
        collaborator.save(update_fields=["status"])

    record_event(
        investigation.complaint,
        verb=enums.EventVerb.INFORMATION_REQUESTED,
        actor=actor,
        payload={
            "collaborator_id": str(collaborator.pk),
            "request_id": str(info_request.pk),
        },
        request=request,
    )
    notifications.notify(
        notifications.INFORMATION_REQUESTED,
        investigation.complaint,
        [collaborator.employee],
        reference=investigation.complaint.reference,
    )
    return info_request


@transaction.atomic
def respond_to_request(
    *, info_request: InformationRequest, actor, body: str, request=None
) -> InformationResponse:
    """Answer a question.

    Locks the request before checking its status: a double-submitted answer
    would otherwise create two responses and mark the request answered twice.
    """
    info_request = InformationRequest.objects.select_for_update().get(
        pk=info_request.pk
    )
    if info_request.status != enums.InformationRequestStatus.PENDING:
        raise ServiceError("This request has already been answered or has expired.")

    body = _require_text(body, "Your response", MAX_RESPONSE_CHARS)

    response = InformationResponse.objects.create(
        request=info_request, body=body, responded_by=actor
    )
    info_request.status = enums.InformationRequestStatus.ANSWERED
    info_request.save(update_fields=["status"])

    complaint = info_request.investigation.complaint
    record_event(
        complaint,
        verb=enums.EventVerb.INFORMATION_RECEIVED,
        actor=actor,
        payload={"request_id": str(info_request.pk)},
        request=request,
    )
    notifications.notify(
        notifications.INFORMATION_RECEIVED,
        complaint,
        [info_request.investigation.lead],
        reference=complaint.reference,
    )
    return response


@transaction.atomic
def record_meeting(
    *,
    investigation: Investigation,
    actor,
    meeting_date,
    findings: str,
    attendee_employees: list | None = None,
    request=None,
) -> InvestigationMeeting:
    """Record a meeting and its findings.

    Per C1 the attendee picker searches the whole organisation, so anyone named
    who is not yet on the case is added as a collaborator. Otherwise the record
    ends up pointing at people the investigation does not know about.
    """
    meeting_date = _coerce_date(meeting_date)
    if meeting_date is None:
        raise ServiceError("Enter the date of the meeting.")
    if meeting_date > timezone.localdate():
        raise ServiceError("A meeting cannot be recorded with a future date.")
    if investigation.state != enums.InvestigationState.IN_PROGRESS:
        raise ServiceError("This investigation is no longer in progress.")

    findings = _require_text(findings, "The findings", MAX_FINDINGS_CHARS)

    meeting = InvestigationMeeting.objects.create(
        investigation=investigation,
        meeting_date=meeting_date,
        findings=findings,
        recorded_by=actor,
    )

    for employee in attendee_employees or []:
        collaborator = invite_collaborator(
            investigation=investigation,
            actor=actor,
            employee=employee,
            role=enums.CollaboratorRole.OTHER,
            request=request,
        )
        meeting.attendees.add(collaborator)

    record_event(
        investigation.complaint,
        verb=enums.EventVerb.MEETING_RECORDED,
        actor=actor,
        payload={
            "meeting_id": str(meeting.pk),
            "attendees": len(attendee_employees or []),
        },
        request=request,
    )
    return meeting


@transaction.atomic
def submit_report(*, investigation: Investigation, actor, request=None) -> Investigation:
    """Close the investigation and hand the case back to HR for a decision.

    Locked and guarded on both the investigation and the complaint: this sits
    behind a slow modal and is a natural double-click, and submitting twice
    would move an already-decided case backwards.

    Deliberately does *not* require any minimum amount of recorded work. An
    investigation that found nothing is a legitimate outcome, and a server-side
    quota on meetings or notes would only encourage padding.
    """
    investigation = Investigation.objects.select_for_update().get(pk=investigation.pk)
    if investigation.state == enums.InvestigationState.REPORT_SUBMITTED:
        raise transitions.TransitionError(
            "This investigation report has already been submitted."
        )

    complaint = Complaint.objects.select_for_update().get(
        pk=investigation.complaint_id
    )
    transition = transitions.check(complaint, "submit_report")

    previous_state = complaint.state
    now = timezone.now()

    investigation.state = enums.InvestigationState.REPORT_SUBMITTED
    investigation.report_submitted_at = now
    investigation.report_submitted_by = actor
    investigation.save(
        update_fields=["state", "report_submitted_at", "report_submitted_by"]
    )

    complaint.state = transition.target
    complaint.updated_by = actor
    complaint.save(update_fields=["state", "updated_by", "updated_at"])

    # Nothing further can be asked once the report is in.
    InformationRequest.objects.filter(
        investigation=investigation, status=enums.InformationRequestStatus.PENDING
    ).update(status=enums.InformationRequestStatus.EXPIRED)

    record_event(
        complaint,
        verb=enums.EventVerb.REPORT_SUBMITTED,
        actor=actor,
        from_state=previous_state,
        to_state=complaint.state,
        payload={"investigation_id": str(investigation.pk), "round": investigation.round},
        request=request,
    )
    notifications.notify(
        notifications.REPORT_SUBMITTED,
        complaint,
        _hr_recipients(complaint),
        reference=complaint.reference,
    )
    return investigation


def _hr_recipients(complaint: Complaint) -> list:
    from apps.directory.models import Employee  # local: swappable model

    return list(
        Employee.objects.filter(
            organisation_id=complaint.organisation_id, is_hr=True, is_active=True
        )
    )


# ---------------------------------------------------------------------------
# Decision and resolution
# ---------------------------------------------------------------------------

MAX_DECISION_NOTES_CHARS = 20_000

#: A PIP running longer than this is almost certainly a typo in the year.
MAX_PIP_DAYS = 730


def _validate_resolution_shape(
    *, resolution_type: str, formal_type: str, informal_type: str, note: str
) -> None:
    """Enforce the formal/informal rules before touching the database.

    These mirror check constraints on the Resolution table. Duplicating them
    here is deliberate: the constraint is the backstop that guarantees the data
    is sound, but hitting it produces an IntegrityError and a 500. Callers
    deserve a field error instead.
    """
    if resolution_type == enums.ResolutionType.FORMAL:
        if not formal_type:
            raise ServiceError("Select the type of formal resolution.")
        if informal_type:
            raise ServiceError(
                "A formal resolution cannot also carry an informal type."
            )
    elif resolution_type == enums.ResolutionType.INFORMAL:
        if not informal_type:
            raise ServiceError("Select the type of informal resolution.")
        if formal_type:
            raise ServiceError(
                "An informal resolution cannot also carry a formal type."
            )
    elif resolution_type == enums.ResolutionType.NO_RESOLUTION_REQUIRED:
        if formal_type or informal_type:
            raise ServiceError(
                "No resolution required means no resolution type is set."
            )
    else:
        raise ServiceError(f"Unknown resolution type: {resolution_type!r}")

    chose_others = enums.FormalResolutionType.OTHERS in (formal_type,) or (
        enums.InformalResolutionType.OTHERS == informal_type
    )
    if chose_others and not (note or "").strip():
        raise ServiceError("Describe the resolution when choosing 'Others'.")


@transaction.atomic
def resolve_complaint(
    *,
    complaint: Complaint,
    actor,
    decision: str,
    resolution_type: str,
    decision_notes: str,
    formal_resolution_type: str = "",
    informal_resolution_type: str = "",
    resolution_note: str = "",
    pip: dict | None = None,
    request=None,
) -> Resolution:
    """Record HR's decision and close the case.

    One call, one transaction. The alternative -- letting the client create the
    resolution, then the PIP, then the follow-ups -- means a network drop
    halfway leaves a PIP attached to a complaint that is still open, and
    nothing in the system to say which half ran.

    ``pip`` is optional and shaped::

        {"start_date": ..., "end_date": ...,
         "trainings": [<Training>, ...],
         "follow_ups": [{"scheduled_date": ..., "kind": ...,
                         "reminder_enabled": bool, "reminder_time": ...}]}
    """
    complaint = Complaint.objects.select_for_update().get(pk=complaint.pk)
    transition = transitions.check(complaint, "resolve")

    # An HR user named in the complaint must not be the one deciding it.
    _assert_no_conflict(complaint, actor, None)

    investigation = complaint.investigations.order_by("-round").first()
    if investigation is None:
        # Should be unreachable given the state machine, but the invariant is
        # worth asserting: Q4 requires an investigator on record before close.
        raise ServiceError(
            "This complaint has no investigation on record and cannot be closed."
        )
    if hasattr(investigation, "resolution"):
        raise transitions.TransitionError(
            "This investigation round has already been resolved."
        )

    if decision not in enums.InvestigationDecision.values:
        raise ServiceError(f"Unknown investigation decision: {decision!r}")

    _validate_resolution_shape(
        resolution_type=resolution_type,
        formal_type=formal_resolution_type,
        informal_type=informal_resolution_type,
        note=resolution_note,
    )
    decision_notes = _require_text(
        decision_notes, "Decision notes", MAX_DECISION_NOTES_CHARS
    )

    previous_state = complaint.state

    resolution = Resolution.objects.create(
        complaint=complaint,
        investigation=investigation,
        decision=decision,
        resolution_type=resolution_type,
        formal_resolution_type=formal_resolution_type,
        informal_resolution_type=informal_resolution_type,
        resolution_note=(resolution_note or "").strip(),
        decision_notes=decision_notes,
        decided_by=actor,
    )

    if pip:
        _create_pip(resolution=resolution, complaint=complaint, actor=actor, spec=pip)

    complaint.state = transition.target
    complaint.updated_by = actor
    complaint.save(update_fields=["state", "updated_by", "updated_at"])

    record_event(
        complaint,
        verb=enums.EventVerb.RESOLVED,
        actor=actor,
        from_state=previous_state,
        to_state=complaint.state,
        payload={
            "decision": decision,
            "resolution_type": resolution_type,
            "formal_resolution_type": formal_resolution_type,
            "informal_resolution_type": informal_resolution_type,
            "pip_created": bool(pip),
        },
        request=request,
    )

    # The respondent learns the outcome; the complainant learns it closed.
    # Both are notified, neither is told anything the other said.
    notifications.notify(
        notifications.COMPLAINT_RESOLVED,
        complaint,
        [e for e in (complaint.complainant, complaint.respondent) if e is not None],
        reference=complaint.reference,
        decision=decision,
    )
    return resolution


def _create_pip(*, resolution: Resolution, complaint: Complaint, actor, spec: dict):
    """Build the PIP, its training assignments and its follow-up schedule.

    Runs inside the caller's transaction, so a bad follow-up date rolls the
    whole close back rather than leaving a half-built plan behind.
    """
    employee = complaint.respondent
    if employee is None:
        raise ServiceError(
            "A performance improvement plan needs someone to apply to, and this "
            "complaint does not name a respondent."
        )

    start = _coerce_date(spec.get("start_date"))
    end = _coerce_date(spec.get("end_date"))
    if start is None or end is None:
        raise ServiceError("A PIP needs both a start and an end date.")
    if end < start:
        raise ServiceError("The PIP cannot end before it starts.")
    if (end - start).days > MAX_PIP_DAYS:
        raise ServiceError(
            f"A PIP cannot run longer than {MAX_PIP_DAYS} days "
            f"(got {(end - start).days})."
        )

    plan = PIPPlan.objects.create(
        resolution=resolution,
        employee=employee,
        start_date=start,
        end_date=end,
        state=enums.PIPState.ACTIVE,
        created_by=actor,
    )

    for training in spec.get("trainings") or []:
        if training.organisation_id != complaint.organisation_id:
            raise ServiceError("That training is not available in this organisation.")
        PIPTrainingAssignment.objects.create(pip_plan=plan, training=training)

    for follow_up in spec.get("follow_ups") or []:
        scheduled = _coerce_date(follow_up.get("scheduled_date"))
        if scheduled is None:
            raise ServiceError("Each follow-up needs a date.")
        if scheduled < start:
            raise ServiceError(
                f"A follow-up on {scheduled} falls before the PIP starts on {start}."
            )
        PIPFollowUp.objects.create(
            pip_plan=plan,
            scheduled_date=scheduled,
            kind=follow_up.get("kind") or enums.FollowUpKind.CUSTOM,
            reminder_enabled=bool(follow_up.get("reminder_enabled", True)),
            reminder_time=follow_up.get("reminder_time"),
        )

    return plan


# ---------------------------------------------------------------------------
# Withdrawal
# ---------------------------------------------------------------------------

MAX_WITHDRAWAL_REASON_CHARS = 2_000


def can_withdraw(complaint: Complaint, employee) -> bool:
    """Whether ``employee`` may retract ``complaint``.

    The complainant, or HR. Explicitly **not** the respondent: letting the
    person a complaint is about make it go away is the failure mode this whole
    module exists to prevent.

    For a company-filed complaint there is no complainant, so only HR can.
    """
    if employee is None:
        return False
    if complaint.organisation_id != employee.organisation_id:
        return False
    if complaint.respondent_id == employee.pk:
        return False
    if complaint.complainant_id == employee.pk:
        return True
    return bool(getattr(employee, "is_hr", False))


@transaction.atomic
def withdraw_complaint(
    *, complaint: Complaint, actor, reason: str = "", request=None
) -> Complaint:
    """Retract a complaint.

    Two paths, chosen by where the case has got to:

    * **Before an investigation** (B1) -- closes outright as ``WITHDRAWN``. No
      investigator, no decision to record.
    * **During an investigation** -- does *not* close the case. It moves to
      ``AWAITING_DECISION`` with the request recorded, and HR closes it with
      ``WITHDRAWN_BY_COMPLAINANT``. HR keeps the final say because a withdrawn
      harassment complaint may still need investigating on the company's
      behalf.

    Once the case is already awaiting a decision or closed, there is nothing
    left to withdraw.
    """
    complaint = Complaint.objects.select_for_update().get(pk=complaint.pk)

    if not can_withdraw(complaint, actor):
        raise ServiceError("You cannot withdraw this complaint.")

    reason = (reason or "").strip()[:MAX_WITHDRAWAL_REASON_CHARS]
    previous_state = complaint.state
    now = timezone.now()

    if complaint.state == enums.ComplaintState.SUBMITTED:
        transition = transitions.check(complaint, "withdraw_before_investigation")
        verb = enums.EventVerb.WITHDRAWN
    else:
        # Raises for AWAITING_DECISION, RESOLVED and WITHDRAWN.
        transition = transitions.check(complaint, "request_withdrawal")
        verb = enums.EventVerb.WITHDRAWAL_REQUESTED

    complaint.state = transition.target
    complaint.withdrawal_requested_at = now
    complaint.withdrawal_requested_by = actor
    complaint.updated_by = actor
    complaint.save(
        update_fields=[
            "state", "withdrawal_requested_at", "withdrawal_requested_by",
            "updated_by", "updated_at",
        ]
    )

    # Nothing further is expected from anyone once a withdrawal is in.
    current = complaint.investigations.order_by("-round").first()
    if current is not None:
        InformationRequest.objects.filter(
            investigation=current, status=enums.InformationRequestStatus.PENDING
        ).update(status=enums.InformationRequestStatus.EXPIRED)

    record_event(
        complaint,
        verb=verb,
        actor=actor,
        from_state=previous_state,
        to_state=complaint.state,
        payload={"reason": reason} if reason else {},
        request=request,
    )

    # HR always hears about it. Before an investigation there is nobody else to
    # tell; during one, the lead needs to know to stop.
    recipients = _hr_recipients(complaint)
    if current is not None:
        recipients.append(current.lead)

    notifications.notify(
        notifications.COMPLAINT_WITHDRAWN,
        complaint,
        recipients,
        reference=complaint.reference,
        closed_outright=complaint.state == enums.ComplaintState.WITHDRAWN,
    )
    return complaint
