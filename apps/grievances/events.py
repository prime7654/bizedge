"""Audit trail writes.

Every meaningful action on a complaint goes through here. A grievance record
is evidence in an employment dispute; "who saw this, and when" is a question
that gets asked, and the answer has to come from somewhere.

Rows are append-only. Nothing in the codebase should ever update or delete a
:class:`ComplaintEvent`.
"""
from __future__ import annotations

from typing import Any

from apps.grievances.models import Complaint, ComplaintEvent


def record_event(
    complaint: Complaint,
    *,
    verb: str,
    actor=None,
    from_state: str = "",
    to_state: str = "",
    payload: dict[str, Any] | None = None,
    request=None,
) -> ComplaintEvent:
    """Append one audit row.

    ``request`` is optional and only used to capture IP and user agent. Pass it
    where you have it -- for anything a person did deliberately, that context
    is worth having later.
    """
    ip = None
    user_agent = ""
    if request is not None:
        ip = _client_ip(request)
        user_agent = request.META.get("HTTP_USER_AGENT", "")[:512]

    return ComplaintEvent.objects.create(
        complaint=complaint,
        actor=actor,
        verb=verb,
        from_state=from_state or "",
        to_state=to_state or "",
        payload=payload or {},
        ip_address=ip,
        user_agent=user_agent,
    )


def _client_ip(request) -> str | None:
    """Best-effort client IP.

    X-Forwarded-For is trusted only as far as the deployment's proxy config
    allows; treat the result as indicative, not authoritative.
    """
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None
