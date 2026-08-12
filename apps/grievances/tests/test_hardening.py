"""Tests for hostile and malformed input.

These cover the failure modes that are trivially triggerable from outside and
were not caught by the happy-path suite. Each one existed as a real defect
before its test did.
"""
from __future__ import annotations

import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.grievances import enums
from apps.grievances.events import _client_ip, record_event
from apps.grievances.models import ComplaintEvent
from apps.grievances.tests.factories import make_complaint, make_employee, make_org
from apps.grievances.tests.test_intake import as_user

pytestmark = pytest.mark.django_db

URL = "/api/v1/complaints/"


class FakeRequest:
    def __init__(self, **meta):
        self.META = meta


# ---------------------------------------------------------------------------
# Client IP -- attacker-controlled, and headed for a Postgres inet column
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "header",
    [
        "not-an-ip",
        "'; DROP TABLE complaints--",
        "x" * 600,
        "999.999.999.999",
        "<script>alert(1)</script>",
        "  ",
    ],
)
def test_malformed_forwarded_for_never_reaches_the_database(header):
    """Regression: this used to 500 every audited endpoint on Postgres.

    ip_address is a GenericIPAddressField, which is `inet` in Postgres and
    rejects anything unparseable. SQLite accepts it, so the original test
    suite could not have caught this.
    """
    ip = _client_ip(FakeRequest(HTTP_X_FORWARDED_FOR=header, REMOTE_ADDR="10.0.0.1"))
    assert ip == "10.0.0.1", "should fall back to REMOTE_ADDR, not pass junk through"


def test_junk_in_both_ip_sources_yields_none():
    assert _client_ip(FakeRequest(HTTP_X_FORWARDED_FOR="junk", REMOTE_ADDR="also junk")) is None


@pytest.mark.parametrize(
    "header,expected",
    [
        ("203.0.113.5", "203.0.113.5"),
        ("203.0.113.5, 70.41.3.18", "203.0.113.5"),  # left-most is the client
        ("2001:db8::1", "2001:db8::1"),
    ],
)
def test_valid_addresses_are_kept(header, expected):
    assert _client_ip(FakeRequest(HTTP_X_FORWARDED_FOR=header)) == expected


def test_audit_row_survives_a_hostile_header():
    org = make_org()
    employee = make_employee(org, "Employee")
    complaint = make_complaint(org, complainant=employee)

    event = record_event(
        complaint,
        verb=enums.EventVerb.VIEWED,
        actor=employee,
        request=FakeRequest(HTTP_X_FORWARDED_FOR="nonsense", REMOTE_ADDR="bad too"),
    )
    assert event.ip_address is None
    assert ComplaintEvent.objects.filter(pk=event.pk).exists()


def test_absurdly_long_user_agent_is_truncated():
    org = make_org()
    employee = make_employee(org, "Employee")
    complaint = make_complaint(org, complainant=employee)

    event = record_event(
        complaint,
        verb=enums.EventVerb.VIEWED,
        actor=employee,
        request=FakeRequest(HTTP_USER_AGENT="A" * 10_000, REMOTE_ADDR="10.0.0.1"),
    )
    assert len(event.user_agent) <= 512


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

@pytest.fixture
def uploader():
    org = make_org()
    hr = make_employee(org, "HR User", is_hr=True)
    complaint = make_complaint(org, complainant=hr, visibility=enums.Visibility.HR)
    return as_user(hr), complaint


def upload_url(complaint) -> str:
    return f"{URL}{complaint.pk}/attachments/"


def test_extension_and_mime_type_must_agree(uploader):
    """content_type is a client header. A renamed binary will claim to be a PDF."""
    client, complaint = uploader
    disguised = SimpleUploadedFile("payload.exe", b"MZ\x90\x00", content_type="application/pdf")
    response = client.post(upload_url(complaint), {"file": disguised}, format="multipart")
    assert response.status_code == 400
    assert "file" in response.data


def test_disallowed_mime_type_is_rejected(uploader):
    client, complaint = uploader
    bad = SimpleUploadedFile("script.pdf", b"#!/bin/sh", content_type="application/x-sh")
    response = client.post(upload_url(complaint), {"file": bad}, format="multipart")
    assert response.status_code == 400


def test_path_traversal_in_the_filename_is_stripped(uploader):
    """`../../etc/passwd.pdf` must not survive into the stored filename."""
    client, complaint = uploader
    sneaky = SimpleUploadedFile(
        "../../../etc/passwd.pdf", b"%PDF-1.4 ok", content_type="application/pdf"
    )
    response = client.post(upload_url(complaint), {"file": sneaky}, format="multipart")
    assert response.status_code == 201, response.data
    assert "/" not in response.data["original_filename"]
    assert ".." not in response.data["original_filename"]


def test_empty_file_is_rejected(uploader):
    client, complaint = uploader
    empty = SimpleUploadedFile("empty.pdf", b"", content_type="application/pdf")
    response = client.post(upload_url(complaint), {"file": empty}, format="multipart")
    assert response.status_code == 400


def test_oversized_file_is_rejected(uploader, settings):
    client, complaint = uploader
    settings.GRIEVANCES_MAX_ATTACHMENT_BYTES = 100
    big = SimpleUploadedFile("big.pdf", b"x" * 5_000, content_type="application/pdf")
    response = client.post(upload_url(complaint), {"file": big}, format="multipart")
    assert response.status_code == 400


def test_missing_file_field_is_a_400_not_a_500(uploader):
    client, complaint = uploader
    response = client.post(upload_url(complaint), {}, format="multipart")
    assert response.status_code == 400


def test_valid_attachment_is_accepted(uploader):
    client, complaint = uploader
    good = SimpleUploadedFile("evidence.pdf", b"%PDF-1.4 fine", content_type="application/pdf")
    response = client.post(upload_url(complaint), {"file": good}, format="multipart")
    assert response.status_code == 201, response.data
    assert response.data["original_filename"] == "evidence.pdf"


# ---------------------------------------------------------------------------
# Search input
# ---------------------------------------------------------------------------

def test_enormous_search_term_does_not_reach_the_database_unbounded():
    org = make_org()
    hr = make_employee(org, "HR User", is_hr=True)
    make_complaint(org, complainant=hr, visibility=enums.Visibility.HR)

    response = as_user(hr).get(URL, {"q": "x" * 100_000})
    assert response.status_code == 200
    assert response.data["count"] == 0


def test_search_metacharacters_are_treated_as_text():
    org = make_org()
    hr = make_employee(org, "HR User", is_hr=True)
    make_complaint(org, complainant=hr, visibility=enums.Visibility.HR)

    for term in ["%", "_", "'; DROP TABLE complaints--", "\\"]:
        response = as_user(hr).get(URL, {"q": term})
        assert response.status_code == 200, term
