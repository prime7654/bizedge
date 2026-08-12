"""Shared abstract models: tenancy, timestamps, soft deletion."""
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.managers import AllObjectsManager, SoftDeleteManager


class Organisation(models.Model):
    """Tenant boundary.

    Every domain row carries an organisation. Cross-tenant leakage in a
    grievances module is a serious incident, so this is never optional.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name


class TimeStampedModel(models.Model):
    """Adds created/updated timestamps."""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class TenantOwnedModel(models.Model):
    """Adds the organisation FK that scopes every query."""

    organisation = models.ForeignKey(
        settings.GRIEVANCES_ORGANISATION_MODEL,
        on_delete=models.CASCADE,
        related_name="%(app_label)s_%(class)s_set",
    )

    class Meta:
        abstract = True


class SoftDeleteModel(models.Model):
    """Adds soft deletion.

    ``objects`` hides deleted rows; ``all_objects`` sees them. Calling
    ``delete()`` on an instance marks it deleted rather than removing it.
    """

    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    deleted_by = models.ForeignKey(
        settings.GRIEVANCES_EMPLOYEE_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()

    class Meta:
        abstract = True

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def delete(self, using=None, keep_parents=False, deleted_by=None):  # type: ignore[override]
        """Soft delete. Pass ``deleted_by`` to record who did it."""
        self.deleted_at = timezone.now()
        if deleted_by is not None:
            self.deleted_by = deleted_by
        self.save(update_fields=["deleted_at", "deleted_by"])

    def hard_delete(self, using=None, keep_parents=False):
        """Genuinely remove the row. Tests and data migrations only."""
        return super().delete(using=using, keep_parents=keep_parents)

    def restore(self) -> None:
        self.deleted_at = None
        self.deleted_by = None
        self.save(update_fields=["deleted_at", "deleted_by"])
