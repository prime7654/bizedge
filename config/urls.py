from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

urlpatterns = [
    path("admin/", admin.site.urls),
    # MAKAY employee app -- a frontend-shaped compatibility surface over the
    # same domain (see apps/grievances/app_api). Kept on its own base path so
    # the HR console contract on /api/v1/ stays exactly as it was. Listed first
    # so the more specific /api/v1/app/ prefix resolves before /api/v1/.
    path("api/v1/app/", include("apps.grievances.app_api.urls")),
    # JWT auth for the SPA frontends: obtain / refresh a Bearer token, and `me`.
    path("api/v1/auth/", include("apps.grievances.auth_urls")),
    path("api/v1/", include("apps.grievances.urls")),
    path("api/v1/", include("apps.directory.urls")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    # Session login for DRF's browsable API, so the endpoints can be explored
    # in a browser without an admin account. Grants no permissions of its own --
    # whoever logs in still sees exactly what the access policy allows, which is
    # the point: it makes the per-role differences visible.
    path("api-auth/", include("rest_framework.urls")),
]
