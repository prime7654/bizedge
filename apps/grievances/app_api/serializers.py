"""Request and response shapes for the MAKAY employee app.

These serializers exist only to translate. Domain validation and every write
still go through :mod:`apps.grievances.services`; the create serializer here
re-expresses the same rule table (spec v4 section 5) with the app's field names
so inline errors land on the right inputs.

Response serializers use ``to_representation`` for exact control over the wire
keys the design asks for (``dateReported``, ``filedAgainst``, ``reportedTo`` and
so on -- not merely camelCased domain names).
"""
from __future__ import annotations

import uuid

from django.utils import timezone
from rest_framework import serializers

# Mirrors apps.grievances.serializers, which already imports the directory
# models directly. Both are rewired to the platform models at merge via the
# GRIEVANCES_*_MODEL settings.
from apps.directory.models import Employee
from apps.grievances import enums
from apps.grievances.app_api import mappings


def _valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _person(employee):
    """The {id, name, title} shape the app uses for a respondent."""
    if employee is None:
        return None
    return {
        "id": str(employee.pk),
        "name": employee.full_name,
        "title": employee.job_title or None,
    }


def _file_url(attachment):
    """Best-effort URL for an attachment.

    Dev only: returns the storage URL. Production needs a permission-checked,
    signed short-lived URL -- that endpoint is on the outstanding list, and this
    is where it plugs in. Never leak a guessable path once real evidence lands.
    """
    try:
        return attachment.file.url
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


class AppEmployeeLookupSerializer(serializers.BaseSerializer):
    """The employee picker row: {id, full_name, avatar_url, role_title, department}."""

    def to_representation(self, employee):
        return {
            "id": str(employee.pk),
            "full_name": employee.full_name,
            # No avatar on the standalone Employee stub. Wired to the platform
            # profile at merge; null until then so the client can fall back.
            "avatar_url": None,
            "role_title": employee.job_title or None,
            "department": employee.department.name if employee.department_id else None,
        }


class AppComplaintListSerializer(serializers.BaseSerializer):
    """One row of the Complaints table."""

    def to_representation(self, complaint):
        return {
            "id": str(complaint.pk),
            "dateReported": complaint.created_at.date().isoformat(),
            "complaintType": complaint.get_complaint_type_display(),
            # Null for a general complaint; the client renders "N/A".
            "filedAgainst": (
                complaint.respondent.full_name if complaint.respondent_id else None
            ),
            "status": mappings.status_for(complaint.state),
            "stage": mappings.stage_for(complaint.state),
            "decision": mappings.decision_label_for(complaint),
        }


class AppComplaintDetailSerializer(serializers.BaseSerializer):
    """Full detail, for anyone with FULL access to the complaint."""

    def to_representation(self, complaint):
        return {
            "id": str(complaint.pk),
            "scope": mappings.SUBJECT_TYPE_TO_CATEGORY[complaint.subject_type],
            "complaintType": complaint.get_complaint_type_display(),
            "respondent": _person(complaint.respondent),
            "incidentDate": (
                complaint.incident_date.isoformat() if complaint.incident_date else None
            ),
            "frequency": mappings.FREQUENCY_TO_APP.get(complaint.frequency),
            "occurrenceCount": complaint.occurrence_count,
            "description": complaint.description,
            "reportedTo": self._reported_to(complaint),
            "documents": [
                {
                    "id": str(a.pk),
                    "fileName": a.original_filename,
                    "fileUrl": _file_url(a),
                }
                for a in complaint.attachments.all()
            ],
            "status": mappings.status_for(complaint.state),
            "stage": mappings.stage_for(complaint.state),
            "decision": mappings.decision_label_for(complaint),
        }

    def _reported_to(self, complaint):
        """Who the complaint was routed to, per its effective visibility.

        ASSUMPTION (flagged for Product): the design shows a person here. The
        domain stores a visibility, not an HR person, so HR/BOTH render as a
        label ({name: "HR"}) and LINE_MANAGER as the complainant's *current*
        line manager. An orphaned LINE_MANAGER complaint has fallen back to HR,
        so it reads "HR" too. Confirm the intended shape before the frontend
        leans on it.
        """
        if complaint.visibility in (enums.Visibility.HR, enums.Visibility.BOTH):
            return {"id": None, "name": "HR", "role": "HR"}
        if complaint.visibility == enums.Visibility.LINE_MANAGER:
            complainant = complaint.complainant
            manager = getattr(complainant, "line_manager", None) if complainant else None
            if manager is not None and manager.pk != complaint.respondent_id:
                return {
                    "id": str(manager.pk),
                    "name": manager.full_name,
                    "role": "Line Manager",
                }
            # Orphan fallback: nobody on the line-manager route, so HR holds it.
            return {"id": None, "name": "HR", "role": "HR"}
        return None


