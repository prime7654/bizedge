"""STUB models standing in for platform-owned data.

These exist so the grievances module runs and is testable on its own. When
this merges into BizEdge / MAKAY, point the GRIEVANCES_*_MODEL settings at the
real models and delete this app. Nothing in apps.grievances imports from here
directly.

Deliberately minimal: only the fields the grievances module actually needs.
"""
import uuid

from django.conf import settings
from django.db import models

from apps.core.models import Organisation, TimeStampedModel


class Department(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey(
        Organisation, on_delete=models.CASCADE, related_name="departments"
    )
    name = models.CharField(max_length=255)

    class Meta:
        ordering = ("name",)
        constraints = [
            models.UniqueConstraint(
                fields=("organisation", "name"), name="uniq_department_name_per_org"
            )
        ]

    def __str__(self) -> str:
        return self.name


class Employee(TimeStampedModel):
    """An employee.

    ``line_manager`` is the org-chart relationship. Spec v4 Q3: an employee has
    one line manager at a time, and complaint visibility follows the *current*
    manager rather than one frozen at filing. So this field is the single
    source of truth and is resolved at query time, never copied onto a case.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey(
        Organisation, on_delete=models.CASCADE, related_name="employees"
    )
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="employee_profile",
        null=True,
        blank=True,
    )
    full_name = models.CharField(max_length=255)
    email = models.EmailField()
    job_title = models.CharField(max_length=255, blank=True)
    department = models.ForeignKey(
        Department,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="employees",
    )
    line_manager = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="direct_reports",
        help_text="Current line manager. Grievance visibility follows this field.",
    )
    is_hr = models.BooleanField(
        default=False,
        help_text="HR users see every complaint filed to HR across the organisation.",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("full_name",)
        indexes = [
            models.Index(fields=("organisation", "is_hr")),
            models.Index(fields=("line_manager",)),
        ]

    def __str__(self) -> str:
        return self.full_name


class Training(TimeStampedModel):
    """Training courses assignable as part of a PIP."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey(
        Organisation, on_delete=models.CASCADE, related_name="trainings"
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name
