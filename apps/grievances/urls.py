from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.grievances.views import ComplaintViewSet

router = DefaultRouter()
router.register("complaints", ComplaintViewSet, basename="complaint")

app_name = "grievances"

urlpatterns = [path("", include(router.urls))]
