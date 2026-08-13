"""Send reminders for PIP check-ins that have come due.

    python manage.py send_due_pip_reminders [--date YYYY-MM-DD] [--dry-run]

The only part of this module not driven by a user action, so it needs
something to invoke it -- Celery Beat, cron, or a Kubernetes CronJob. The logic
lives here rather than in a Celery task so it can be tested and run by hand
without any of that machinery, and so the scheduler stays swappable.

**Safe to run repeatedly.** Each reminder stamps ``reminder_sent_at``, which
drops the row out of the due queryset, so running twice in a day sends once.
Missing a day is also safe: due check-ins are selected as scheduled *on or
before* today, so yesterday's reminders go out late rather than never.
"""
from __future__ import annotations

import logging

from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_date

from apps.grievances.services import due_follow_ups, send_follow_up_reminder

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Send reminders for PIP follow-ups that are due."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--date",
            help="Treat this ISO date as today. For backfills and testing.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be sent without sending or stamping.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=500,
            help=(
                "Cap the batch. A backlog after an outage should not turn into "
                "one enormous run; the next invocation picks up the rest."
            ),
        )

    def handle(self, *args, **options) -> None:
        on_date = None
        raw_date = options["date"]
        if raw_date is not None:
            # parse_date returns None for a malformed string but *raises*
            # ValueError for one that is well formed yet impossible, such as
            # 2026-13-45. Both are user error and both should read the same.
            try:
                on_date = parse_date(raw_date.strip())
            except ValueError:
                on_date = None
            if on_date is None:
                raise CommandError(
                    f"{raw_date!r} is not a valid date (expected YYYY-MM-DD)."
                )

        limit = options["limit"]
        if limit < 1:
            raise CommandError(f"--limit must be at least 1, got {limit}.")

        due = list(due_follow_ups(on_date=on_date)[:limit])
        if not due:
            self.stdout.write("No PIP reminders due.")
            return

        sent = 0
        skipped = 0
        for follow_up in due:
            if options["dry_run"]:
                self.stdout.write(
                    f"would remind: {follow_up.pip_plan.employee.full_name} "
                    f"({follow_up.scheduled_date}, {follow_up.kind})"
                )
                continue

            try:
                if send_follow_up_reminder(follow_up):
                    sent += 1
                else:
                    # Another worker got there first, or it was completed
                    # between selecting the batch and processing this row.
                    skipped += 1
            except Exception:  # noqa: BLE001
                # One bad row must not abandon the rest of the batch. The
                # reminder is not stamped, so it is retried next run.
                skipped += 1
                logger.exception(
                    "PIP reminder failed", extra={"follow_up_id": str(follow_up.pk)}
                )

        if options["dry_run"]:
            self.stdout.write(f"{len(due)} reminder(s) would be sent.")
        else:
            self.stdout.write(f"Sent {sent} reminder(s), skipped {skipped}.")
