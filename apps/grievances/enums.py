"""Fixed value sets for the grievances module.

Spec v4 section 4. These are deliberately code-level TextChoices rather than
lookup tables: Product confirmed the lists are fixed, because free text breaks
filtering and reporting the moment people type their own variants.

Adding a value later is a migration plus a coordinated frontend release, so
changes here are not casual.
"""
from django.db import models


class ComplaintSource(models.TextChoices):
    SELF = "SELF", "Filed by the employee"
    HR_FOR_EMPLOYEE = "HR_FOR_EMPLOYEE", "Filed by HR on behalf of an employee"
    HR_FOR_COMPANY = "HR_FOR_COMPANY", "Filed by HR on behalf of the company"


class SubjectType(models.TextChoices):
    GENERAL = "GENERAL", "General grievance"
    EMPLOYEE = "EMPLOYEE", "About an employee"


class ComplaintType(models.TextChoices):
    SEXUAL_ASSAULT = "SEXUAL_ASSAULT", "Sexual assault"
    SEXUAL_HARASSMENT = "SEXUAL_HARASSMENT", "Sexual harassment"
    HOSTILE_WORK_ENVIRONMENT = "HOSTILE_WORK_ENVIRONMENT", "Hostile work environment"
    THEFT = "THEFT", "Theft"
    UNSUSTAINABLE_WORKLOAD = "UNSUSTAINABLE_WORKLOAD", "Unsustainable workload"
    OTHERS = "OTHERS", "Others"


class Visibility(models.TextChoices):
    """Who a complaint is filed to.

    Spec v4 section 2 -- these are genuinely three-way. HR does NOT see
    LINE_MANAGER complaints. Any 'all complaints' metric must respect this.
    """

    HR = "HR", "HR only"
    LINE_MANAGER = "LINE_MANAGER", "Line manager only"
    BOTH = "BOTH", "HR and line manager"


class Frequency(models.TextChoices):
    ONE_TIME = "ONE_TIME", "One time"
    REPEAT_BEHAVIOR = "REPEAT_BEHAVIOR", "Repeat behaviour"


class ComplaintState(models.TextChoices):
    """Single canonical lifecycle field.

    The UI shows Status, Stage and Decision as three columns. They are views of
    this one field, derived in the serializer. Storing them separately produces
    impossible combinations such as 'Closed / Investigation'.
    """

    SUBMITTED = "SUBMITTED", "Submitted"
    UNDER_INVESTIGATION = "UNDER_INVESTIGATION", "Under investigation"
    AWAITING_DECISION = "AWAITING_DECISION", "Awaiting decision"
    RESOLVED = "RESOLVED", "Resolved"
    WITHDRAWN = "WITHDRAWN", "Withdrawn"


# Derived display values. Keep in one place so the UI mapping cannot drift.
STATE_TO_STATUS = {
    ComplaintState.SUBMITTED: "Pending",
    ComplaintState.UNDER_INVESTIGATION: "Open",
    ComplaintState.AWAITING_DECISION: "Open",
    ComplaintState.RESOLVED: "Closed",
    ComplaintState.WITHDRAWN: "Closed",
}

STATE_TO_STAGE = {
    ComplaintState.SUBMITTED: "N/A",
    ComplaintState.UNDER_INVESTIGATION: "Investigation",
    ComplaintState.AWAITING_DECISION: "Resolution",
    ComplaintState.RESOLVED: "Resolution",
    ComplaintState.WITHDRAWN: "N/A",
}

#: Reverse maps for filtering. The UI filters on Status and Stage, which are
#: derived values -- these translate a filter choice back to the states it
#: covers. Derived from STATE_TO_STATUS / STATE_TO_STAGE so they cannot drift.
STATUS_TO_STATES = {
    status: [s for s, v in STATE_TO_STATUS.items() if v == status]
    for status in dict.fromkeys(STATE_TO_STATUS.values())
}

STAGE_TO_STATES = {
    stage: [s for s, v in STATE_TO_STAGE.items() if v == stage]
    for stage in dict.fromkeys(STATE_TO_STAGE.values())
}

#: HR console tabs. "By Employees" is everything an employee filed themselves;
#: "By HR" is everything HR filed, on behalf of a person or the company.
SOURCE_TAB_EMPLOYEE = "employee"
SOURCE_TAB_HR = "hr"

#: States in which a complaint may still be soft-deleted or withdrawn.
#: Spec v4 section 7: both close the moment an investigator is appointed.
PRE_INVESTIGATION_STATES = frozenset({ComplaintState.SUBMITTED})


