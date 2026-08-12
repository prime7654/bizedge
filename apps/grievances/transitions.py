"""The complaint state machine.

One canonical ``state`` field, one table of legal transitions, one place that
performs them. Spec v4 section 3.

Every transition does the same four things, in this order, inside a single
transaction:

1. **Guard** -- is this move legal from the current state, and is this person
   allowed to make it?
2. **Mutate** -- change the state and whatever else the move implies.
3. **Audit** -- write a :class:`ComplaintEvent`. Never optional.
4. **Notify** -- tell the people who need to know.

Keeping that shape uniform is what makes the audit trail trustworthy: there is
no code path that moves a complaint without recording it.

Transitions are never expressed as PATCH on the model. A client cannot set
``state`` directly -- it calls a named action, and the action decides.
"""
from __future__ import annotations

from dataclasses import dataclass

from apps.grievances import enums

State = enums.ComplaintState


@dataclass(frozen=True)
class Transition:
    """One legal move."""

    name: str
    sources: frozenset[str]
    target: str
    description: str


#: The complete set of legal moves. Anything not listed here cannot happen.
#:
#: Note what is deliberately absent: SUBMITTED -> RESOLVED. Product confirmed
#: (Q4) that every case must have an investigator on record before it closes,
#: so even a trivial complaint goes through investigation with HR as the lead.
TRANSITIONS: dict[str, Transition] = {
    "appoint_investigator": Transition(
        name="appoint_investigator",
        sources=frozenset({State.SUBMITTED}),
        target=State.UNDER_INVESTIGATION,
        description="HR appoints an investigation lead and opens the case.",
    ),
    "withdraw_before_investigation": Transition(
        name="withdraw_before_investigation",
        sources=frozenset({State.SUBMITTED}),
        target=State.WITHDRAWN,
        description="The complainant retracts before any investigation starts.",
    ),
    "request_withdrawal": Transition(
        name="request_withdrawal",
        sources=frozenset({State.UNDER_INVESTIGATION}),
        target=State.AWAITING_DECISION,
        description=(
            "The complainant retracts mid-investigation. Routes to HR for a "
            "decision rather than closing outright -- a withdrawn complaint may "
            "still need investigating on the company's behalf."
        ),
    ),
    "submit_report": Transition(
        name="submit_report",
        sources=frozenset({State.UNDER_INVESTIGATION}),
        target=State.AWAITING_DECISION,
        description="The investigation lead submits their report.",
    ),
    "resolve": Transition(
        name="resolve",
        sources=frozenset({State.AWAITING_DECISION}),
        target=State.RESOLVED,
        description="HR records a decision and closes the case.",
    ),
    "reopen": Transition(
        name="reopen",
        sources=frozenset({State.RESOLVED}),
        target=State.UNDER_INVESTIGATION,
        description="HR reopens a closed case, starting a new investigation round.",
    ),
}


class TransitionError(Exception):
    """The move is not legal from the complaint's current state."""


def check(complaint, transition_name: str) -> Transition:
    """Return the transition if it is legal from ``complaint``'s state.

    Raises :class:`TransitionError` otherwise. Call this inside the same
    transaction that performs the move, after locking the row -- checking
    against a stale read is how two people both appoint an investigator.
    """
    try:
        transition = TRANSITIONS[transition_name]
    except KeyError as exc:  # pragma: no cover - programmer error
        raise TransitionError(f"Unknown transition: {transition_name}") from exc

    if complaint.state not in transition.sources:
        allowed = ", ".join(sorted(transition.sources))
        raise TransitionError(
            f"Cannot {transition_name} a complaint that is {complaint.state}. "
            f"Only allowed from: {allowed}."
        )
    return transition


def available_transitions(state: str) -> list[str]:
    """Every move legal from ``state``.

    Handy for telling a client which action buttons to render, rather than
    having it reimplement this table.
    """
    return [name for name, t in TRANSITIONS.items() if state in t.sources]
