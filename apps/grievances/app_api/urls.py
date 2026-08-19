"""MAKAY employee-app routes, mounted at /api/v1/app/.

Paths and (absence of) trailing slashes match the frontend spec exactly, so the
app can point its base URL at /api/v1/app and use the documented paths verbatim.
"""
from django.urls import path

from apps.grievances.app_api import views

app_name = "grievances_app_api"

urlpatterns = [
    path("complaints/types", views.ComplaintTypesView.as_view(), name="complaint-types"),
    path("complaints", views.ComplaintCollectionView.as_view(), name="complaints"),
    path("complaints/<uuid:pk>", views.ComplaintDetailView.as_view(), name="complaint-detail"),
    path("employees", views.EmployeeLookupView.as_view(), name="employees"),
    path("departments", views.DepartmentListView.as_view(), name="departments"),
]
