"""MAKAY employee-app views.

Thin translation over the domain. Reads go through
:meth:`ComplaintAccessPolicy.visible_queryset` exactly like the HR console, so
this surface can never show a row the policy would deny; writes go through
:func:`apps.grievances.services.file_complaint`. The only things new here are
wire-shape: param names, the ``{data,total}`` envelope, and 422 errors.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.directory.models import Department, Employee
from apps.grievances import enums
from apps.grievances.access import AccessLevel, ComplaintAccessPolicy, employee_for
from apps.grievances.app_api import mappings
from apps.grievances.app_api.errors import app_exception_handler
from apps.grievances.app_api.pagination import AppPagination
from apps.grievances.app_api.serializers import (
    AppComplaintCreateSerializer,
    AppComplaintDetailSerializer,
    AppComplaintListSerializer,
    AppComplaintRestrictedSerializer,
    AppEmployeeLookupSerializer,
)
from apps.grievances.events import record_event
from apps.grievances.models import Attachment, Complaint
from apps.grievances.permissions import IsEmployee
from apps.grievances.services import ServiceError, file_complaint


def _valid_uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


class AppView(APIView):
    """Shared base: employee-only, and the 422 error envelope."""

    permission_classes = [IsEmployee]

    def get_exception_handler(self):
        # Scopes the 422 rewrap to this surface only; the HR console keeps 400.
        return app_exception_handler

    @property
    def employee(self):
        return employee_for(self.request.user)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


class ComplaintTypesView(AppView):
    """GET /app/complaints/types?category=general|employee"""

    def get(self, request):
        category = request.query_params.get("category")
        # Category is echoed for the client's benefit; the list is the same six
        # either way (see mappings.complaint_type_options).
        return Response(
            {"category": category, "types": mappings.complaint_type_options()}
        )


class EmployeeLookupView(AppView):
    """GET /app/employees — the Select Employee / Select Witness picker."""

    def get(self, request):
        employee = self.employee
        queryset = Employee.objects.filter(
            organisation_id=employee.organisation_id, is_active=True
        ).select_related("department")

        # `search` is the spec's param; `q` is accepted too for parity with the
        # rest of the API.
        term = (request.query_params.get("search") or request.query_params.get("q") or "").strip()
        if term:
            term = term[:200]
            queryset = queryset.filter(
                Q(full_name__icontains=term)
                | Q(job_title__icontains=term)
                | Q(email__icontains=term)
            )

        department = request.query_params.get("department")
        if department and _valid_uuid(department):
            queryset = queryset.filter(department_id=department)

        exclude_id = request.query_params.get("exclude_id")
        if exclude_id and _valid_uuid(exclude_id):
            queryset = queryset.exclude(pk=exclude_id)

        queryset = queryset.order_by("full_name")

        paginator = AppPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        data = AppEmployeeLookupSerializer(page, many=True).data
        return paginator.get_paginated_response(data)


class DepartmentListView(AppView):
    """GET /app/departments — the department filter dropdown."""

    def get(self, request):
        employee = self.employee
        names = list(
            Department.objects.filter(organisation_id=employee.organisation_id)
            .order_by("name")
            .values_list("name", flat=True)
        )
        return Response({"data": names})


# ---------------------------------------------------------------------------
# Complaints
# ---------------------------------------------------------------------------


class ComplaintCollectionView(AppView):
    """GET /app/complaints (list) and POST /app/complaints (create)."""

    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def _base_queryset(self, employee):
        base = Complaint.objects.select_related(
            "complainant", "respondent", "filed_by"
        ).prefetch_related("attachments")
        return ComplaintAccessPolicy.visible_queryset(base, employee)

    # -- list ---------------------------------------------------------------

    def get(self, request):
        employee = self.employee
        queryset = self._base_queryset(employee)

        queryset = self._apply_relation(request, queryset, employee)
        queryset, empty = self._apply_filters(request, queryset)
        if empty:
            # An unknown filter token returns nothing rather than everything --
            # a typo must not silently widen the result set. Mirrors the HR API.
            queryset = queryset.none()
        queryset = self._apply_ordering(request, queryset)

        paginator = AppPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        data = AppComplaintListSerializer(
            page, many=True, context={"employee": employee}
        ).data
        return paginator.get_paginated_response(data)

    def _apply_relation(self, request, queryset, employee):
        # `view` is the spec's param; `tab=reported|against` is accepted too,
        # since the spec uses it in one section.
        view = request.query_params.get("view")
        if not view:
            tab = request.query_params.get("tab")
            view = {"reported": "reported_by_me", "against": "against_me"}.get(tab)

        if view == "reported_by_me":
            return queryset.filter(
                Q(complainant_id=employee.pk) | Q(filed_by_id=employee.pk)
            )
        if view == "against_me":
            return queryset.filter(respondent_id=employee.pk)
        return queryset

    def _apply_filters(self, request, queryset):
        params = request.query_params

        term = (params.get("search") or "").strip()
        if term:
            term = term[:200]
            queryset = queryset.filter(
                Q(reference__icontains=term)
                | Q(complainant__full_name__icontains=term)
                | Q(respondent__full_name__icontains=term)
            )

        raw_type = params.get("complaint_type")
        if raw_type:
            value = mappings.complaint_type_from_app(raw_type)
            if value is None:
                return queryset, True
            queryset = queryset.filter(complaint_type=value)

        raw_status = params.get("status")
        if raw_status:
            label = mappings.STATUS_TOKEN_TO_LABEL.get(raw_status.strip().lower())
            states = enums.STATUS_TO_STATES.get(label) if label else None
            if not states:
                return queryset, True
            queryset = queryset.filter(state__in=states)

        raw_stage = params.get("stage")
        if raw_stage:
            label = mappings.STAGE_TOKEN_TO_LABEL.get(raw_stage.strip().lower())
            states = enums.STAGE_TO_STATES.get(label) if label else None
            if not states:
                return queryset, True
            queryset = queryset.filter(state__in=states)

        return queryset, False

    def _apply_ordering(self, request, queryset):
        # The design sorts on the "Date Reported" column; map it to created_at.
        sort = (request.query_params.get("sort") or "").strip()
        field, _, direction = sort.partition(":")
        if field == "date_reported":
            return queryset.order_by(
                "created_at" if direction == "asc" else "-created_at"
            )
        return queryset.order_by("-created_at")

    # -- create -------------------------------------------------------------

    def post(self, request):
        employee = self.employee

        serializer = AppComplaintCreateSerializer(
            data=request.data, context={"employee": employee}
        )
        serializer.is_valid()
        errors = dict(serializer.errors)

        uploads = request.FILES.getlist("documents")
        document_errors = [
            message for message in (_validate_upload(u) for u in uploads) if message
        ]
        if document_errors:
            errors["documents"] = document_errors

        if errors:
            # -> 422 {"errors": {...}} via app_exception_handler.
            raise ValidationError(errors)

        resolved = serializer.validated_data["_resolved"]

        try:
            complaint = file_complaint(
                organisation=employee.organisation,
                filed_by=employee,
                source=enums.ComplaintSource.SELF,
                subject_type=resolved["subject_type"],
                complaint_type=resolved["complaint_type"],
                complaint_type_note=resolved["complaint_type_note"],
                description=resolved["description"],
                visibility=resolved["visibility"],
                complainant=employee,
                respondent=resolved["respondent"],
                frequency=resolved["frequency"],
                occurrence_count=resolved["occurrence_count"],
                incident_date=resolved["incident_date"],
                witnesses=resolved["witnesses"],
                request=request,
            )
        except ServiceError as exc:
            raise ValidationError({"detail": str(exc)}) from exc

        if uploads:
            self._attach_documents(complaint, uploads, employee, request)

        return Response(
            {
                "id": str(complaint.pk),
                "category": resolved["category"],
                "status": mappings.status_for(complaint.state),
                "stage": mappings.stage_for(complaint.state),
                "decision": mappings.decision_label_for(complaint),
                "created_at": complaint.created_at.isoformat(),
            },
            status=status.HTTP_201_CREATED,
        )

    def _attach_documents(self, complaint, uploads, employee, request):
        with transaction.atomic():
            for upload in uploads:
                safe_name = Path(upload.name or "").name
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


class ComplaintDetailView(AppView):
    """GET /app/complaints/{id} — the "View complaint" modal."""

    def get(self, request, pk):
        employee = self.employee
        base = Complaint.objects.select_related(
            "complainant", "respondent", "filed_by"
        ).prefetch_related("attachments")
        # get_object_or_404 over the *visible* queryset: a complaint the caller
        # may not see is indistinguishable from one that does not exist.
        visible = ComplaintAccessPolicy.visible_queryset(base, employee)
        complaint = get_object_or_404(visible, pk=pk)

        level = ComplaintAccessPolicy.access_level(complaint, employee)
        record_event(
            complaint,
            verb=enums.EventVerb.VIEWED,
            actor=employee,
            payload={"access_level": level.value, "surface": "makay_app"},
            request=request,
        )

        if level is AccessLevel.RESTRICTED:
            data = AppComplaintRestrictedSerializer(complaint).data
        else:
            data = AppComplaintDetailSerializer(complaint).data
        return Response(data)


def _validate_upload(upload):
    """Return an error message for a bad upload, or None if it is acceptable.

    Same allow-list as the HR attachment endpoint: bound the size, and require
    both the MIME type and the extension to be accepted, since the browser's
    content-type header is trivially spoofed.
    """
    if upload.size == 0:
        return "That file is empty."
    if upload.size > settings.GRIEVANCES_MAX_ATTACHMENT_BYTES:
        limit_mb = settings.GRIEVANCES_MAX_ATTACHMENT_BYTES // (1024 * 1024)
        return f"Files must be {limit_mb} MB or smaller."
    if upload.content_type not in settings.GRIEVANCES_ALLOWED_ATTACHMENT_TYPES:
        return "That file type is not accepted."
    suffix = Path(Path(upload.name or "").name).suffix.lower()
    if suffix not in settings.GRIEVANCES_ALLOWED_ATTACHMENT_EXTENSIONS:
        return f"Files ending {suffix or '?'} are not accepted."
    return None
