"""drf-spectacular hook for the MAKAY app surface.

The /api/v1/app/ endpoints are plain APIViews with their own frontend contract
(the MAKAY spec), not serializer-backed DRF views, so including them in the
generated OpenAPI schema adds warnings and low-value noise. This preprocessing
hook drops them, keeping `make docs` / `make docs-check` clean and the schema
focused on the HR-console API the generated client is built from.
"""
from __future__ import annotations

APP_API_PREFIX = "/api/v1/app/"


def exclude_app_api_endpoints(endpoints, **kwargs):
    """Drop every /api/v1/app/ path from the generated schema.

    ``endpoints`` is a list of (path, path_regex, method, callback) tuples.
    """
    return [
        endpoint for endpoint in endpoints
        if not endpoint[0].startswith(APP_API_PREFIX)
    ]
