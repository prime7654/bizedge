"""Test helpers.

Plain functions rather than factory_boy: the access tests need very explicit
control over org-chart shape, and readable setup matters more here than
brevity.
"""
from __future__ import annotations

import uuid

from apps.core.models import Organisation
from apps.directory.models import Employee
from apps.grievances import enums
from apps.grievances.models import Complaint, Investigation


def make_org(name: str = "Acme") -> Organisation:
    token = uuid.uuid4().hex[:8]
    return Organisation.objects.create(name=f"{name} {token}", slug=f"{name.lower()}-{token}")


def make_employee(org, name: str, *, manager=None, is_hr: bool = False) -> Employee:
    return Employee.objects.create(
        organisation=org,
        full_name=name,
        email=f"{name.lower().replace(' ', '.')}.{uuid.uuid4().hex[:6]}@example.com",
        line_manager=manager,
        is_hr=is_hr,
    )


def make_complaint(
    org,
    *,
    complainant,
    filed_by=None,
    respondent=None,
    visibility=enums.Visibility.HR,
    state=enums.ComplaintState.SUBMITTED,
    subject_type=None,
    source=enums.ComplaintSource.SELF,
) -> Complaint:
    if subject_type is None:
        subject_type = (
            enums.SubjectType.EMPLOYEE if respondent else enums.SubjectType.GENERAL
        )
    return Complaint.objects.create(
        organisation=org,
        reference=f"CMP-{uuid.uuid4().hex[:10].upper()}",
        source=source,
        subject_type=subject_type,
        filed_by=filed_by or complainant,
        complainant=complainant,
        respondent=respondent,
        complaint_type=enums.ComplaintType.THEFT,
        description="Something happened.",
        visibility=visibility,
        visibility_requested=visibility,
        state=state,
    )


def make_investigation(complaint, lead, *, round_no: int = 1) -> Investigation:
    return Investigation.objects.create(
        complaint=complaint,
        round=round_no,
        lead=lead,
        lead_is_hr=lead.is_hr,
        start_date="2026-08-12",
    )
