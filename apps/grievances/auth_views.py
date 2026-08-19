"""Authentication endpoints for the SPA frontends.

The API is called cross-origin from separate sites (MAKAY on Vercel, the HR
console), so it authenticates with short-lived JWT access tokens in the
Authorization header rather than session cookies -- no cross-site-cookie or CSRF
dance, and nothing a browser's third-party-cookie policy can block.

    POST /api/v1/auth/token/          -> {access, refresh} from username+password
    POST /api/v1/auth/token/refresh/  -> a fresh access token from a refresh token
    GET  /api/v1/auth/me/             -> the signed-in employee (id, name, role)

Obtaining a token only proves the Django login is valid; every data endpoint
still requires a linked Employee profile, exactly as before. A valid login with
no profile gets a token and then 403s on the API -- the profile is the gate.
"""
from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.directory.models import Employee
from apps.grievances.access import employee_for
from apps.grievances.permissions import IsEmployee


class MeSerializer(serializers.ModelSerializer):
    """The signed-in employee, for the frontend to render who/role."""

    department = serializers.CharField(
        source="department.name", read_only=True, allow_null=True
    )

    class Meta:
        model = Employee
        fields = ("id", "full_name", "email", "job_title", "is_hr", "department")
        read_only_fields = fields


class MeView(APIView):
    """Who am I? Resolves the Employee profile behind the JWT."""

    permission_classes = [IsEmployee]

    @extend_schema(responses=MeSerializer)
    def get(self, request):
        employee = employee_for(request.user)
        return Response(MeSerializer(employee).data)
