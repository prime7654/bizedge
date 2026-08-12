"""Complaint API.

Two rules govern everything in this module:

1. ``get_queryset`` always goes through
   :meth:`ComplaintAccessPolicy.visible_queryset`. Object-level permissions do
   not run for list endpoints, so an unfiltered queryset is a leak, not a bug
   to be caught later by a permission class.
2. Response shape comes from the access *level*, never from the role. A
   respondent who is also HR must not receive the full payload.
"""
from __future__ import annotations

from django.db import transaction
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from apps.grievances import enums
from apps.grievances.access import AccessLevel, ComplaintAccessPolicy, employee_for
from apps.grievances.events import record_event
from apps.grievances.models import Attachment, Complaint
from apps.grievances.permissions import CanViewComplaint, IsEmployee
from apps.grievances.serializers import (
    AttachmentSerializer,
    ComplaintCreateSerializer,
    ComplaintDetailSerializer,
    ComplaintListSerializer,
    serializer_for_access,
)
from apps.grievances.services import ServiceError, file_complaint


class ComplaintViewSet(viewsets.ReadOnlyModelViewSet):
    """Complaints.

    Read and create only for now. State changes arrive in later steps as
    explicit actions rather than PATCH, so that every transition runs through
    the service layer and writes an audit row.
    """

    permission_classes = [IsEmployee, CanViewComplaint]
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get_queryset(self):
        """Never return an unfiltered queryset. See the module docstring."""
        employee = employee_for(self.request.user)
        base = Complaint.objects.select_related(
            "complainant", "respondent", "filed_by"
        ).prefetch_related("witnesses", "attachments")
        return ComplaintAccessPolicy.visible_queryset(base, employee).order_by(
            "-created_at"
        )

    def get_serializer_class(self):
        if self.action == "create":
            return ComplaintCreateSerializer
        if self.action == "list":
            return ComplaintListSerializer
        return ComplaintDetailSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        employee = employee_for(self.request.user)
        context["employee"] = employee
        context["organisation_id"] = getattr(employee, "organisation_id", None)
        return context

    def retrieve(self, request, *args, **kwargs):
        """Shape the response by access level, not by role."""
        complaint = self.get_object()
        employee = employee_for(request.user)
        level = ComplaintAccessPolicy.access_level(complaint, employee)

        serializer_class = serializer_for_access(level)
        serializer = serializer_class(complaint, context=self.get_serializer_context())

        record_event(
            complaint,
            verb=enums.EventVerb.VIEWED,
            actor=employee,
            payload={"access_level": level.value},
            request=request,
        )
        return Response(serializer.data)

    @extend_schema(
        request=ComplaintCreateSerializer,
        responses={201: ComplaintDetailSerializer},
        description=(
            "File a complaint. Covers all four routes: an employee filing for "
            "themselves, an employee filing about a colleague, HR filing on "
            "behalf of an employee, and HR filing on behalf of the company."
        ),
    )
    def create(self, request, *args, **kwargs):
        employee = employee_for(request.user)
        serializer = ComplaintCreateSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        source = data["source"]
        complainant = (
            employee if source == enums.ComplaintSource.SELF else data.get("complainant")
        )

        try:
            complaint = file_complaint(
                organisation=employee.organisation,
                filed_by=employee,
                source=source,
                subject_type=data["subject_type"],
                complaint_type=data["complaint_type"],
                complaint_type_note=data.get("complaint_type_note", ""),
                description=data["description"],
                visibility=data["visibility"],
                complainant=complainant,
                respondent=data.get("respondent"),
                frequency=data.get("frequency", ""),
                occurrence_count=data.get("occurrence_count"),
                incident_date=data.get("incident_date"),
                witnesses=data.get("witnesses", []),
                request=request,
            )
        except ServiceError as exc:
            raise ValidationError({"detail": str(exc)}) from exc

        output = ComplaintDetailSerializer(
            complaint, context=self.get_serializer_context()
        )
        return Response(output.data, status=status.HTTP_201_CREATED)

    @extend_schema(
        request={"multipart/form-data": {"type": "object", "properties": {
            "file": {"type": "string", "format": "binary"}}}},
        responses={201: AttachmentSerializer},
        description="Attach evidence to a complaint.",
    )
    @action(detail=True, methods=["post"], url_path="attachments")
    def add_attachment(self, request, pk=None):
        """Attach a file.

        Only while the complaint is still open for edits, and only for people
        who can see it. Uploads are validated against the allow-list in
        settings -- grievance evidence goes to private storage and is served
        through a signed URL, never a guessable path.
        """
        complaint = self.get_object()
        employee = employee_for(request.user)

        if ComplaintAccessPolicy.access_level(complaint, employee) is AccessLevel.RESTRICTED:
            raise PermissionDenied(_("You cannot attach files to this complaint."))

        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": _("No file was uploaded.")})

        from django.conf import settings

        if upload.size > settings.GRIEVANCES_MAX_ATTACHMENT_BYTES:
            limit_mb = settings.GRIEVANCES_MAX_ATTACHMENT_BYTES // (1024 * 1024)
            raise ValidationError(
                {"file": _("Files must be %(limit)s MB or smaller.") % {"limit": limit_mb}}
            )
        if upload.content_type not in settings.GRIEVANCES_ALLOWED_ATTACHMENT_TYPES:
            raise ValidationError(
                {"file": _("That file type is not accepted.")}
            )

        with transaction.atomic():
            attachment = Attachment.objects.create(
                owner=complaint,
                file=upload,
                original_filename=upload.name[:512],
                content_type_header=upload.content_type or "",
                size_bytes=upload.size,
                uploaded_by=employee,
            )
            record_event(
                complaint,
                verb=enums.EventVerb.ATTACHMENT_ADDED,
                actor=employee,
                payload={"filename": attachment.original_filename},
                request=request,
            )

        return Response(
            AttachmentSerializer(attachment).data, status=status.HTTP_201_CREATED
        )
