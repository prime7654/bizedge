"""Managers enforcing soft deletion.

Spec v4 section 7: nothing in this module is hard deleted. The default
manager must exclude soft-deleted rows so a forgotten filter cannot leak a
deleted grievance back into a list.
"""
from django.db import models
from django.utils import timezone


class SoftDeleteQuerySet(models.QuerySet):
    """QuerySet that soft-deletes instead of issuing a DELETE."""

    def delete(self):  # type: ignore[override]
        """Soft delete every row in the queryset.

        Deliberately shadows QuerySet.delete() so that a bulk delete cannot
        silently destroy grievance records.
        """
        return self.update(deleted_at=timezone.now())

    def hard_delete(self):
        """Genuinely remove rows. Reserved for tests and data migrations."""
        return super().delete()

    def alive(self):
        return self.filter(deleted_at__isnull=True)

    def dead(self):
        return self.filter(deleted_at__isnull=False)


class SoftDeleteManager(models.Manager):
    """Default manager: soft-deleted rows are invisible."""

    def get_queryset(self) -> SoftDeleteQuerySet:
        return SoftDeleteQuerySet(self.model, using=self._db).filter(
            deleted_at__isnull=True
        )


class AllObjectsManager(models.Manager):
    """Escape hatch that sees deleted rows. Use explicitly and sparingly."""

    def get_queryset(self) -> SoftDeleteQuerySet:
        return SoftDeleteQuerySet(self.model, using=self._db)
