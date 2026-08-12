from django.contrib import admin

from apps.directory.models import Department, Employee, Training


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("name", "organisation")
    list_filter = ("organisation",)
    search_fields = ("name",)


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ("full_name", "email", "job_title", "department", "line_manager", "is_hr")
    list_filter = ("organisation", "is_hr", "is_active", "department")
    search_fields = ("full_name", "email", "job_title")
    autocomplete_fields = ("line_manager", "department")
    raw_id_fields = ("user",)


@admin.register(Training)
class TrainingAdmin(admin.ModelAdmin):
    list_display = ("name", "organisation", "is_active")
    list_filter = ("organisation", "is_active")
    search_fields = ("name",)
