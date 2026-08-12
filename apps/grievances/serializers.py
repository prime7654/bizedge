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
from rest_framework import serializers
from apps.directory.models import Department, Employee
from apps.grievances import enums
from apps.grievances.access import AccessLevel, ComplaintAccessPolicy
from apps.grievances.models import Attachment, Complaint, ComplaintWitness


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

    def get_respondent(self, obj):
        if obj.respondent is None:
            return None
        return EmployeeBriefSerializer(obj.respondent).data

    def get_complainant(self, obj):
        """Masked from the respondent unless HR has released the identity."""
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

    class Meta(ComplaintListSerializer.Meta):
        fields = ComplaintListSerializer.Meta.fields + (
            "description", "incident_date", "frequency", "occurrence_count",
            "filed_by", "witnesses", "attachments", "visibility_requested",
            "complainant_identity_released",
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
