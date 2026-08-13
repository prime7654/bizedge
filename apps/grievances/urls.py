from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.grievances.investigation_views import (
    InvestigationViewSet,
    MyInformationRequestViewSet,
)
from apps.grievances.pip_views import PIPPlanViewSet
from apps.grievances.views import ComplaintViewSet

router = DefaultRouter()
router.register("complaints", ComplaintViewSet, basename="complaint")
router.register("investigations", InvestigationViewSet, basename="investigation")
router.register("pips", PIPPlanViewSet, basename="pip")
router.register(
    "me/information-requests",
    MyInformationRequestViewSet,
    basename="my-information-request",
)

app_name = "grievances"

urlpatterns = [path("", include(router.urls))]