class AppComplaintRestrictedSerializer(serializers.BaseSerializer):
    """What a respondent sees on the "Against You" detail.

    A separate shape, not the full one with keys removed -- mirrors the domain's
    ComplaintRestrictedSerializer. Deliberately omits reportedTo (routing),
    documents, and anything identifying the complainant.
    """

    def to_representation(self, complaint):
        return {
            "id": str(complaint.pk),
            "scope": mappings.SUBJECT_TYPE_TO_CATEGORY[complaint.subject_type],
            "complaintType": complaint.get_complaint_type_display(),
            "respondent": _person(complaint.respondent),
            "incidentDate": (
                complaint.incident_date.isoformat() if complaint.incident_date else None
            ),
            "frequency": mappings.FREQUENCY_TO_APP.get(complaint.frequency),
            "occurrenceCount": complaint.occurrence_count,
            "description": complaint.description,
            "status": mappings.status_for(complaint.state),
            "stage": mappings.stage_for(complaint.state),
            "decision": mappings.decision_label_for(complaint),
        }


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


class AppComplaintCreateSerializer(serializers.Serializer):
    """The "File A Complaint" form, both paths (general + about an employee).

    Accepts the app's vocabulary (``category``, ``respondent_id``, ``witness_ids``,
    lowercase enum tokens, complaint-type labels) and resolves it to the domain
    values the service expects. Errors are keyed by the app's own field names.
    """

    category = serializers.ChoiceField(choices=["general", "employee"])
    complaint_type = serializers.CharField(max_length=200)
    complaint_type_note = serializers.CharField(
        required=False, allow_blank=True, max_length=2000
    )
    respondent_id = serializers.CharField(
        required=False, allow_blank=True, allow_null=True
    )
    frequency = serializers.CharField(required=False, allow_blank=True, max_length=40)
    occurrence_count = serializers.IntegerField(
        required=False, allow_null=True, min_value=1
    )
    incident_date = serializers.DateField(required=False, allow_null=True)
    description = serializers.CharField(max_length=20000)
    visibility = serializers.CharField(max_length=40)
    witness_ids = serializers.ListField(
        child=serializers.CharField(), required=False
    )

    def validate(self, attrs):
        employee = self.context["employee"]
        errors: dict[str, str] = {}
        in_tenant = Employee.objects.filter(
            organisation_id=employee.organisation_id, is_active=True
        )

        subject_type = mappings.CATEGORY_TO_SUBJECT_TYPE[attrs["category"]]

        complaint_type = mappings.complaint_type_from_app(attrs.get("complaint_type"))
        if complaint_type is None:
            errors["complaint_type"] = "Select a valid complaint type."

        visibility = mappings.VISIBILITY_FROM_APP.get(
            (attrs.get("visibility") or "").strip().lower()
        )
        if visibility is None:
            errors["visibility"] = "Select who can see this complaint."

        respondent = None
        frequency = ""

        if subject_type == enums.SubjectType.EMPLOYEE:
            rid = (attrs.get("respondent_id") or "").strip()
            if not rid:
                errors["respondent_id"] = "Select the employee this complaint is about."
            else:
                respondent = in_tenant.filter(pk=rid).first() if _valid_uuid(rid) else None
                if respondent is None:
                    errors["respondent_id"] = "No such employee in your organisation."
                elif respondent.pk == employee.pk:
                    errors["respondent_id"] = "You cannot file a complaint against yourself."

            if not attrs.get("incident_date"):
                errors["incident_date"] = "Enter the date this happened."

            freq_token = (attrs.get("frequency") or "").strip().lower()
            if not freq_token:
                errors["frequency"] = "Select how often this happened."
            else:
                frequency = mappings.FREQUENCY_FROM_APP.get(freq_token) or ""
                if not frequency:
                    errors["frequency"] = "Select how often this happened."

            if frequency == enums.Frequency.REPEAT_BEHAVIOR and not attrs.get(
                "occurrence_count"
            ):
                errors["occurrence_count"] = "Enter how many times this happened."
        else:
            if (attrs.get("respondent_id") or "").strip():
                errors["respondent_id"] = (
                    "A general complaint cannot name a specific person."
                )

        incident_date = attrs.get("incident_date")
        if incident_date and incident_date > timezone.localdate():
            errors["incident_date"] = "The incident date cannot be in the future."

        # OTHERS needs a note. The design says "please specify in the
        # description", so fall back to the description when no explicit note is
        # given -- this satisfies the DB constraint without a second box.
        note = (attrs.get("complaint_type_note") or "").strip()
        if complaint_type == enums.ComplaintType.OTHERS and not note:
            note = (attrs.get("description") or "").strip()

        witnesses = []
        unknown_witnesses = []
        for wid in attrs.get("witness_ids") or []:
            wid = (wid or "").strip()
            match = in_tenant.filter(pk=wid).first() if _valid_uuid(wid) else None
            if match is None:
                unknown_witnesses.append(wid)
            else:
                witnesses.append(
                    {"witness_type": enums.WitnessType.EMPLOYEE, "employee": match}
                )
        if unknown_witnesses:
            errors["witness_ids"] = (
                f"Unknown employee(s): {', '.join(unknown_witnesses)}."
            )

        if errors:
            raise serializers.ValidationError(errors)

        attrs["_resolved"] = {
            "subject_type": subject_type,
            "complaint_type": complaint_type,
            "complaint_type_note": note,
            "visibility": visibility,
            "respondent": respondent,
            "frequency": frequency,
            "occurrence_count": attrs.get("occurrence_count"),
            "incident_date": incident_date,
            "description": attrs["description"],
            "witnesses": witnesses,
            "category": attrs["category"],
        }
        return attrs
