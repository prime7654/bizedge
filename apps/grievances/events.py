"""Audit trail writes.

Every meaningful action on a complaint goes through here. A grievance record
is evidence in an employment dispute; "who saw this, and when" is a question
that gets asked, and the answer has to come from somewhere.

Rows are append-only. Nothing in the codebase should ever update or delete a
:class:`ComplaintEvent`.
"""
from __future__ import annotations

import ipaddress
import logging
from typing import Any

from apps.grievances.models import Complaint, ComplaintEvent

logger = logging.getLogger(__name__)


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
    """Best-effort client IP, validated before it goes anywhere near the DB.

    X-Forwarded-For is attacker-controlled: it is whatever the client chose to
    send. ``ComplaintEvent.ip_address`` is a ``GenericIPAddressField``, which
    maps to the Postgres ``inet`` type and *rejects* anything that is not an
    address. Storing the header unvalidated therefore turns a junk header into
    a 500 on every endpoint that writes an audit row -- which is all of the
    interesting ones.

    So: parse it, and fall back to None rather than raising. An audit row with
    no IP is a small loss; a filing endpoint that anyone can break with a
    header is not.

    The value remains indicative rather than authoritative -- it is only as
    trustworthy as the deployment's proxy configuration.
    """
    candidates = []

    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        # Left-most entry is the original client, per convention.
        candidates.append(forwarded.split(",")[0])
    candidates.append(request.META.get("REMOTE_ADDR", ""))

    for candidate in candidates:
        candidate = (candidate or "").strip()
        if not candidate:
            continue
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            logger.debug("Discarding unparseable client IP: %r", candidate[:64])
            continue

    return None
