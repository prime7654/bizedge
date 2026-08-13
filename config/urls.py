from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

urlpatterns = [
    path("admin/", admin.site.urls),
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
