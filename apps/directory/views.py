"""Directory lookups backing the employee, department and training pickers.

Read-only and tenant-scoped. These will be replaced by the platform's own
endpoints at merge -- see the GRIEVANCES_*_MODEL settings.
"""
from __future__ import annotations

from django.db.models import Q
from rest_framework import serializers, viewsets

from apps.directory.models import Department, Employee, Training
from apps.grievances.access import employee_for
from apps.grievances.permissions import IsEmployee


class EmployeeLookupSerializer(serializers.ModelSerializer):
    department_name = serializers.CharField(source="department.name", read_only=True)

    class Meta:
        model = Employee
        fields = ("id", "full_name", "job_title", "department", "department_name")
        read_only_fields = fields


class DepartmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Department
        fields = ("id", "name")
        read_only_fields = fields


class TrainingSerializer(serializers.ModelSerializer):
    class Meta:
        model = Training
        fields = ("id", "name", "description")
        read_only_fields = fields


class TenantScopedViewSet(viewsets.ReadOnlyModelViewSet):
    """Base that refuses to return anything outside the caller's tenant."""

    permission_classes = [IsEmployee]

    def get_queryset(self):
        employee = employee_for(self.request.user)
        if employee is None:
            return self.queryset.none()
        return self.queryset.filter(organisation_id=employee.organisation_id)


class EmployeeLookupViewSet(TenantScopedViewSet):
    """Powers Select Employee, Witness and Select Investigation Manager."""

    queryset = Employee.objects.filter(is_active=True).select_related("department")
    serializer_class = EmployeeLookupSerializer
    search_fields = ["full_name", "job_title", "email"]
    ordering = ["full_name"]

    def get_queryset(self):
        queryset = super().get_queryset()
        params = self.request.query_params

        search = (params.get("q") or "").strip()
        if search:
            queryset = queryset.filter(
                Q(full_name__icontains=search)
                | Q(job_title__icontains=search)
                | Q(email__icontains=search)
            )

        department = params.get("department")
        if department:
            queryset = queryset.filter(department_id=department)

        if params.get("exclude_self") in ("1", "true", "True"):
            me = employee_for(self.request.user)
            if me is not None:
                queryset = queryset.exclude(pk=me.pk)

        return queryset


class DepartmentViewSet(TenantScopedViewSet):
    queryset = Department.objects.all()
    serializer_class = DepartmentSerializer
    ordering = ["name"]


class TrainingViewSet(TenantScopedViewSet):
    """Training catalogue for the PIP flow."""

    queryset = Training.objects.filter(is_active=True)
    serializer_class = TrainingSerializer
    ordering = ["name"]
