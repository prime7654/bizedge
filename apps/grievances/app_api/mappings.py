"""Translation between the domain's canonical values and the employee app's
wire vocabulary.

One direction-explicit place for every mapping, so the serializers stay thin
and the domain model is never bent to fit the UI. If an enum grows a value,
this is the file that has to learn about it.
"""
from __future__ import annotations

from apps.grievances import enums

# --- category <-> subject_type ----------------------------------------------
# The employee app only ever files as SELF, so `source` is not part of its
# vocabulary at all. Its `category` is purely the subject_type.
CATEGORY_TO_SUBJECT_TYPE = {
    "general": enums.SubjectType.GENERAL,
    "employee": enums.SubjectType.EMPLOYEE,
}
SUBJECT_TYPE_TO_CATEGORY = {v: k for k, v in CATEGORY_TO_SUBJECT_TYPE.items()}

# --- visibility --------------------------------------------------------------
VISIBILITY_FROM_APP = {
    "hr": enums.Visibility.HR,
    "line_manager": enums.Visibility.LINE_MANAGER,
    "both": enums.Visibility.BOTH,
}
VISIBILITY_TO_APP = {v: k for k, v in VISIBILITY_FROM_APP.items()}

# --- frequency ---------------------------------------------------------------
FREQUENCY_FROM_APP = {
    "one_time": enums.Frequency.ONE_TIME,
    "repeat_behavior": enums.Frequency.REPEAT_BEHAVIOR,
}
FREQUENCY_TO_APP = {v: k for k, v in FREQUENCY_FROM_APP.items()}


def status_for(state: str) -> str:
    """Lowercase status token the design's Status column/filter uses."""
    return enums.STATE_TO_STATUS[state].lower()


def stage_for(state: str):
    """Lowercase stage token, or None where the domain shows 'N/A'."""
    stage = enums.STATE_TO_STAGE[state]
    return None if stage == "N/A" else stage.lower()


# Reverse maps for the list filters, which arrive as the lowercase tokens the
# Status/Stage dropdowns use. Built from the canonical display maps so they
# cannot drift from status_for()/stage_for().
STATUS_TOKEN_TO_LABEL = {
    label.lower(): label for label in dict.fromkeys(enums.STATE_TO_STATUS.values())
}
STAGE_TOKEN_TO_LABEL = {
    ("null" if label == "N/A" else label.lower()): label
    for label in dict.fromkeys(enums.STATE_TO_STAGE.values())
}


def complaint_type_options() -> list[dict]:
    """The complaint-type dropdown, as {value, label} pairs.

    One list, six values, identical for every category -- the Product-confirmed
    types (decision Q2). The Figma design shows a longer, category-split list
    that predates that decision; reconciling the two is an open Product question
    (see the note delivered to Alfred), not something this layer invents.
    """
    return [{"value": v, "label": label} for v, label in enums.ComplaintType.choices]


#: Accept either the machine value ("SEXUAL_HARASSMENT") or the human label
#: ("Sexual harassment"), case-insensitively, when the app posts a type back.
#: The dropdown is served with both; being forgiving here costs nothing, while a
#: strict match would turn a label round-trip into a 422.
_COMPLAINT_TYPE_LOOKUP: dict[str, str] = {}
for _value, _label in enums.ComplaintType.choices:
    _COMPLAINT_TYPE_LOOKUP[_value.lower()] = _value
    _COMPLAINT_TYPE_LOOKUP[str(_label).lower()] = _value


def complaint_type_from_app(raw):
    """Resolve a posted complaint type to its enum value, or None if unknown."""
    if raw is None:
        return None
    return _COMPLAINT_TYPE_LOOKUP.get(str(raw).strip().lower())


def decision_label_for(complaint):
    """The single human-readable 'Decision' the list/detail columns show.

    Null until the case is closed. The design's examples ("Dismissed",
    "Suspension") are resolution sub-types, not the InvestigationDecision, so
    those win when present; otherwise the decision itself.

    Short-circuits on state: only a RESOLVED case carries a decision, so this
    never issues a query for a pending/open row.
    """
    if complaint.state != enums.ComplaintState.RESOLVED:
        return None
    resolution = _current_resolution(complaint)
    if resolution is None:
        return None
    if resolution.formal_resolution_type:
        return dict(enums.FormalResolutionType.choices).get(
            resolution.formal_resolution_type
        )
    if resolution.informal_resolution_type:
        return dict(enums.InformalResolutionType.choices).get(
            resolution.informal_resolution_type
        )
    return dict(enums.InvestigationDecision.choices).get(resolution.decision)


def _current_resolution(complaint):
    current = complaint.current_investigation
    return getattr(current, "resolution", None) if current else None
