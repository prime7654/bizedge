"""Query filters for complaint lists.

The UI filters on Status and Stage, which are not stored -- they are derived
from ``state``. Those filters therefore translate a display value back to the
set of states it covers, using the reverse maps in :mod:`enums` so the mapping
has exactly one definition.
"""
from __future__ import annotations

import django_filters as filters
from django.db.models import Q

from apps.grievances import enums
from apps.grievances.models import Complaint


class ComplaintFilter(filters.FilterSet):
    """Filters shared by the employee app and the HR console.

    ``relation`` drives the employee tabs (Reported By You / Against You).
    ``source_tab`` drives the HR console tabs (By Employees / By HR).
    Both are applied on top of the access policy, never instead of it.
    """

    relation = filters.CharFilter(method="filter_relation")
    source_tab = filters.CharFilter(method="filter_source_tab")
    status = filters.CharFilter(method="filter_status")
    stage = filters.CharFilter(method="filter_stage")
    type = filters.CharFilter(field_name="complaint_type", lookup_expr="exact")
    reported_to = filters.CharFilter(field_name="visibility", lookup_expr="exact")
    q = filters.CharFilter(method="filter_search")
    date_from = filters.DateFilter(field_name="created_at", lookup_expr="date__gte")
    date_to = filters.DateFilter(field_name="created_at", lookup_expr="date__lte")

    class Meta:
        model = Complaint
        fields = ["state", "complaint_type", "visibility", "source", "subject_type"]

    # -- employee tabs -----------------------------------------------------

    def filter_relation(self, queryset, name, value):
        employee = self.request_employee
        if employee is None:
            return queryset.none()

        if value == "reported_by_me":
            return queryset.filter(
                Q(complainant_id=employee.pk) | Q(filed_by_id=employee.pk)
            )
        if value == "against_me":
            # The access policy already hides complaints the respondent should
            # not yet see, so this needs no state clause of its own.
            return queryset.filter(respondent_id=employee.pk)
        return queryset

    # -- HR console tabs ---------------------------------------------------

    def filter_source_tab(self, queryset, name, value):
        if value == enums.SOURCE_TAB_EMPLOYEE:
            return queryset.filter(source=enums.ComplaintSource.SELF)
        if value == enums.SOURCE_TAB_HR:
            return queryset.filter(
                source__in=[
                    enums.ComplaintSource.HR_FOR_EMPLOYEE,
                    enums.ComplaintSource.HR_FOR_COMPANY,
                ]
            )
        return queryset

    # -- derived display fields -------------------------------------------

    def filter_status(self, queryset, name, value):
        states = enums.STATUS_TO_STATES.get(value)
        return queryset.filter(state__in=states) if states else queryset.none()

    def filter_stage(self, queryset, name, value):
        states = enums.STAGE_TO_STATES.get(value)
        return queryset.filter(state__in=states) if states else queryset.none()

    # -- search ------------------------------------------------------------

    def filter_search(self, queryset, name, value):
        """Free-text search.

        Deliberately does not search ``description``. Complaint descriptions
        contain the substance of an allegation, and a search that matched on
        them would let someone with access to a list probe for content in
        cases they can only partly see.
        """
        value = (value or "").strip()
        if not value:
            return queryset
        return queryset.filter(
            Q(reference__icontains=value)
            | Q(complainant__full_name__icontains=value)
            | Q(respondent__full_name__icontains=value)
        )

    @property
    def request_employee(self):
        request = getattr(self, "request", None)
        return getattr(request, "grievance_employee", None) if request else None
