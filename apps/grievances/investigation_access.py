"""Who may do what inside an investigation.

Separate from :mod:`apps.grievances.access`, which governs the complaint
itself. The distinction matters: seeing a complaint and being able to run its
investigation are different rights, and one collaborator role deliberately
gets neither.

Three tiers:

* **Manage** -- the investigation lead. Invites people, asks questions, records
  meetings and notes, submits the report.
* **View** -- anyone with FULL access to the complaint, chiefly HR. Can read
  the investigation but not drive it.
* **Respond** -- a collaborator. Sees the single question addressed to them and
  nothing else: not the complaint description, not other people's answers, not
  meeting findings. This is the sharpest boundary in the module.
"""
from __future__ import annotations

from apps.grievances.access import AccessLevel, ComplaintAccessPolicy
from apps.grievances.models import InformationRequest, Investigation


class InvestigationAccessPolicy:
    """Read and write access to an investigation."""

    @staticmethod
    def can_manage(investigation: Investigation, employee) -> bool:
        """Only the lead drives their own investigation.

        Deliberately not "HR can do anything". An HR user who wants to take
        over should reassign the case, which leaves an audit trail, rather than
        quietly acting inside someone else's investigation.
        """
        if employee is None:
            return False
        if investigation.complaint.organisation_id != employee.organisation_id:
            return False
        return investigation.lead_id == employee.pk

    @staticmethod
    def can_view(investigation: Investigation, employee) -> bool:
        """Full access to the complaint carries read access to its investigation."""
        if employee is None:
            return False
        if InvestigationAccessPolicy.can_manage(investigation, employee):
            return True
        return (
            ComplaintAccessPolicy.access_level(investigation.complaint, employee)
            is AccessLevel.FULL
        )

    @staticmethod
    def can_respond(request_obj: InformationRequest, employee) -> bool:
        """Only the person the question was put to may answer it.

        Checked against the collaborator's employee, not the complaint's
        participants -- someone can be a collaborator without being party to
        the complaint, and being party to the complaint does not entitle you to
        answer somebody else's question.
        """
        if employee is None:
            return False
        collaborator = request_obj.collaborator
        if collaborator.employee_id != employee.pk:
            return False
        return (
            collaborator.investigation.complaint.organisation_id
            == employee.organisation_id
        )

    @staticmethod
    def pending_requests_for(employee):
        """The collaborator's inbox.

        Only requests still awaiting an answer, on investigations that are
        still running. A question on a closed case is not actionable and
        showing it invites a pointless reply.
        """
        from apps.grievances import enums

        if employee is None:
            return InformationRequest.objects.none()

        return (
            InformationRequest.objects.filter(
                collaborator__employee_id=employee.pk,
                status=enums.InformationRequestStatus.PENDING,
                collaborator__status__in=[
                    enums.CollaboratorStatus.INVITED,
                    enums.CollaboratorStatus.ACTIVE,
                ],
                investigation__state=enums.InvestigationState.IN_PROGRESS,
            )
            .select_related("investigation__complaint", "requested_by")
            .order_by("-requested_at")
        )
