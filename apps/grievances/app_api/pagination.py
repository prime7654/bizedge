"""Pagination in the shape the employee app expects: {data, total, page, pageSize}."""
from __future__ import annotations

from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response


class AppPagination(PageNumberPagination):
    """PageNumberPagination re-enveloped for the MAKAY app.

    The design's list shows "1-5 of 5" and a page size the client can set, so
    unlike the HR console (fixed page size) this one honours ``pageSize``.
    """

    page_size = 25
    page_size_query_param = "pageSize"
    max_page_size = 100

    def get_paginated_response(self, data):
        return Response(
            {
                "data": data,
                "total": self.page.paginator.count,
                "page": self.page.number,
                "pageSize": self.get_page_size(self.request),
            }
        )