class InvestigationState(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    IN_PROGRESS = "IN_PROGRESS", "In progress"
    REPORT_SUBMITTED = "REPORT_SUBMITTED", "Report submitted"


class CollaboratorRole(models.TextChoices):
    COMPLAINANT = "COMPLAINANT", "Complainant"
    RESPONDENT = "RESPONDENT", "Respondent"
    WITNESS = "WITNESS", "Witness"
    OTHER = "OTHER", "Other"


class CollaboratorStatus(models.TextChoices):
    INVITED = "INVITED", "Invited"
    ACTIVE = "ACTIVE", "Active"
    DECLINED = "DECLINED", "Declined"
    REMOVED = "REMOVED", "Removed"


class InformationRequestStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    ANSWERED = "ANSWERED", "Answered"
    EXPIRED = "EXPIRED", "Expired"


class WitnessType(models.TextChoices):
    EMPLOYEE = "EMPLOYEE", "Employee"
    DEPARTMENT = "DEPARTMENT", "Department"
    LINE_MANAGER = "LINE_MANAGER", "Line manager"


class InvestigationDecision(models.TextChoices):
    SUBSTANTIATED = "SUBSTANTIATED", "Substantiated"
    UNSUBSTANTIATED = "UNSUBSTANTIATED", "Unsubstantiated"
    PARTIALLY_SUBSTANTIATED = "PARTIALLY_SUBSTANTIATED", "Partially substantiated"
    INCONCLUSIVE = "INCONCLUSIVE", "Inconclusive"
    WITHDRAWN_BY_COMPLAINANT = "WITHDRAWN_BY_COMPLAINANT", "Withdrawn by complainant"
    NO_FURTHER_ACTION_REQUIRED = "NO_FURTHER_ACTION_REQUIRED", "No further action required"


class ResolutionType(models.TextChoices):
    FORMAL = "FORMAL", "Formal resolution"
    INFORMAL = "INFORMAL", "Informal resolution"
    NO_RESOLUTION_REQUIRED = "NO_RESOLUTION_REQUIRED", "No resolution required"


class FormalResolutionType(models.TextChoices):
    FIRST_WRITTEN_WARNING = "FIRST_WRITTEN_WARNING", "First written warning"
    SECOND_WRITTEN_WARNING = "SECOND_WRITTEN_WARNING", "Second written warning"
    FINAL_WRITTEN_WARNING = "FINAL_WRITTEN_WARNING", "Final written warning"
    SUSPENSION = "SUSPENSION", "Suspension"
    DISMISSAL = "DISMISSAL", "Dismissal"
    OTHERS = "OTHERS", "Others"


class InformalResolutionType(models.TextChoices):
    """Not present in the Figma design -- added by Product, spec v4 section 7.2.

    Backend carries it now; the frontend can adopt it when convenient.
    """

    VERBAL_WARNING = "VERBAL_WARNING", "Verbal warning"
    COACHING = "COACHING", "Coaching"
    MEDIATION = "MEDIATION", "Mediation"
    NO_ACTION_RESOLVED_WITHOUT_FORMAL_RECORD = (
        "NO_ACTION_RESOLVED_WITHOUT_FORMAL_RECORD",
        "No action (resolved without formal record)",
    )
    OTHERS = "OTHERS", "Others"


class PIPState(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    COMPLETED = "COMPLETED", "Completed"
    CANCELLED = "CANCELLED", "Cancelled"


class FollowUpKind(models.TextChoices):
    TWO_WEEK_CHECKIN = "TWO_WEEK_CHECKIN", "Two week check-in"
    CUSTOM = "CUSTOM", "Custom"


class EventVerb(models.TextChoices):
    """Audit trail verbs.

    A grievance record is evidence. 'Who saw this, and when' is a question that
    gets asked in an employment dispute, so every meaningful action writes one
    of these.
    """

    FILED = "FILED", "Complaint filed"
    VIEWED = "VIEWED", "Complaint viewed"
    VISIBILITY_OVERRIDDEN = "VISIBILITY_OVERRIDDEN", "Visibility overridden at intake"
    ORPHAN_FALLBACK = "ORPHAN_FALLBACK", "Fell back to HR visibility"
    INVESTIGATOR_APPOINTED = "INVESTIGATOR_APPOINTED", "Investigator appointed"
    COLLABORATOR_INVITED = "COLLABORATOR_INVITED", "Collaborator invited"
    COLLABORATOR_REMOVED = "COLLABORATOR_REMOVED", "Collaborator removed"
    INFORMATION_REQUESTED = "INFORMATION_REQUESTED", "Information requested"
    INFORMATION_RECEIVED = "INFORMATION_RECEIVED", "Information received"
    MEETING_RECORDED = "MEETING_RECORDED", "Meeting recorded"
    ATTACHMENT_ADDED = "ATTACHMENT_ADDED", "Attachment added"
    IDENTITY_RELEASED = "IDENTITY_RELEASED", "Complainant identity released"
    REPORT_SUBMITTED = "REPORT_SUBMITTED", "Investigation report submitted"
    WITHDRAWAL_REQUESTED = "WITHDRAWAL_REQUESTED", "Withdrawal requested"
    WITHDRAWN = "WITHDRAWN", "Complaint withdrawn"
    RESOLVED = "RESOLVED", "Complaint resolved and closed"
    REOPENED = "REOPENED", "Complaint reopened"
    DELETE_ATTEMPTED = "DELETE_ATTEMPTED", "Deletion attempted"
    DELETED = "DELETED", "Complaint deleted"
