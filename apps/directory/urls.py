from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.directory.views import (
    DepartmentViewSet,
    EmployeeLookupViewSet,
    TrainingViewSet,
)

router = DefaultRouter()
router.register("employees", EmployeeLookupViewSet, basename="employee")
router.register("departments", DepartmentViewSet, basename="department")
router.register("trainings", TrainingViewSet, basename="training")

app_name = "directory"

urlpatterns = [path("", include(router.urls))]
