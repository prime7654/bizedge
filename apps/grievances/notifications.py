"""Notification dispatch.

A seam, not an implementation. The platform's notification system has no
confirmed owner yet (spec v4 section 10.3), so this logs and records intent.
When the real service is available, replace :func:`_deliver` -- nothing else
should need to change.

Deliberately one dispatcher rather than notification calls scattered through
the services: the set of events that notify someone is a product decision, and
it should be readable in one place.
"""
from __future__ import annotations

import logging
from typing import Iterable

logger = logging.getLogger(__name__)


def notify(event: str, complaint, recipients: Iterable, **context) -> None:
    """Emit one notification per recipient.

    Never raises. A failed notification must not roll back the transaction that
    triggered it -- losing a complaint because an email bounced would be worse
    than a missing notification.
    """
    for recipient in recipients:
        if recipient is None:
            continue
        try:
            _deliver(event, complaint, recipient, context)
        except Exception:  # noqa: BLE001 - deliberately swallowed, see docstring
            logger.exception(
                "Notification failed", extra={"event": event, "complaint": complaint.pk}
            )


def _deliver(event: str, complaint, recipient, context: dict) -> None:
    """Placeholder delivery. Swap for the platform notification service."""
    logger.info(
        "notify: %s -> %s (complaint %s) %s",
        event,
        getattr(recipient, "email", recipient),
        complaint.reference,
        context or "",
    )


# Event names. Kept as constants so a typo is an ImportError rather than a
# notification that silently never fires.
COMPLAINT_FILED = "complaint.filed"
INVESTIGATOR_APPOINTED = "complaint.investigator_appointed"
COLLABORATOR_INVITED = "investigation.collaborator_invited"
INFORMATION_REQUESTED = "investigation.information_requested"
INFORMATION_RECEIVED = "investigation.information_received"
REPORT_SUBMITTED = "investigation.report_submitted"
COMPLAINT_RESOLVED = "complaint.resolved"
COMPLAINT_REOPENED = "complaint.reopened"
COMPLAINT_WITHDRAWN = "complaint.withdrawn"
PIP_FOLLOW_UP_DUE = "pip.follow_up_due"
