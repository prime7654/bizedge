"""Verify the database-level constraints from spec v4 actually reject bad data.

Run inside the container:
    docker compose exec -T web python scripts/verify_constraints.py

Creates and rolls nothing back -- run against a scratch database, not one with
real cases in it.
"""
import os
import sys
from pathlib import Path

import django

# Running as `python scripts/...` puts scripts/ on sys.path, not the project
# root. Add the root so `config` and `apps` resolve.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

import uuid

from django.db import IntegrityError, transaction
from apps.core.models import Organisation
from apps.directory.models import Employee
from apps.grievances import enums
from apps.grievances.models import Complaint, Investigation, Resolution

REF = uuid.uuid4().hex[:6].upper() + "-"
org = Organisation.objects.create(name="Acme " + REF, slug="acme-" + REF.lower().rstrip("-"))
alice = Employee.objects.create(organisation=org, full_name="Alice", email=f"a-{REF}@x.com")
bob = Employee.objects.create(organisation=org, full_name="Bob", email=f"b-{REF}@x.com", line_manager=alice)
hr = Employee.objects.create(organisation=org, full_name="HR", email=f"h-{REF}@x.com", is_hr=True)

results = []
def expect_reject(label, fn):
    try:
        with transaction.atomic():
            fn()
        results.append((label, "FAIL - was accepted"))
    except IntegrityError:
        results.append((label, "ok - rejected"))

def expect_accept(label, fn):
    try:
        with transaction.atomic():
            fn()
        results.append((label, "ok - accepted"))
    except Exception as e:
        results.append((label, f"FAIL - {type(e).__name__}: {e}"))

base = dict(organisation=org, source=enums.ComplaintSource.SELF, filed_by=bob,
            complainant=bob, description="x", visibility=enums.Visibility.HR,
            visibility_requested=enums.Visibility.HR)

expect_reject("EMPLOYEE complaint with no respondent",
    lambda: Complaint.objects.create(reference=REF + "C1", subject_type=enums.SubjectType.EMPLOYEE,
        complaint_type=enums.ComplaintType.THEFT, respondent=None, **base))

expect_reject("GENERAL complaint WITH a respondent",
    lambda: Complaint.objects.create(reference=REF + "C2", subject_type=enums.SubjectType.GENERAL,
        complaint_type=enums.ComplaintType.THEFT, respondent=alice, **base))

expect_reject("complainant == respondent",
    lambda: Complaint.objects.create(reference=REF + "C3", subject_type=enums.SubjectType.EMPLOYEE,
        complaint_type=enums.ComplaintType.THEFT, respondent=bob, **base))

expect_reject("OTHERS type with empty note",
    lambda: Complaint.objects.create(reference=REF + "C4", subject_type=enums.SubjectType.GENERAL,
        complaint_type=enums.ComplaintType.OTHERS, complaint_type_note="", **base))

expect_reject("REPEAT_BEHAVIOR with no occurrence_count",
    lambda: Complaint.objects.create(reference=REF + "C5", subject_type=enums.SubjectType.EMPLOYEE,
        complaint_type=enums.ComplaintType.THEFT, respondent=alice,
        frequency=enums.Frequency.REPEAT_BEHAVIOR, occurrence_count=None, **base))

expect_accept("valid EMPLOYEE complaint",
    lambda: Complaint.objects.create(reference=REF + "C6", subject_type=enums.SubjectType.EMPLOYEE,
        complaint_type=enums.ComplaintType.THEFT, respondent=alice,
        frequency=enums.Frequency.REPEAT_BEHAVIOR, occurrence_count=3, **base))

expect_accept("valid OTHERS complaint with note",
    lambda: Complaint.objects.create(reference=REF + "C7", subject_type=enums.SubjectType.GENERAL,
        complaint_type=enums.ComplaintType.OTHERS, complaint_type_note="Rota unfairness", **base))

c = Complaint.objects.get(reference=REF + "C6")
inv = Investigation.objects.create(complaint=c, lead=hr, lead_is_hr=True, start_date="2026-08-12")
rbase = dict(complaint=c, investigation=inv, decision=enums.InvestigationDecision.SUBSTANTIATED,
             decision_notes="n", decided_by=hr)

expect_reject("FORMAL resolution with an informal sub-type",
    lambda: Resolution.objects.create(resolution_type=enums.ResolutionType.FORMAL,
        formal_resolution_type="", informal_resolution_type=enums.InformalResolutionType.COACHING, **rbase))

expect_reject("FORMAL resolution with no sub-type at all",
    lambda: Resolution.objects.create(resolution_type=enums.ResolutionType.FORMAL,
        formal_resolution_type="", informal_resolution_type="", **rbase))

expect_reject("sub-type OTHERS with no resolution_note",
    lambda: Resolution.objects.create(resolution_type=enums.ResolutionType.FORMAL,
        formal_resolution_type=enums.FormalResolutionType.OTHERS,
        informal_resolution_type="", resolution_note="", **rbase))

expect_accept("valid FORMAL resolution",
    lambda: Resolution.objects.create(resolution_type=enums.ResolutionType.FORMAL,
        formal_resolution_type=enums.FormalResolutionType.FIRST_WRITTEN_WARNING,
        informal_resolution_type="", **rbase))

# Soft delete behaviour
c7 = Complaint.objects.get(reference=REF + "C7")
c7.delete(deleted_by=hr)
hidden = not Complaint.objects.filter(reference=REF + "C7").exists()
still_there = Complaint.all_objects.filter(reference=REF + "C7").exists()
results.append(("soft delete hides from default manager", "ok" if hidden else "FAIL"))
results.append(("soft delete keeps row in all_objects", "ok" if still_there else "FAIL"))
results.append(("derived status label for SUBMITTED", "ok" if c.status_label == "Pending" else f"FAIL {c.status_label}"))

width = max(len(l) for l, _ in results)
fails = 0
for label, outcome in results:
    if outcome.startswith("FAIL"):
        fails += 1
    print(f"  {label.ljust(width)}  {outcome}")
print()
print(f"{len(results) - fails}/{len(results)} passed")
sys.exit(1 if fails else 0)
