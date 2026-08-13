"""PIP endpoints.

A PIP outlives the complaint that produced it, so access here is HR-owned
rather than derived from complaint visibility. The employee under a plan can be
told about it, but marking their own check-in complete is not theirs to do.
"""
from __future__ import annotations

from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.grievances.access import employee_for
from apps.grievances.models import PIPFollowUp, PIPPlan
from apps.grievances.permissions import IsEmployee, IsHR
from apps.grievances.serializers import (
    CompleteFollowUpSerializer,
    FollowUpSerializer,
    PIPPlanSerializer,
)
from apps.grievances.services import ServiceError, complete_follow_up


class PIPPlanViewSet(viewsets.ReadOnlyModelViewSet):
    """Performance improvement plans."""

    permission_classes = [IsEmployee, IsHR]
    serializer_class = PIPPlanSerializer

    def get_queryset(self):
        employee = employee_for(self.request.user)
        if employee is None:
            return PIPPlan.objects.none()

        queryset = (
            PIPPlan.objects.filter(
                employee__organisation_id=employee.organisation_id
            )
            .select_related("employee")
            .prefetch_related("follow_ups", "training_assignments__training")
            .order_by("-start_date")
        )

        state = self.request.query_params.get("state")
        if state:
            queryset = queryset.filter(state=state)
        subject = self.request.query_params.get("employee")
        if subject:
            queryset = queryset.filter(employee_id=subject)
        return queryset

    @extend_schema(
        request=CompleteFollowUpSerializer,
        responses={200: FollowUpSerializer},
        description=(
            "Mark a check-in complete. Completing the last outstanding one "
            "closes the plan."
        ),
    )
    @action(
        detail=True,
        methods=["post"],
        url_path=r"follow-ups/(?P<follow_up_id>[^/.]+)/complete",
    )
    def complete_follow_up(self, request, pk=None, follow_up_id=None):
        plan = self.get_object()
        follow_up = PIPFollowUp.objects.filter(
            pk=follow_up_id, pip_plan=plan
        ).first()
        if follow_up is None:
            raise NotFound(_("No such check-in on this plan."))

        serializer = CompleteFollowUpSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            follow_up = complete_follow_up(
                follow_up=follow_up,
                actor=employee_for(request.user),
                outcome_notes=serializer.validated_data.get("outcome_notes", ""),
                request=request,
            )
        except ServiceError as exc:
            raise ValidationError({"detail": str(exc)}) from exc

        return Response(FollowUpSerializer(follow_up).data)
