"""Write operations on complaints.

Every state change lives here, never in a serializer or a view. Each service
does the same four things in one transaction: validate the guard, mutate,
write the audit row, emit notifications. Keeping that shape uniform is what
makes the audit trail trustworthy -- there is no path that changes a complaint
without recording it.
"""
from __future__ import annotations
from django.db import transaction
from apps.grievances import enums, notifications
from apps.grievances.access import resolve_effective_visibility
from apps.grievances.events import record_event
from apps.grievances.models import Complaint, ComplaintWitness
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
