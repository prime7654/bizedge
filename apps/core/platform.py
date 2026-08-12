"""Indirection layer for models owned by the wider platform.

The grievances module is standalone today and will be merged into BizEdge /
MAKAY later. Nothing in apps.grievances should import a platform model
directly -- go through the string settings here so the merge is a config
change rather than a rewrite.
"""
from django.conf import settings


def employee_model() -> str:
    """Return the swappable Employee model as an 'app_label.Model' string."""
    return settings.GRIEVANCES_EMPLOYEE_MODEL


def department_model() -> str:
    return settings.GRIEVANCES_DEPARTMENT_MODEL


def training_model() -> str:
    return settings.GRIEVANCES_TRAINING_MODEL


def organisation_model() -> str:
    return settings.GRIEVANCES_ORGANISATION_MODEL
