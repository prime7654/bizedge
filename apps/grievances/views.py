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

from pathlib import Path

from django.db.models import Count, Q
from django.db import transaction
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from apps.grievances import enums
from apps.grievances.access import AccessLevel, ComplaintAccessPolicy, employee_for
from apps.grievances.events import record_event
from apps.grievances import metadata as metadata_module
from apps.grievances.filters import ComplaintFilter
from apps.grievances.models import Attachment, Complaint
from apps.grievances.permissions import CanViewComplaint, IsEmployee, IsHR
from apps.grievances.serializers import (
    AppointInvestigatorSerializer,
    InvestigationSerializer,
    AttachmentSerializer,
    ComplaintEventSerializer,
    ComplaintCreateSerializer,
    ComplaintDetailSerializer,
    ComplaintListSerializer,
    serializer_for_access,
)
from apps.grievances.services import ServiceError, file_complaint
from apps.grievances.services import (
    appoint_investigator as appoint_investigator_service,
)
from apps.grievances.transitions import TransitionError


class ComplaintViewSet(viewsets.ReadOnlyModelViewSet):
    """Complaints.

    Read and create only for now. State changes arrive in later steps as
    explicit actions rather than PATCH, so that every transition runs through
    the service layer and writes an audit row.
    """

    permission_classes = [IsEmployee, CanViewComplaint]
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    filterset_class = ComplaintFilter
    ordering_fields = ["created_at", "due_date", "state", "reference"]
    ordering = ["-created_at"]

    def initial(self, request, *args, **kwargs):
        """Resolve the employee once and hang it off the request.

        The filterset needs it and only receives the request, so this avoids
        every filter method re-resolving the profile.
        """
        super().initial(request, *args, **kwargs)
        request.grievance_employee = employee_for(request.user)

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

        if upload.size == 0:
            raise ValidationError({"file": _("That file is empty.")})
        if upload.size > settings.GRIEVANCES_MAX_ATTACHMENT_BYTES:
            limit_mb = settings.GRIEVANCES_MAX_ATTACHMENT_BYTES // (1024 * 1024)
            raise ValidationError(
                {"file": _("Files must be %(limit)s MB or smaller.") % {"limit": limit_mb}}
            )

        # content_type is a client-supplied header and trivially spoofed, so
        # the extension is checked as well. Neither is proof of what is inside
        # the file -- real content sniffing belongs with the AV scan, which is
        # not built yet.
        if upload.content_type not in settings.GRIEVANCES_ALLOWED_ATTACHMENT_TYPES:
            raise ValidationError({"file": _("That file type is not accepted.")})

        safe_name = Path(upload.name or "").name  # strips any ../ path components
        suffix = Path(safe_name).suffix.lower()
        if suffix not in settings.GRIEVANCES_ALLOWED_ATTACHMENT_EXTENSIONS:
            raise ValidationError(
                {"file": _("Files ending %(ext)s are not accepted.") % {"ext": suffix or "?"}}
            )

        with transaction.atomic():
            attachment = Attachment.objects.create(
                owner=complaint,
                file=upload,
                original_filename=safe_name[:512],
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

    @extend_schema(
        responses={200: dict},
        description=(
            "Every option set and conditional form rule in one call. Fetch on "
            "load rather than hardcoding dropdowns client-side."
        ),
    )
    @action(
        detail=False,
        methods=["get"],
        permission_classes=[IsAuthenticated],
        filter_backends=[],
        pagination_class=None,
    )
    def metadata(self, request):
        return Response(metadata_module.complaint_metadata())

    @extend_schema(
        responses={200: dict},
        description="Counts for the list tabs and status filters.",
    )
    @action(detail=False, methods=["get"], filter_backends=[], pagination_class=None)
    def summary(self, request):
        """Tab and status counts, computed over what the caller may see.

        Deliberately reuses ``get_queryset()`` so the counts can never disagree
        with the lists they label -- a badge showing 5 above a list of 3 is a
        support ticket.
        """
        employee = employee_for(request.user)
        visible = self.get_queryset()

        totals = visible.aggregate(
            reported_by_me=Count(
                "pk",
                filter=Q(complainant_id=employee.pk) | Q(filed_by_id=employee.pk),
                distinct=True,
            ),
            against_me=Count(
                "pk", filter=Q(respondent_id=employee.pk), distinct=True
            ),
            by_employees=Count(
                "pk", filter=Q(source=enums.ComplaintSource.SELF), distinct=True
            ),
            by_hr=Count(
                "pk",
                filter=Q(
                    source__in=[
                        enums.ComplaintSource.HR_FOR_EMPLOYEE,
                        enums.ComplaintSource.HR_FOR_COMPANY,
                    ]
                ),
                distinct=True,
            ),
        )

        by_state = {
            row["state"]: row["n"]
            for row in visible.values("state").annotate(n=Count("pk"))
        }
        by_status: dict[str, int] = {}
        for status_label, states in enums.STATUS_TO_STATES.items():
            by_status[status_label] = sum(by_state.get(st, 0) for st in states)

        return Response(
            {
                "total": visible.count(),
                "tabs": totals,
                "by_state": by_state,
                "by_status": by_status,
            }
        )

    @extend_schema(
        responses={200: ComplaintEventSerializer(many=True)},
        description="Case history. Requires full access to the complaint.",
    )
    @action(detail=True, methods=["get"], filter_backends=[], pagination_class=None)
    def timeline(self, request, pk=None):
        """The audit trail for one complaint.

        Restricted to people with FULL access. A respondent seeing the timeline
        would learn who was invited, who gave evidence and when -- far more
        than the allegation itself.
        """
        complaint = self.get_object()
        employee = employee_for(request.user)

        if ComplaintAccessPolicy.access_level(complaint, employee) is not AccessLevel.FULL:
            raise PermissionDenied(_("You cannot view the history of this complaint."))

        events = complaint.events.select_related("actor").order_by("-occurred_at")
        return Response(ComplaintEventSerializer(events, many=True).data)

    @extend_schema(
        request=AppointInvestigatorSerializer,
        responses={200: ComplaintDetailSerializer},
        description=(
            "Appoint an investigation lead and open the case. HR only. Pass "
            '`\"self\"` as the lead to take the case yourself, which is the '
            "normal route for a minor complaint. This is the point at which "
            "the respondent can first see the complaint, and after which it "
            "can no longer be deleted."
        ),
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="appoint-investigator",
        permission_classes=[IsEmployee, IsHR, CanViewComplaint],
        filter_backends=[],
    )
    def appoint_investigator(self, request, pk=None):
        complaint = self.get_object()
        employee = employee_for(request.user)

        serializer = AppointInvestigatorSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)

        try:
            appoint_investigator_service(
                complaint=complaint,
                actor=employee,
                lead=serializer.validated_data["lead"],
                due_date=serializer.validated_data.get("due_date"),
                request=request,
            )
        except TransitionError as exc:
            # 409: the request was well-formed, the case had simply moved on.
            # Usually a double submission from a slow modal.
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except ServiceError as exc:
            raise ValidationError({"detail": str(exc)}) from exc

        complaint.refresh_from_db()
        return Response(
            ComplaintDetailSerializer(
                complaint, context=self.get_serializer_context()
            ).data
        )

    @extend_schema(
        responses={200: InvestigationSerializer(many=True)},
        description="Investigation rounds on this complaint, oldest first.",
    )
    @action(detail=True, methods=["get"], filter_backends=[], pagination_class=None)
    def investigations(self, request, pk=None):
        """Every round, including superseded ones.

        A reopened case keeps its earlier rounds intact -- the record has to
        show what was decided the first time and on what basis.
        """
        complaint = self.get_object()
        employee = employee_for(request.user)

        if ComplaintAccessPolicy.access_level(complaint, employee) is not AccessLevel.FULL:
            raise PermissionDenied(_("You cannot view the investigation."))

        rounds = complaint.investigations.select_related("lead").prefetch_related(
            "collaborators__employee"
        ).order_by("round")
        return Response(InvestigationSerializer(rounds, many=True).data)
