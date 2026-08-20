"""JWT auth: obtain a Bearer token, use it, and confirm the profile gate holds.

The point of token auth here is that the SPA authenticates with an Authorization
header and no cookies/CSRF. These assert that flow works AND that a valid login
without an Employee profile still can't reach data -- the profile, not the token,
is what grants access.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from rest_framework.test import APIClient

from apps.grievances.tests.factories import make_employee, make_org

pytestmark = pytest.mark.django_db

TOKEN_URL = "/api/v1/auth/token/"
REFRESH_URL = "/api/v1/auth/token/refresh/"
ME_URL = "/api/v1/auth/me/"
PROTECTED = "/api/v1/app/complaints/types?category=general"

PASSWORD = "s3cret-pw"  # noqa: S105


def make_login(org, name, *, is_hr=False, with_profile=True):
    """A user who can log in -- with or without an Employee profile."""
    employee = make_employee(org, name, is_hr=is_hr) if with_profile else None
    user = User.objects.create_user(
        username=name.lower().replace(" ", "."), password=PASSWORD
    )
    if employee is not None:
        employee.user = user
        employee.save(update_fields=["user"])
    return user.username, employee


def tokens_for(username):
    return APIClient().post(
        TOKEN_URL, {"username": username, "password": PASSWORD}, format="json"
    )


def bearer(access):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    return client


def test_obtain_token_with_valid_credentials():
    username, _ = make_login(make_org(), "Bob")
    resp = tokens_for(username)
    assert resp.status_code == 200, resp.data
    assert "access" in resp.data and "refresh" in resp.data


def test_bad_password_is_rejected():
    username, _ = make_login(make_org(), "Bob")
    resp = APIClient().post(
        TOKEN_URL, {"username": username, "password": "wrong"}, format="json"
    )
    assert resp.status_code == 401


def test_bearer_token_reaches_a_protected_endpoint():
    username, _ = make_login(make_org(), "Bob")
    access = tokens_for(username).data["access"]
    resp = bearer(access).get(PROTECTED)
    assert resp.status_code == 200


def test_me_returns_the_signed_in_employee():
    username, employee = make_login(make_org(), "Priya", is_hr=True)
    access = tokens_for(username).data["access"]
    resp = bearer(access).get(ME_URL)
    assert resp.status_code == 200
    assert str(resp.data["id"]) == str(employee.pk)
    assert resp.data["is_hr"] is True


def test_refresh_issues_a_new_access_token():
    username, _ = make_login(make_org(), "Bob")
    refresh = tokens_for(username).data["refresh"]
    resp = APIClient().post(REFRESH_URL, {"refresh": refresh}, format="json")
    assert resp.status_code == 200
    assert "access" in resp.data


def test_token_without_employee_profile_is_gated_at_the_api():
    """A valid Django login with no Employee profile: token yes, data no."""
    username, _ = make_login(make_org(), "Ghost", with_profile=False)
    access = tokens_for(username).data["access"]
    resp = bearer(access).get(PROTECTED)
    assert resp.status_code == 403


def test_no_token_is_refused():
    assert APIClient().get(ME_URL).status_code in (401, 403)


def test_token_endpoint_trailing_slash_optional():
    """`/auth/token` and `/auth/token/` both work (no APPEND_SLASH 404 on POST)."""
    username, _ = make_login(make_org(), "Bob")
    body = {"username": username, "password": PASSWORD}
    assert APIClient().post("/api/v1/auth/token", body, format="json").status_code == 200
    assert APIClient().post("/api/v1/auth/token/", body, format="json").status_code == 200
