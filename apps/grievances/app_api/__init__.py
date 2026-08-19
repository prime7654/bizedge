"""MAKAY employee-app API surface.

A compatibility layer over the grievances domain. The employee app
(`MAKAY_Grievance_Employee_Section_API_Spec`) speaks a different wire dialect
from the HR console: a single ``category`` instead of ``source`` + ``subject_type``,
lowercase enum tokens, camelCase response keys, human-readable complaint-type
labels, a ``{data, total, page, pageSize}`` envelope, and HTTP 422 validation
errors.

Nothing in this package changes the domain. Every view reuses the existing
services (:mod:`apps.grievances.services`) and the single access policy
(:class:`apps.grievances.access.ComplaintAccessPolicy`); this layer only
translates at the boundary. The existing ``/api/v1/`` endpoints are untouched,
so the HR console keeps its contract.

Mounted at ``/api/v1/app/``. All translation between the two vocabularies lives
in :mod:`apps.grievances.app_api.mappings`.
"""
