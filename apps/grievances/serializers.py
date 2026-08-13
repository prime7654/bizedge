"""Request and response shapes for the complaints API.

Validation here is deliberately conditional: which fields are required depends
on ``source``, ``subject_type`` and ``frequency``. The whole rule table lives
in :meth:`ComplaintCreateSerializer.validate` rather than being spread across
several serializers, because the design has a dedicated error state screen and
the frontend needs error keys that match its form field names exactly.
"""
from __future__ import annotations

from django.conf import settings
from django.db.models import Q
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from apps.directory.models import Department, Employee, Training
from apps.grievances import enums
from apps.grievances.access import AccessLevel, ComplaintAccessPolicy
from apps.grievances.models import (
    Attachment,
    Complaint,
    ComplaintEvent,
    ComplaintWitness,
    InformationRequest,
    InformationResponse,
    Investigation,
    InvestigationMeeting,
    InvestigationNote,
    PIPFollowUp,
    PIPPlan,
    Resolution,
)


class EmployeeBriefSerializer(serializers.ModelSerializer):
    """Minimal employee representation for embedding in complaint payloads."""

    class Meta:
        model = Employee
        fields = ("id", "full_name", "job_title")
        read_only_fields = fields


class WitnessInputSerializer(serializers.Serializer):
    """A witness named at filing.

    Not a plain employee reference: the picker also accepts departments and
    "my line manager", so the payload is polymorphic by ``witness_type``.
    """

    witness_type = serializers.ChoiceField(choices=enums.WitnessType.choices)
    employee = serializers.PrimaryKeyRelatedField(
        queryset=Employee.objects.none(), required=False, allow_null=True
    )
    department = serializers.PrimaryKeyRelatedField(
        queryset=Department.objects.none(), required=False, allow_null=True
    )

    def get_fields(self):
        """Scope the pickers to the caller's tenant.

        Done here rather than in ``__init__``: this serializer is declared as a
        nested field, so it is instantiated at class-definition time when
        ``self.context`` is still empty. ``get_fields`` runs lazily, after the
        parent has bound context, which is the only point where the tenant is
        actually known.

        Getting this wrong is not a soft failure -- an unscoped queryset here
        was silently ``Employee.objects.none()``, which rejected every valid
        witness.
        """
        fields = super().get_fields()
        organisation_id = self.context.get("organisation_id")
        if organisation_id is not None:
            fields["employee"].queryset = Employee.objects.filter(
                organisation_id=organisation_id, is_active=True
            )
            fields["department"].queryset = Department.objects.filter(
                organisation_id=organisation_id
            )
        return fields

    def validate(self, attrs):
        witness_type = attrs.get("witness_type")
        if witness_type == enums.WitnessType.EMPLOYEE and not attrs.get("employee"):
            raise serializers.ValidationError(
                {"employee": "Select the employee who witnessed this."}
            )
        if witness_type == enums.WitnessType.DEPARTMENT and not attrs.get("department"):
            raise serializers.ValidationError(
                {"department": "Select the department."}
            )
        return attrs


class ComplaintWitnessSerializer(serializers.ModelSerializer):
    employee = EmployeeBriefSerializer(read_only=True)
    department_name = serializers.CharField(source="department.name", read_only=True)

    class Meta:
        model = ComplaintWitness
        fields = ("id", "witness_type", "employee", "department_name")
        read_only_fields = fields


class AttachmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Attachment
        fields = ("id", "original_filename", "size_bytes", "created_at")
        read_only_fields = fields


