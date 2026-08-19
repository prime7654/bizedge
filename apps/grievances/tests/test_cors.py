"""CORS is configured so a browser on the frontend origin can reach the API.

These assert the response actually carries the Access-Control-* headers -- the
part the browser enforces -- for a listed origin, and withholds them from an
unlisted one. django-cors-headers adds the headers regardless of status code,
which is what lets the browser read a 403 auth error rather than an opaque CORS
failure.
"""
from __future__ import annotations

import pytest
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db

ALLOWED = "http://localhost:5173"
DENIED = "https://evil.example.com"
PATH = "/api/v1/app/complaints/types?category=general"


def test_preflight_allows_a_listed_origin():
    resp = APIClient().options(
        PATH, HTTP_ORIGIN=ALLOWED, HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET"
    )
    assert resp.status_code == 200
    assert resp["Access-Control-Allow-Origin"] == ALLOWED
    # Session auth needs credentialed requests, so this header must be present.
    assert resp["Access-Control-Allow-Credentials"] == "true"


def test_actual_request_carries_the_cors_header_for_a_listed_origin():
    resp = APIClient().get(PATH, HTTP_ORIGIN=ALLOWED)
    assert resp["Access-Control-Allow-Origin"] == ALLOWED


def test_unlisted_origin_gets_no_cors_header():
    resp = APIClient().get(PATH, HTTP_ORIGIN=DENIED)
    assert not resp.has_header("Access-Control-Allow-Origin")
