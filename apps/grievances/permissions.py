"""DRF permission classes.

Thin wrappers over :mod:`apps.grievances.access`. They exist so views declare
intent rather than reimplement rules -- no view should ever decide visibility
for itself.

Object-level permissions alone are not enough. ``has_object_permission`` never
runs for list endpoints, so a view that filters nothing will happily return
every row. Always pair these with
:meth:`ComplaintAccessPolicy.visible_queryset` in ``get_queryset()``.
"""
from __future__ import annotations

from rest_framework import permissions

from apps.grievances.access import ComplaintAccessPolicy, employee_for


class IsEmployee(permissions.BasePermission):
    """Require an authenticated user with an Employee profile."""

    message = "No employee profile is linked to this account."

    def has_permission(self, request, view) -> bool:
        return employee_for(request.user) is not None


class CanViewComplaint(permissions.BasePermission):
    """Object-level read access, delegated to the policy."""

    message = "You do not have access to this complaint."

    def has_permission(self, request, view) -> bool:
        return employee_for(request.user) is not None

    def has_object_permission(self, request, view, obj) -> bool:
        return ComplaintAccessPolicy.can_view(obj, employee_for(request.user))


class IsHR(permissions.BasePermission):
    """Restrict an action to HR.

    Triage, decisions and closure are HR-only. Read access is a separate
    question -- use :class:`CanViewComplaint` for that.
    """

    message = "This action is restricted to HR."

    def has_permission(self, request, view) -> bool:
        employee = employee_for(request.user)
        return employee is not None and employee.is_hr


class IsInvestigationLead(permissions.BasePermission):
    """Restrict an action to the lead on the current round.

    Expects the object to be an Investigation, or something exposing
    ``investigation``.
    """

    message = "This action is restricted to the investigation lead."

    def has_object_permission(self, request, view, obj) -> bool:
        employee = employee_for(request.user)
        if employee is None:
            return False
        investigation = getattr(obj, "investigation", obj)
        return getattr(investigation, "lead_id", None) == employee.pk