class ComplaintCreateSerializer(serializers.Serializer):
    """All four filing routes.

    One serializer rather than four, because the routes differ by a handful of
    required fields rather than by shape, and four near-identical serializers
    drift apart.
    """

    source = serializers.ChoiceField(choices=enums.ComplaintSource.choices)
    subject_type = serializers.ChoiceField(choices=enums.SubjectType.choices)
    complaint_type = serializers.ChoiceField(choices=enums.ComplaintType.choices)
    complaint_type_note = serializers.CharField(
        required=False, allow_blank=True, max_length=2000
    )

    complainant = serializers.PrimaryKeyRelatedField(
        queryset=Employee.objects.none(), required=False, allow_null=True
    )
    respondent = serializers.PrimaryKeyRelatedField(
        queryset=Employee.objects.none(), required=False, allow_null=True
    )

    frequency = serializers.ChoiceField(
        choices=enums.Frequency.choices, required=False, allow_blank=True
    )
    occurrence_count = serializers.IntegerField(
        required=False, allow_null=True, min_value=1
    )
    incident_date = serializers.DateField(required=False, allow_null=True)
    description = serializers.CharField(max_length=20000)
    visibility = serializers.ChoiceField(choices=enums.Visibility.choices)
    witnesses = WitnessInputSerializer(many=True, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        employee = self.context.get("employee")
        if employee is not None:
            in_tenant = Employee.objects.filter(
                organisation_id=employee.organisation_id, is_active=True
            )
            self.fields["complainant"].queryset = in_tenant
            self.fields["respondent"].queryset = in_tenant

    def validate_incident_date(self, value):
        if value is None:
            return value
        from django.utils import timezone

        if value > timezone.localdate():
            raise serializers.ValidationError("The incident date cannot be in the future.")
        return value

    def validate(self, attrs):
        """The conditional rule table from spec v4 section 5.

        Errors are keyed by form field name so the frontend can attach them to
        the right input.
        """
        errors: dict[str, str] = {}
        employee = self.context["employee"]

        source = attrs["source"]
        subject_type = attrs["subject_type"]
        complainant = attrs.get("complainant")
        respondent = attrs.get("respondent")

        # --- who is allowed to use which route -------------------------------
        if source in (
            enums.ComplaintSource.HR_FOR_EMPLOYEE,
            enums.ComplaintSource.HR_FOR_COMPANY,
        ) and not employee.is_hr:
            errors["source"] = "Only HR can file a complaint on behalf of someone else."

        # --- complainant rules by source -------------------------------------
        if source == enums.ComplaintSource.SELF:
            if complainant is not None and complainant.pk != employee.pk:
                errors["complainant"] = (
                    "Leave this blank when filing your own complaint."
                )
        elif source == enums.ComplaintSource.HR_FOR_EMPLOYEE:
            if complainant is None:
                errors["complainant"] = "Select the employee this complaint is for."
        elif source == enums.ComplaintSource.HR_FOR_COMPANY:
            if complainant is not None:
                errors["complainant"] = (
                    "Leave this blank when filing on behalf of the company."
                )

        # --- respondent rules by subject type --------------------------------
        if subject_type == enums.SubjectType.EMPLOYEE:
            if respondent is None:
                errors["respondent"] = "Select the employee this complaint is about."
            if not attrs.get("incident_date"):
                errors["incident_date"] = "Enter the date this happened."
            if not attrs.get("frequency"):
                errors["frequency"] = "Select how often this happened."
        else:
            if respondent is not None:
                errors["respondent"] = (
                    "A general complaint cannot be filed against a specific person."
                )

        # Nobody complains about themselves. Mirrors a DB constraint; caught
        # here so the user gets a field error rather than a 500.
        effective_complainant = (
            complainant if source != enums.ComplaintSource.SELF else employee
        )
        if (
            respondent is not None
            and effective_complainant is not None
            and respondent.pk == effective_complainant.pk
        ):
            errors["respondent"] = "A complaint cannot be filed against the complainant."

        # --- frequency ---------------------------------------------------------
        if attrs.get("frequency") == enums.Frequency.REPEAT_BEHAVIOR:
            if not attrs.get("occurrence_count"):
                errors["occurrence_count"] = "Enter how many times this happened."

        # --- OTHERS needs a description ----------------------------------------
        if attrs["complaint_type"] == enums.ComplaintType.OTHERS:
            if not (attrs.get("complaint_type_note") or "").strip():
                errors["complaint_type_note"] = (
                    "Describe the type of complaint you are making."
                )

        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class InvestigationSerializer(serializers.ModelSerializer):
    lead = EmployeeBriefSerializer(read_only=True)
    collaborators = serializers.SerializerMethodField()

    class Meta:
        model = Investigation
        fields = (
            "id", "round", "lead", "lead_is_hr", "start_date",
            "state", "report_submitted_at", "collaborators",
        )
        read_only_fields = fields

    @extend_schema_field(serializers.ListField(child=serializers.DictField()))
    def get_collaborators(self, obj):
        return [
            {
                "id": str(c.pk),
                "employee": EmployeeBriefSerializer(c.employee).data,
                "role": c.role,
                "status": c.status,
            }
            for c in obj.collaborators.select_related("employee")
        ]


class ComplaintListSerializer(serializers.ModelSerializer):
    """Row shape for complaint lists.

    ``status`` and ``stage`` are derived from ``state``, never stored -- see
    the note in enums.py about why.
    """

    status = serializers.CharField(source="status_label", read_only=True)
    stage = serializers.CharField(source="stage_label", read_only=True)
    complaint_type_display = serializers.CharField(
        source="get_complaint_type_display", read_only=True
    )
    respondent = serializers.SerializerMethodField()
    complainant = serializers.SerializerMethodField()

    class Meta:
        model = Complaint
        fields = (
            "id", "reference", "complaint_type", "complaint_type_display",
            "complaint_type_note", "subject_type", "source", "state",
            "status", "stage", "visibility", "due_date",
            "complainant", "respondent", "created_at",
        )
        read_only_fields = fields

    def _viewer(self):
        return self.context.get("employee")

    @extend_schema_field(EmployeeBriefSerializer(allow_null=True))
    def get_respondent(self, obj):
        if obj.respondent is None:
            return None
        return EmployeeBriefSerializer(obj.respondent).data

    @extend_schema_field(EmployeeBriefSerializer(allow_null=True))
    def get_complainant(self, obj):
        """Masked from the respondent unless HR has released the identity.

        Nullable in the schema on purpose -- clients must handle a null
        complainant, both for masking and for company-filed complaints.
        """
        if obj.complainant is None:
            return None
        if ComplaintAccessPolicy.should_mask_complainant(obj, self._viewer()):
            return None
        return EmployeeBriefSerializer(obj.complainant).data


class ComplaintDetailSerializer(ComplaintListSerializer):
    """Full detail for anyone with FULL access."""

    witnesses = ComplaintWitnessSerializer(many=True, read_only=True)
    attachments = AttachmentSerializer(many=True, read_only=True)
    filed_by = EmployeeBriefSerializer(read_only=True)
    available_transitions = serializers.SerializerMethodField()
    investigation = serializers.SerializerMethodField()
    resolution = serializers.SerializerMethodField()

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_available_transitions(self, obj):
        """Which moves are legal from here.

        Served so the client can render action buttons from the server's
        transition table instead of keeping its own copy of the state machine.
        """
        from apps.grievances.transitions import available_transitions

        return available_transitions(obj.state)

    @extend_schema_field(InvestigationSerializer(allow_null=True))
    def get_investigation(self, obj):
        current = obj.current_investigation
        return InvestigationSerializer(current).data if current else None

    @extend_schema_field(serializers.DictField(allow_null=True))
    def get_resolution(self, obj):
        """The decision on the current round, if one has been made."""
        current = obj.current_investigation
        resolution = getattr(current, "resolution", None) if current else None
        if resolution is None:
            return None
        from apps.grievances.serializers import ResolutionSerializer

        return ResolutionSerializer(resolution).data

    class Meta(ComplaintListSerializer.Meta):
        fields = ComplaintListSerializer.Meta.fields + (
            "description", "incident_date", "frequency", "occurrence_count",
            "filed_by", "witnesses", "attachments", "visibility_requested",
            "complainant_identity_released", "available_transitions",
            "investigation", "resolution",
        )
        read_only_fields = fields


class ComplaintRestrictedSerializer(ComplaintListSerializer):
    """What a respondent sees.

    A separate serializer, not the detail one with fields popped. Popping is
    how a field creeps back in during a later refactor and nobody notices.

    Deliberately excluded: witnesses, attachments, filed_by, visibility, and
    anything that could identify the complainant.
    """

    class Meta:
        model = Complaint
        fields = (
            "id", "reference", "complaint_type", "complaint_type_display",
            "subject_type", "state", "status", "stage",
            "incident_date", "description", "respondent", "created_at",
        )
        read_only_fields = fields


def serializer_for_access(level: AccessLevel):
    """Pick the response shape from the access level, never from the role."""
    if level is AccessLevel.RESTRICTED:
        return ComplaintRestrictedSerializer
    return ComplaintDetailSerializer


class ComplaintEventSerializer(serializers.ModelSerializer):
    """One row of the case timeline."""

    actor = EmployeeBriefSerializer(read_only=True)
    verb_display = serializers.CharField(source="get_verb_display", read_only=True)

    class Meta:
        model = ComplaintEvent
        fields = (
            "id", "verb", "verb_display", "actor",
            "from_state", "to_state", "payload", "occurred_at",
        )
        read_only_fields = fields


class AppointInvestigatorSerializer(serializers.Serializer):
    """Triage input.

    ``lead`` accepts an employee id, or the literal ``"self"`` for HR taking
    the case themselves -- which per Q4 is the normal route for a minor
    complaint.
    """

    lead = serializers.CharField(
        help_text='Employee id, or "self" to assign the case to yourself.'
    )
    due_date = serializers.DateField(
        required=False,
        allow_null=True,
        help_text="When the investigation should conclude. Set by HR at triage.",
    )

    def validate_due_date(self, value):
        if value is None:
            return value
        from django.utils import timezone

        if value < timezone.localdate():
            raise serializers.ValidationError("The due date cannot be in the past.")
        return value

    def validate_lead(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Select who will investigate this.")

        employee = self.context["employee"]
        if value == "self":
            return employee

        lead = Employee.objects.filter(
            pk=value, organisation_id=employee.organisation_id, is_active=True
        ).first()
        if lead is None:
            raise serializers.ValidationError("No such employee in this organisation.")
        return lead


# ---------------------------------------------------------------------------
# Investigation
# ---------------------------------------------------------------------------


class InviteCollaboratorSerializer(serializers.Serializer):
    employee = serializers.PrimaryKeyRelatedField(queryset=Employee.objects.none())
    role = serializers.ChoiceField(
        choices=enums.CollaboratorRole.choices,
        default=enums.CollaboratorRole.OTHER,
    )

    def get_fields(self):
        fields = super().get_fields()
        organisation_id = self.context.get("organisation_id")
        if organisation_id is not None:
            fields["employee"].queryset = Employee.objects.filter(
                organisation_id=organisation_id, is_active=True
            )
        return fields


class RequestInformationSerializer(serializers.Serializer):
    prompt = serializers.CharField(max_length=5000)
    due_at = serializers.DateTimeField(required=False, allow_null=True)

    def validate_prompt(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Enter the question you want answered.")
        return value


class RespondToRequestSerializer(serializers.Serializer):
    body = serializers.CharField(max_length=20000)

    def validate_body(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Enter your response.")
        return value


class InformationResponseSerializer(serializers.ModelSerializer):
    responded_by = EmployeeBriefSerializer(read_only=True)

    class Meta:
        model = InformationResponse
        fields = ("id", "body", "responded_by", "responded_at")
        read_only_fields = fields


class InformationRequestSerializer(serializers.ModelSerializer):
    """The lead's view: question plus the whole answer thread."""

    responses = InformationResponseSerializer(many=True, read_only=True)
    requested_by = EmployeeBriefSerializer(read_only=True)
    collaborator_employee = EmployeeBriefSerializer(
        source="collaborator.employee", read_only=True
    )

    class Meta:
        model = InformationRequest
        fields = (
            "id", "prompt", "status", "requested_by", "requested_at",
            "due_at", "collaborator", "collaborator_employee", "responses",
        )
        read_only_fields = fields


class MyInformationRequestSerializer(serializers.ModelSerializer):
    """The collaborator's view. Deliberately, almost nothing.

    A person asked to give evidence sees the question, who asked it, and the
    case reference so they know what it concerns. They do **not** see the
    complaint description, who filed it, who else was asked, or any other
    answer. Widening this serializer is a privacy regression -- if a field
    seems useful here, check the spec before adding it.
    """

    requested_by = EmployeeBriefSerializer(read_only=True)
    complaint_reference = serializers.CharField(
        source="investigation.complaint.reference", read_only=True
    )

    class Meta:
        model = InformationRequest
        fields = (
            "id", "prompt", "requested_by", "requested_at",
            "due_at", "complaint_reference",
        )
        read_only_fields = fields


class MeetingSerializer(serializers.ModelSerializer):
    recorded_by = EmployeeBriefSerializer(read_only=True)
    attendees = serializers.SerializerMethodField()

    class Meta:
        model = InvestigationMeeting
        fields = (
            "id", "meeting_date", "findings", "recorded_by",
            "attendees", "created_at",
        )
        read_only_fields = fields

    @extend_schema_field(serializers.ListField(child=serializers.DictField()))
    def get_attendees(self, obj):
        return [
            EmployeeBriefSerializer(c.employee).data
            for c in obj.attendees.select_related("employee")
        ]


class RecordMeetingSerializer(serializers.Serializer):
    meeting_date = serializers.DateField()
    findings = serializers.CharField(max_length=50000)
    attendees = serializers.PrimaryKeyRelatedField(
        queryset=Employee.objects.none(), many=True, required=False
    )

    def get_fields(self):
        fields = super().get_fields()
        organisation_id = self.context.get("organisation_id")
        if organisation_id is not None:
            fields["attendees"].child_relation.queryset = Employee.objects.filter(
                organisation_id=organisation_id, is_active=True
            )
        return fields

    def validate_meeting_date(self, value):
        from django.utils import timezone

        if value > timezone.localdate():
            raise serializers.ValidationError("A meeting cannot be in the future.")
        return value

    def validate_findings(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Record what came out of the meeting.")
        return value


class InvestigationNoteSerializer(serializers.ModelSerializer):
    author = EmployeeBriefSerializer(read_only=True)

    class Meta:
        model = InvestigationNote
        fields = ("id", "body", "author", "created_at")
        read_only_fields = ("id", "author", "created_at")

    def validate_body(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("The note cannot be empty.")
        return value


class InvestigationDetailSerializer(InvestigationSerializer):
    """Everything the lead needs on one screen."""

    information_requests = serializers.SerializerMethodField()
    meetings = MeetingSerializer(many=True, read_only=True)
    notes = InvestigationNoteSerializer(many=True, read_only=True)
    attachments = AttachmentSerializer(many=True, read_only=True)
    complaint_reference = serializers.CharField(
        source="complaint.reference", read_only=True
    )

    class Meta(InvestigationSerializer.Meta):
        fields = InvestigationSerializer.Meta.fields + (
            "complaint", "complaint_reference", "information_requests",
            "meetings", "notes", "attachments",
        )
        read_only_fields = fields

    @extend_schema_field(InformationRequestSerializer(many=True))
    def get_information_requests(self, obj):
        queryset = obj.information_requests.select_related(
            "requested_by", "collaborator__employee"
        ).prefetch_related("responses__responded_by")
        return InformationRequestSerializer(queryset, many=True).data


# ---------------------------------------------------------------------------
# Decision and resolution
# ---------------------------------------------------------------------------


class FollowUpInputSerializer(serializers.Serializer):
    scheduled_date = serializers.DateField()
    kind = serializers.ChoiceField(
        choices=enums.FollowUpKind.choices, default=enums.FollowUpKind.CUSTOM
    )
    reminder_enabled = serializers.BooleanField(default=True)
    reminder_time = serializers.TimeField(required=False, allow_null=True)


class PIPInputSerializer(serializers.Serializer):
    """The optional PIP attached to a resolution."""

    start_date = serializers.DateField()
    end_date = serializers.DateField()
    trainings = serializers.PrimaryKeyRelatedField(
        queryset=Training.objects.none(), many=True, required=False
    )
    follow_ups = FollowUpInputSerializer(many=True, required=False)

    def get_fields(self):
        fields = super().get_fields()
        organisation_id = self.context.get("organisation_id")
        if organisation_id is not None:
            fields["trainings"].child_relation.queryset = Training.objects.filter(
                organisation_id=organisation_id, is_active=True
            )
        return fields

    def validate(self, attrs):
        if attrs["end_date"] < attrs["start_date"]:
            raise serializers.ValidationError(
                {"end_date": "The PIP cannot end before it starts."}
            )
        return attrs


class ResolveComplaintSerializer(serializers.Serializer):
    """HR's decision, and everything that follows from it.

    The conditional rules here mirror check constraints on the Resolution
    table. That duplication is intentional -- the constraint guarantees the
    data is sound, but reaching it means a 500. This layer turns the same rule
    into a field error.
    """

    decision = serializers.ChoiceField(choices=enums.InvestigationDecision.choices)
    resolution_type = serializers.ChoiceField(choices=enums.ResolutionType.choices)
    formal_resolution_type = serializers.ChoiceField(
        choices=enums.FormalResolutionType.choices, required=False, allow_blank=True
    )
    informal_resolution_type = serializers.ChoiceField(
        choices=enums.InformalResolutionType.choices, required=False, allow_blank=True
    )
    resolution_note = serializers.CharField(
        required=False, allow_blank=True, max_length=5000
    )
    decision_notes = serializers.CharField(max_length=20000)
    pip = PIPInputSerializer(required=False, allow_null=True)

    def validate_decision_notes(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError(
                "Record why this decision was reached."
            )
        return value

    def validate(self, attrs):
        errors: dict[str, str] = {}
        resolution_type = attrs["resolution_type"]
        formal = attrs.get("formal_resolution_type") or ""
        informal = attrs.get("informal_resolution_type") or ""

        if resolution_type == enums.ResolutionType.FORMAL:
            if not formal:
                errors["formal_resolution_type"] = (
                    "Select the type of formal resolution."
                )
            if informal:
                errors["informal_resolution_type"] = (
                    "Leave this blank for a formal resolution."
                )
        elif resolution_type == enums.ResolutionType.INFORMAL:
            if not informal:
                errors["informal_resolution_type"] = (
                    "Select the type of informal resolution."
                )
            if formal:
                errors["formal_resolution_type"] = (
                    "Leave this blank for an informal resolution."
                )
        else:
            if formal:
                errors["formal_resolution_type"] = "Leave this blank."
            if informal:
                errors["informal_resolution_type"] = "Leave this blank."

        chose_others = (
            formal == enums.FormalResolutionType.OTHERS
            or informal == enums.InformalResolutionType.OTHERS
        )
        if chose_others and not (attrs.get("resolution_note") or "").strip():
            errors["resolution_note"] = "Describe the resolution."

        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class FollowUpSerializer(serializers.ModelSerializer):
    class Meta:
        model = PIPFollowUp
        fields = (
            "id", "scheduled_date", "kind", "reminder_enabled",
            "reminder_time", "completed_at", "outcome_notes",
        )
        read_only_fields = fields


class PIPPlanSerializer(serializers.ModelSerializer):
    employee = EmployeeBriefSerializer(read_only=True)
    follow_ups = FollowUpSerializer(many=True, read_only=True)
    trainings = serializers.SerializerMethodField()

    class Meta:
        model = PIPPlan
        fields = (
            "id", "employee", "start_date", "end_date", "state",
            "trainings", "follow_ups",
        )
        read_only_fields = fields

    @extend_schema_field(serializers.ListField(child=serializers.DictField()))
    def get_trainings(self, obj):
        return [
            {"id": str(a.training_id), "name": a.training.name,
             "completed_at": a.completed_at}
            for a in obj.training_assignments.select_related("training")
        ]


class ResolutionSerializer(serializers.ModelSerializer):
    decided_by = EmployeeBriefSerializer(read_only=True)
    pip_plan = PIPPlanSerializer(read_only=True)
    decision_display = serializers.CharField(
        source="get_decision_display", read_only=True
    )

    class Meta:
        model = Resolution
        fields = (
            "id", "decision", "decision_display", "resolution_type",
            "formal_resolution_type", "informal_resolution_type",
            "resolution_note", "decision_notes", "decided_by", "decided_at",
            "pip_plan",
        )
        read_only_fields = fields


class WithdrawComplaintSerializer(serializers.Serializer):
    reason = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=2000,
        help_text="Optional. Recorded on the case history.",
    )


class ReopenComplaintSerializer(serializers.Serializer):
    """A reopened case needs an owner for the new round."""

    lead = serializers.CharField(
        help_text='Employee id, or "self" to take the new round yourself.'
    )
    reason = serializers.CharField(required=False, allow_blank=True, max_length=2000)

    def validate_lead(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError(
                "Select who will investigate this round."
            )

        employee = self.context["employee"]
        if value == "self":
            return employee

        lead = Employee.objects.filter(
            pk=value, organisation_id=employee.organisation_id, is_active=True
        ).first()
        if lead is None:
            raise serializers.ValidationError("No such employee in this organisation.")
        return lead
