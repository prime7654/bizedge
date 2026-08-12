"""Grievances domain model.

Implements spec v4. Two rules run through everything here and should be
understood before changing anything:

1. ``Complaint.state`` is the single lifecycle field. Status and Stage are
   derived, never stored.
2. Visibility is genuinely three-way. HR does not see LINE_MANAGER complaints.
   Access decisions belong in the access policy, not in model properties, but
   the fields those decisions read are defined here.
"""
import uuid

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.core.validators import MinValueValidator
from django.db import models

from apps.core.models import SoftDeleteModel, TenantOwnedModel, TimeStampedModel
from apps.grievances import enums

EMPLOYEE = settings.GRIEVANCES_EMPLOYEE_MODEL
DEPARTMENT = settings.GRIEVANCES_DEPARTMENT_MODEL
TRAINING = settings.GRIEVANCES_TRAINING_MODEL


class Attachment(TimeStampedModel):
    """Polymorphic attachment for complaints, investigations and meetings.

    Storage must be private. Grievance evidence is served through a
    permission-checked view issuing short-lived signed URLs -- never a
    guessable MEDIA_URL path.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.UUIDField()
    owner = GenericForeignKey("content_type", "object_id")

    file = models.FileField(upload_to="grievances/%Y/%m/")
    original_filename = models.CharField(max_length=512)
    content_type_header = models.CharField(max_length=128, blank=True)
    size_bytes = models.BigIntegerField(default=0)
    uploaded_by = models.ForeignKey(
        EMPLOYEE, on_delete=models.SET_NULL, null=True, related_name="+"
    )

    class Meta:
        indexes = [models.Index(fields=("content_type", "object_id"))]
        ordering = ("created_at",)

    def __str__(self) -> str:
        return self.original_filename


class Complaint(TenantOwnedModel, TimeStampedModel, SoftDeleteModel):
    """The aggregate root.

    Deletion is soft and tightly restricted (spec v4 section 7): only while
    SUBMITTED, and only by the HR user who created the record.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(
        max_length=32,
        unique=True,
        help_text="Human-readable case reference, e.g. CMP-2026-00184.",
    )

    source = models.CharField(max_length=32, choices=enums.ComplaintSource.choices)
    subject_type = models.CharField(max_length=16, choices=enums.SubjectType.choices)

    filed_by = models.ForeignKey(
        EMPLOYEE,
        on_delete=models.PROTECT,
        related_name="complaints_filed",
        help_text="Whoever submitted the form. Equals complainant unless HR filed it.",
    )
    complainant = models.ForeignKey(
        EMPLOYEE,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="complaints_as_complainant",
        help_text="Null only when HR files on behalf of the company.",
    )
    respondent = models.ForeignKey(
        EMPLOYEE,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="complaints_as_respondent",
        help_text="Required when subject_type is EMPLOYEE.",
    )

    complaint_type = models.CharField(max_length=32, choices=enums.ComplaintType.choices)
    complaint_type_note = models.TextField(
        blank=True,
        help_text=(
            "Free text describing an OTHERS complaint. Stored for the case "
            "record and reporting. Never used as a filter dimension."
        ),
    )

    frequency = models.CharField(
        max_length=16, choices=enums.Frequency.choices, blank=True
    )
    occurrence_count = models.PositiveIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1)]
    )
    incident_date = models.DateField(null=True, blank=True)
    description = models.TextField()

    # --- Visibility -------------------------------------------------------
    visibility = models.CharField(
        max_length=16,
        choices=enums.Visibility.choices,
        db_index=True,
        help_text="Effective visibility, after any intake override.",
    )
    visibility_requested = models.CharField(
        max_length=16,
        choices=enums.Visibility.choices,
        help_text=(
            "What the complainant actually selected. Differs from `visibility` "
            "when the complaint is about their own line manager and was forced "
            "to HR. Audit needs the original choice."
        ),
    )

    # --- Lifecycle --------------------------------------------------------
    state = models.CharField(
        max_length=32,
        choices=enums.ComplaintState.choices,
        default=enums.ComplaintState.SUBMITTED,
        db_index=True,
    )
    due_date = models.DateField(
        null=True,
        blank=True,
        help_text="Set by HR when an investigator is appointed. Null before that.",
    )

    # --- Complainant identity release (spec v4 A1) ------------------------
    complainant_identity_released = models.BooleanField(
        default=False,
        help_text=(
            "The respondent sees the allegation but not who filed it, unless "
            "HR explicitly releases the identity."
        ),
    )
    complainant_identity_released_by = models.ForeignKey(
        EMPLOYEE, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    complainant_identity_released_at = models.DateTimeField(null=True, blank=True)

    # --- Mid-investigation withdrawal (spec v4 section 3) -----------------
    withdrawal_requested_at = models.DateTimeField(null=True, blank=True)
    withdrawal_requested_by = models.ForeignKey(
        EMPLOYEE, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    created_by = models.ForeignKey(
        EMPLOYEE, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_by = models.ForeignKey(
        EMPLOYEE, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    attachments = GenericRelation(Attachment)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("organisation", "state")),
            models.Index(fields=("organisation", "visibility")),
            models.Index(fields=("complainant", "state")),
            models.Index(fields=("respondent", "state")),
        ]
        constraints = [
            # An EMPLOYEE complaint must name a respondent; a GENERAL one must not.
            models.CheckConstraint(
                condition=(
                    models.Q(subject_type=enums.SubjectType.EMPLOYEE, respondent__isnull=False)
                    | models.Q(subject_type=enums.SubjectType.GENERAL, respondent__isnull=True)
                ),
                name="complaint_respondent_matches_subject_type",
            ),
            # Nobody may file a complaint against themselves.
            models.CheckConstraint(
                condition=(
                    models.Q(respondent__isnull=True)
                    | models.Q(complainant__isnull=True)
                    | ~models.Q(respondent=models.F("complainant"))
                ),
                name="complaint_respondent_is_not_complainant",
            ),
            # Repeat behaviour must say how many times.
            models.CheckConstraint(
                condition=(
                    ~models.Q(frequency=enums.Frequency.REPEAT_BEHAVIOR)
                    | models.Q(occurrence_count__isnull=False)
                ),
                name="complaint_repeat_requires_occurrence_count",
            ),
            # OTHERS must carry a description, or the category is meaningless.
            models.CheckConstraint(
                condition=(
                    ~models.Q(complaint_type=enums.ComplaintType.OTHERS)
                    | ~models.Q(complaint_type_note="")
                ),
                name="complaint_others_requires_note",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.reference} ({self.get_complaint_type_display()})"

    # --- Derived UI fields ------------------------------------------------
    @property
    def status_label(self) -> str:
        return enums.STATE_TO_STATUS[self.state]

    @property
    def stage_label(self) -> str:
        return enums.STATE_TO_STAGE[self.state]

    @property
    def is_open_for_deletion(self) -> bool:
        """Deletion closes permanently once an investigator is appointed."""
        return self.state in enums.PRE_INVESTIGATION_STATES

    @property
    def current_investigation(self):
        return self.investigations.order_by("-round").first()


class ComplaintWitness(TimeStampedModel):
    """Witnesses named at filing.

    Not a plain M2M to Employee: the picker also accepts departments and
    'my line manager', so the reference is polymorphic by type.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    complaint = models.ForeignKey(
        Complaint, on_delete=models.CASCADE, related_name="witnesses"
    )
    witness_type = models.CharField(max_length=16, choices=enums.WitnessType.choices)
    employee = models.ForeignKey(
        EMPLOYEE, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    department = models.ForeignKey(
        DEPARTMENT, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    added_by = models.ForeignKey(
        EMPLOYEE, null=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(witness_type=enums.WitnessType.EMPLOYEE, employee__isnull=False)
                    | models.Q(witness_type=enums.WitnessType.DEPARTMENT, department__isnull=False)
                    | models.Q(witness_type=enums.WitnessType.LINE_MANAGER)
                ),
                name="witness_reference_matches_type",
            )
        ]


class Investigation(TimeStampedModel):
    """One round of investigation on a complaint.

    A reopen creates a new row with an incremented ``round``. Previous rounds
    are never mutated -- the record must show what was decided the first time
    and on what basis.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    complaint = models.ForeignKey(
        Complaint, on_delete=models.CASCADE, related_name="investigations"
    )
    round = models.PositiveIntegerField(default=1)

    lead = models.ForeignKey(
        EMPLOYEE,
        on_delete=models.PROTECT,
        related_name="investigations_led",
        help_text="One lead per case. May be the HR user who triaged it.",
    )
    lead_is_hr = models.BooleanField(
        default=False, help_text="True when HR assigned the case to themselves."
    )
    invited_by = models.ForeignKey(
        EMPLOYEE, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    invited_at = models.DateTimeField(null=True, blank=True)

    start_date = models.DateField()
    state = models.CharField(
        max_length=32,
        choices=enums.InvestigationState.choices,
        default=enums.InvestigationState.IN_PROGRESS,
    )
    report_submitted_at = models.DateTimeField(null=True, blank=True)
    report_submitted_by = models.ForeignKey(
        EMPLOYEE, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    attachments = GenericRelation(Attachment)

    class Meta:
        ordering = ("complaint", "round")
        constraints = [
            models.UniqueConstraint(
                fields=("complaint", "round"), name="uniq_investigation_round_per_complaint"
            )
        ]

    def __str__(self) -> str:
        return f"{self.complaint.reference} round {self.round}"


class InvestigationCollaborator(TimeStampedModel):
    """Someone pulled into an investigation.

    Soft-removed via ``status``, never deleted -- the audit trail needs to show
    who was involved even after they were taken off the case.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    investigation = models.ForeignKey(
        Investigation, on_delete=models.CASCADE, related_name="collaborators"
    )
    employee = models.ForeignKey(EMPLOYEE, on_delete=models.PROTECT, related_name="+")
    role = models.CharField(max_length=16, choices=enums.CollaboratorRole.choices)
    status = models.CharField(
        max_length=16,
        choices=enums.CollaboratorStatus.choices,
        default=enums.CollaboratorStatus.INVITED,
    )
    invited_by = models.ForeignKey(
        EMPLOYEE, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    invited_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("investigation", "employee"),
                name="uniq_collaborator_per_investigation",
            )
        ]


class InformationRequest(TimeStampedModel):
    """A written question put to a collaborator.

    One-to-many with responses so that 'Request Additional Information'
    appends a new request rather than overwriting the previous exchange.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    investigation = models.ForeignKey(
        Investigation, on_delete=models.CASCADE, related_name="information_requests"
    )
    collaborator = models.ForeignKey(
        InvestigationCollaborator,
        on_delete=models.CASCADE,
        related_name="information_requests",
    )
    prompt = models.TextField()
    requested_by = models.ForeignKey(
        EMPLOYEE, on_delete=models.PROTECT, related_name="+"
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    due_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=16,
        choices=enums.InformationRequestStatus.choices,
        default=enums.InformationRequestStatus.PENDING,
    )

    class Meta:
        ordering = ("requested_at",)


class InformationResponse(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(
        InformationRequest, on_delete=models.CASCADE, related_name="responses"
    )
    body = models.TextField()
    responded_by = models.ForeignKey(
        EMPLOYEE, on_delete=models.PROTECT, related_name="+"
    )
    responded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("responded_at",)


class InvestigationMeeting(TimeStampedModel):
    """A meeting held during an investigation.

    Attendees resolve to collaborator records. Picking someone not yet on the
    case creates a collaborator for them, so a meeting never points at people
    the case does not know about.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    investigation = models.ForeignKey(
        Investigation, on_delete=models.CASCADE, related_name="meetings"
    )
    meeting_date = models.DateField()
    attendees = models.ManyToManyField(
        InvestigationCollaborator, related_name="meetings", blank=True
    )
    findings = models.TextField()
    recorded_by = models.ForeignKey(
        EMPLOYEE, on_delete=models.PROTECT, related_name="+"
    )

    attachments = GenericRelation(Attachment)

    class Meta:
        ordering = ("-meeting_date",)


class InvestigationNote(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    investigation = models.ForeignKey(
        Investigation, on_delete=models.CASCADE, related_name="notes"
    )
    body = models.TextField()
    author = models.ForeignKey(EMPLOYEE, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ("-created_at",)


class Resolution(TimeStampedModel):
    """HR's decision, one per investigation round.

    Exactly one of formal/informal sub-type is ever populated. Enforced at the
    database level, not only in the serializer.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    complaint = models.ForeignKey(
        Complaint, on_delete=models.CASCADE, related_name="resolutions"
    )
    investigation = models.OneToOneField(
        Investigation, on_delete=models.CASCADE, related_name="resolution"
    )

    decision = models.CharField(
        max_length=32, choices=enums.InvestigationDecision.choices
    )
    resolution_type = models.CharField(
        max_length=32, choices=enums.ResolutionType.choices
    )
    formal_resolution_type = models.CharField(
        max_length=32, choices=enums.FormalResolutionType.choices, blank=True
    )
    informal_resolution_type = models.CharField(
        max_length=64, choices=enums.InformalResolutionType.choices, blank=True
    )
    resolution_note = models.TextField(
        blank=True, help_text="Required when either sub-type is OTHERS."
    )
    decision_notes = models.TextField()

    decided_by = models.ForeignKey(EMPLOYEE, on_delete=models.PROTECT, related_name="+")
    decided_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-decided_at",)
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        resolution_type=enums.ResolutionType.FORMAL,
                        formal_resolution_type__gt="",
                        informal_resolution_type="",
                    )
                    | models.Q(
                        resolution_type=enums.ResolutionType.INFORMAL,
                        informal_resolution_type__gt="",
                        formal_resolution_type="",
                    )
                    | models.Q(
                        resolution_type=enums.ResolutionType.NO_RESOLUTION_REQUIRED,
                        formal_resolution_type="",
                        informal_resolution_type="",
                    )
                ),
                name="resolution_subtype_matches_type",
            ),
            models.CheckConstraint(
                condition=(
                    ~(
                        models.Q(formal_resolution_type=enums.FormalResolutionType.OTHERS)
                        | models.Q(
                            informal_resolution_type=enums.InformalResolutionType.OTHERS
                        )
                    )
                    | ~models.Q(resolution_note="")
                ),
                name="resolution_others_requires_note",
            ),
        ]


class PIPPlan(TimeStampedModel):
    """Performance Improvement Plan.

    Outlives the complaint: it runs on its own timeline after closure.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    resolution = models.OneToOneField(
        Resolution, on_delete=models.CASCADE, related_name="pip_plan"
    )
    employee = models.ForeignKey(EMPLOYEE, on_delete=models.PROTECT, related_name="pip_plans")
    start_date = models.DateField()
    end_date = models.DateField()
    state = models.CharField(
        max_length=16, choices=enums.PIPState.choices, default=enums.PIPState.ACTIVE
    )
    created_by = models.ForeignKey(
        EMPLOYEE, null=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ("-start_date",)
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_date__gte=models.F("start_date")),
                name="pip_end_after_start",
            )
        ]


class PIPTrainingAssignment(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    pip_plan = models.ForeignKey(
        PIPPlan, on_delete=models.CASCADE, related_name="training_assignments"
    )
    training = models.ForeignKey(TRAINING, on_delete=models.PROTECT, related_name="+")
    assigned_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)


class PIPFollowUp(TimeStampedModel):
    """Scheduled check-in.

    The reminder is the only part of this module not driven by a user action --
    it needs a scheduled task runner.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    pip_plan = models.ForeignKey(
        PIPPlan, on_delete=models.CASCADE, related_name="follow_ups"
    )
    scheduled_date = models.DateField(db_index=True)
    kind = models.CharField(
        max_length=32, choices=enums.FollowUpKind.choices, default=enums.FollowUpKind.CUSTOM
    )
    reminder_enabled = models.BooleanField(default=True)
    reminder_time = models.TimeField(null=True, blank=True)
    reminder_sent_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    outcome_notes = models.TextField(blank=True)

    class Meta:
        ordering = ("scheduled_date",)


class ComplaintEvent(models.Model):
    """Append-only audit trail.

    Not optional. A grievance record is evidence in an employment dispute, and
    'who saw this and when' is a question that will be asked. Never update or
    delete rows in this table.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    complaint = models.ForeignKey(
        Complaint, on_delete=models.CASCADE, related_name="events"
    )
    actor = models.ForeignKey(
        EMPLOYEE, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    verb = models.CharField(max_length=32, choices=enums.EventVerb.choices, db_index=True)
    from_state = models.CharField(
        max_length=32, choices=enums.ComplaintState.choices, blank=True
    )
    to_state = models.CharField(
        max_length=32, choices=enums.ComplaintState.choices, blank=True
    )
    payload = models.JSONField(default=dict, blank=True)
    occurred_at = models.DateTimeField(auto_now_add=True, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)

    class Meta:
        ordering = ("-occurred_at",)
        indexes = [models.Index(fields=("complaint", "-occurred_at"))]

    def __str__(self) -> str:
        return f"{self.complaint_id} {self.verb} @ {self.occurred_at}"
