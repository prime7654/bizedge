"""Auth routes, mounted at /api/v1/auth/. See apps.grievances.auth_views.

Platform-level, not grievances-specific -- both the employee app and the HR
console log in here. Lives in this app only because it is the standalone home
today; it moves to the platform at merge.
"""
from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from apps.grievances.auth_views import MeView

app_name = "auth"

urlpatterns = [
    path("token/", TokenObtainPairView.as_view(), name="token-obtain"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token-refresh"),
    path("me/", MeView.as_view(), name="me"),
]
