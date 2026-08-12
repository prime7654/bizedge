"""Admin registrations.

Read-mostly on purpose. The admin is for support and debugging; grievance data
should be changed through the API where the state machine, access policy and
audit trail all apply. Editing a case here bypasses all three.
"""
from django.contrib import admin

from apps.grievances.models import (
    Complaint,
    ComplaintEvent,
    ComplaintWitness,
    InformationRequest,
    InformationResponse,
    Investigation,
    InvestigationCollaborator,
    InvestigationMeeting,
    InvestigationNote,
    PIPFollowUp,
    PIPPlan,
    PIPTrainingAssignment,
    Resolution,
)


class ComplaintWitnessInline(admin.TabularInline):
    model = ComplaintWitness
    extra = 0
    autocomplete_fields = ("employee", "department")


class ComplaintEventInline(admin.TabularInline):
    """Audit trail is strictly read-only. Never editable, never deletable."""

    model = ComplaintEvent
    extra = 0
    can_delete = False
    readonly_fields = (
        "actor", "verb", "from_state", "to_state", "payload",
        "occurred_at", "ip_address", "user_agent",
    )
    ordering = ("-occurred_at",)

    def has_add_permission(self, request, obj=None) -> bool:
        return False


@admin.register(Complaint)
class ComplaintAdmin(admin.ModelAdmin):
    list_display = (
        "reference", "complaint_type", "source", "subject_type",
        "visibility", "state", "due_date", "created_at",
    )
    list_filter = (
        "organisation", "state", "visibility", "complaint_type",
        "source", "subject_type",
    )
    search_fields = ("reference", "description", "complaint_type_note")
    date_hierarchy = "created_at"
    inlines = (ComplaintWitnessInline, ComplaintEventInline)
    readonly_fields = (
        "id", "created_at", "updated_at", "deleted_at", "deleted_by",
        "visibility_requested", "complainant_identity_released_at",
        "withdrawal_requested_at",
    )
    autocomplete_fields = ("filed_by", "complainant", "respondent")

    def get_queryset(self, request):
        """Show soft-deleted cases here so support can find them."""
        return self.model.all_objects.get_queryset()


class InvestigationCollaboratorInline(admin.TabularInline):
    model = InvestigationCollaborator
    extra = 0
    autocomplete_fields = ("employee",)


@admin.register(Investigation)
class InvestigationAdmin(admin.ModelAdmin):
    list_display = ("complaint", "round", "lead", "lead_is_hr", "state", "start_date")
    list_filter = ("state", "lead_is_hr")
    search_fields = ("complaint__reference",)
    inlines = (InvestigationCollaboratorInline,)
    autocomplete_fields = ("lead", "invited_by")


@admin.register(Resolution)
class ResolutionAdmin(admin.ModelAdmin):
    list_display = (
        "complaint", "decision", "resolution_type",
        "formal_resolution_type", "informal_resolution_type", "decided_at",
    )
    list_filter = ("decision", "resolution_type", "formal_resolution_type")
    search_fields = ("complaint__reference",)


@admin.register(PIPPlan)
class PIPPlanAdmin(admin.ModelAdmin):
    list_display = ("employee", "start_date", "end_date", "state")
    list_filter = ("state",)


@admin.register(ComplaintEvent)
class ComplaintEventAdmin(admin.ModelAdmin):
    """Fully read-only. This table is evidence."""

    list_display = ("complaint", "verb", "actor", "from_state", "to_state", "occurred_at")
    list_filter = ("verb",)
    search_fields = ("complaint__reference",)
    date_hierarchy = "occurred_at"

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


admin.site.register(
    [
        ComplaintWitness,
        InformationRequest,
        InformationResponse,
        InvestigationMeeting,
        InvestigationNote,
        PIPTrainingAssignment,
        PIPFollowUp,
    ]
)
