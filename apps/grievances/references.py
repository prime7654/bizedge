"""Case reference generation.

References are per-tenant and per-year: ``CMP-2026-00001``. Each organisation
starts at 1 each year, which is what people expect when they quote a case
number.

Concurrency matters here. Two people filing at the same instant must not get
the same reference, so allocation takes a row lock rather than reading
``MAX(...) + 1``.
"""
from __future__ import annotations
from django.db import models, transaction
from django.utils import timezone
from apps.core.models import TimeStampedModel

REFERENCE_PREFIX = "CMP"


class ReferenceCounter(TimeStampedModel):
    """One row per organisation per year, holding the last number issued."""

    organisation = models.ForeignKey(
        "core.Organisation", on_delete=models.CASCADE, related_name="reference_counters"
    )
    year = models.PositiveIntegerField()
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("organisation", "year"), name="uniq_reference_counter_per_year"
            )
        ]

    def __str__(self) -> str:
        return f"{self.organisation_id} {self.year}: {self.last_number}"


@transaction.atomic
def next_reference(organisation, *, year: int | None = None) -> str:
    """Allocate the next reference for ``organisation``.

    Takes a row lock for the duration of the surrounding transaction, so
    concurrent callers queue rather than collide. Must be called inside an
    atomic block -- it already opens one, so nesting is fine.
    """
    year = year or timezone.now().year

    counter, _ = ReferenceCounter.objects.get_or_create(
        organisation=organisation, year=year
    )
    # Re-read under lock: get_or_create does not lock, so another transaction
    # could have incremented between the fetch and the update.
    counter = ReferenceCounter.objects.select_for_update().get(pk=counter.pk)
    counter.last_number += 1
    counter.save(update_fields=["last_number", "updated_at"])

    return f"{REFERENCE_PREFIX}-{year}-{counter.last_number:05d}"
