"""Auth routes, mounted at /api/v1/auth/. See apps.grievances.auth_views.

Trailing slash is optional on every route, matching the app_api convention, so
`/auth/token` and `/auth/token/` both work.

Platform-level, not grievances-specific -- both the employee app and the HR
console log in here. Lives in this app only because it is the standalone home
today; it moves to the platform at merge.
"""
from django.urls import re_path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from apps.grievances.auth_views import MeView

app_name = "auth"

urlpatterns = [
    re_path(r"^token/?$", TokenObtainPairView.as_view(), name="token-obtain"),
    re_path(r"^token/refresh/?$", TokenRefreshView.as_view(), name="token-refresh"),
    re_path(r"^me/?$", MeView.as_view(), name="me"),
]
