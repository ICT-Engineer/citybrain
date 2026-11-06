from __future__ import annotations

from enum import StrEnum


class IncidentState(StrEnum):
    DETECTED = "detected"
    DECIDED_HELD = "decided_held"
    DISPATCHED = "dispatched"
    DISCARDED = "discarded"
    VERIFIED = "verified"
    DECIDED = "decided"


_ALLOWED: dict[IncidentState, set[IncidentState]] = {
    IncidentState.DETECTED: {
        IncidentState.DECIDED_HELD,  # critical path: decide before gate
        IncidentState.VERIFIED,      # normal path: verify first
        IncidentState.DISCARDED,     # normal path: Guardian rejects before we decide
    },
    IncidentState.DECIDED_HELD: {
        IncidentState.DISPATCHED,
        IncidentState.DISCARDED,     # critical path: verdict rejected the held decision
    },
    IncidentState.VERIFIED: {IncidentState.DECIDED},
    IncidentState.DECIDED: {IncidentState.DISPATCHED},
    IncidentState.DISPATCHED: set(),
    IncidentState.DISCARDED: set(),
}


class InvalidTransition(RuntimeError):
    pass


class IncidentStateMachine:
    def __init__(self, correlation_id: str) -> None:
        self.correlation_id = correlation_id
        self.state = IncidentState.DETECTED

    def transition(self, to: IncidentState) -> None:
        if to not in _ALLOWED[self.state]:
            raise InvalidTransition(f"{self.correlation_id}: {self.state} -> {to} not allowed")
        self.state = to
