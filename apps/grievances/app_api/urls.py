"""MAKAY employee-app routes, mounted at /api/v1/app/.

Paths match the frontend spec, and the **trailing slash is optional** on every
route -- `/employees` and `/employees/` both resolve. The spec's paths omit the
slash, but the rest of the API (the DRF router) uses one, so tolerating both
spares the frontend a class of confusing 404s.
"""
from django.urls import re_path

from apps.grievances.app_api import views

app_name = "grievances_app_api"

urlpatterns = [
    re_path(r"^complaints/types/?$", views.ComplaintTypesView.as_view(), name="complaint-types"),
    re_path(r"^complaints/?$", views.ComplaintCollectionView.as_view(), name="complaints"),
    re_path(
        r"^complaints/(?P<pk>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/?$",
        views.ComplaintDetailView.as_view(),
        name="complaint-detail",
    ),
    re_path(r"^employees/?$", views.EmployeeLookupView.as_view(), name="employees"),
    re_path(r"^departments/?$", views.DepartmentListView.as_view(), name="departments"),
]
