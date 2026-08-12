"""Everything the frontend needs to render the forms and filters.

One endpoint rather than six, for two reasons. It saves the client a handful
of round-trips on load, and -- more importantly -- it keeps the labels
server-owned. When Product changes a resolution type, nobody has to remember
that the wording is also hardcoded in a dropdown somewhere.
"""
from __future__ import annotations

from apps.grievances import enums


def _choices(choice_class) -> list[dict[str, str]]:
    return [{"value": value, "label": label} for value, label in choice_class.choices]


def complaint_metadata() -> dict:
    """The full option set, plus the conditional rules that govern the form.

    ``field_rules`` is included so the client can drive its own show/hide and
    required-field logic from the same source the server validates against,
    instead of reimplementing the rule table in JavaScript and drifting.
    """
    return {
        "complaint_types": _choices(enums.ComplaintType),
        "sources": _choices(enums.ComplaintSource),
        "subject_types": _choices(enums.SubjectType),
        "visibilities": _choices(enums.Visibility),
        "frequencies": _choices(enums.Frequency),
        "witness_types": _choices(enums.WitnessType),
        "states": _choices(enums.ComplaintState),
        "investigation_decisions": _choices(enums.InvestigationDecision),
        "resolution_types": _choices(enums.ResolutionType),
        "formal_resolution_types": _choices(enums.FormalResolutionType),
        "informal_resolution_types": _choices(enums.InformalResolutionType),
        "collaborator_roles": _choices(enums.CollaboratorRole),
        "statuses": [
            {"value": s, "label": s} for s in dict.fromkeys(enums.STATE_TO_STATUS.values())
        ],
        "stages": [
            {"value": s, "label": s} for s in dict.fromkeys(enums.STATE_TO_STAGE.values())
        ],
        "field_rules": {
            "respondent": {
                "required_when": {"subject_type": [enums.SubjectType.EMPLOYEE]},
                "forbidden_when": {"subject_type": [enums.SubjectType.GENERAL]},
            },
            "incident_date": {
                "required_when": {"subject_type": [enums.SubjectType.EMPLOYEE]},
                "max": "today",
            },
            "frequency": {
                "required_when": {"subject_type": [enums.SubjectType.EMPLOYEE]},
            },
            "occurrence_count": {
                "required_when": {"frequency": [enums.Frequency.REPEAT_BEHAVIOR]},
                "min": 1,
            },
            "complaint_type_note": {
                "required_when": {"complaint_type": [enums.ComplaintType.OTHERS]},
            },
            "complainant": {
                "required_when": {"source": [enums.ComplaintSource.HR_FOR_EMPLOYEE]},
                "forbidden_when": {
                    "source": [
                        enums.ComplaintSource.SELF,
                        enums.ComplaintSource.HR_FOR_COMPANY,
                    ]
                },
            },
            "formal_resolution_type": {
                "required_when": {"resolution_type": [enums.ResolutionType.FORMAL]},
            },
            "informal_resolution_type": {
                "required_when": {"resolution_type": [enums.ResolutionType.INFORMAL]},
            },
        },
        "hr_only_sources": [
            enums.ComplaintSource.HR_FOR_EMPLOYEE,
            enums.ComplaintSource.HR_FOR_COMPANY,
        ],
    }
