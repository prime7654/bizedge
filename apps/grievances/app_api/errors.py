"""Error format for the employee app: HTTP 422 with a {"errors": {...}} envelope.

The design has a dedicated inline error state ("This is a compulsory field"), and
the frontend attaches messages to inputs by field name. DRF's default is a flat
400 body; this handler -- installed only on the app_api views, via
``get_exception_handler`` -- rewraps validation failures without touching the
rest of the API, which keeps its 400s.
"""
from __future__ import annotations

from rest_framework import status
from rest_framework.views import exception_handler as drf_exception_handler


def app_exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is None:
        # An unhandled (non-DRF) exception. Let it 500 as normal -- swallowing
        # it here would hide a real bug behind a tidy 422.
        return response

    if response.status_code == status.HTTP_400_BAD_REQUEST:
        # DRF has already normalised the ValidationError into response.data,
        # keyed by field name (or "non_field_errors"). Wrap and re-status so the
        # frontend gets {"errors": {field: [msg]}} with a 422.
        response.data = {"errors": response.data}
        response.status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    return response
