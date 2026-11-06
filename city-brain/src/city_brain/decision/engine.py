"""Map an incident to a concrete response.

Step B: uses the trained DecisionModel to pick an action, translating
the action name into DispatchCommand objects. Falls back to the
rule-based table if the model is unavailable or not confident enough.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from city_brain.config import settings
from city_brain.decision.model import get_model
from city_brain.detection.rules import DetectedIncident, IncidentKind
from city_brain.schemas.twin import DispatchCommand

log = logging.getLogger(__name__)


@dataclass
class Decision:
    correlation_id: str
    commands: list[DispatchCommand] = field(default_factory=list)
    chosen_by: str = "rules"          # "model" or "rules"
    confidence: float = 0.0
    reason: str = ""


# ---------------------------------------------------------------------------
# Action name -> (actuator template, action verb, params)
# ---------------------------------------------------------------------------
_ACTION_MAP: dict[str, list[tuple[str, str, dict]]] = {
    "dispatch_ambulance_police": [
        ("ambulance@{zone}", "dispatch", {"priority": "high"}),
        ("police@{zone}", "dispatch", {"priority": "high"}),
    ],
    "dispatch_fire_ambulance": [
        ("fire_brigade@{zone}", "dispatch", {"priority": "high"}),
        ("ambulance@{zone}", "dispatch", {"priority": "high"}),
    ],
    "retime_traffic_signal": [
        ("traffic_signal@{zone}", "retime", {"extend_green_s": 30}),
    ],
    "open_valve": [
        ("valve@{zone}", "adjust", {"target_pct": 50}),
    ],
    "no_action": [],
}


# ---------------------------------------------------------------------------
# Rule-based fallback (kept from step 3)
# ---------------------------------------------------------------------------
_FALLBACK_RESPONSES: dict[IncidentKind, list[tuple[str, str, dict]]] = {
    IncidentKind.ACCIDENT: [
        ("ambulance@{zone}", "dispatch", {"priority": "high"}),
        ("police@{zone}", "dispatch", {"priority": "high"}),
    ],
    IncidentKind.FIRE: [
        ("fire_brigade@{zone}", "dispatch", {"priority": "high"}),
        ("ambulance@{zone}", "dispatch", {"priority": "high"}),
    ],
    IncidentKind.TRAFFIC_JAM: [
        ("traffic_signal@{zone}", "retime", {"extend_green_s": 30}),
    ],
    IncidentKind.FLOOD: [
        ("valve@{zone}", "adjust", {"target_pct": 50}),
    ],
}


def _build_commands(
    incident: DetectedIncident,
    plan: list[tuple[str, str, dict]],
    trust_score: float,
) -> list[DispatchCommand]:
    return [
        DispatchCommand(
            actuator_id=tmpl.format(zone=incident.zone),
            action=action,
            params=params,
            correlation_id=incident.correlation_id,
            trust_score=trust_score,
        )
        for tmpl, action, params in plan
    ]


def decide(incident: DetectedIncident, trust_score: float = 0.0, reading=None) -> Decision:
    """Return a Decision for the incident.

    Tries the ML model first (via `reading`), falls back to rules.
    """
    # Try the model if we have the original reading
    if reading is not None:
        model = get_model()
        result = model.predict(reading)
        if result is not None:
            action_name, confidence = result
            if confidence >= settings.model_confidence_threshold and action_name in _ACTION_MAP:
                plan = _ACTION_MAP[action_name]
                return Decision(
                    correlation_id=incident.correlation_id,
                    commands=_build_commands(incident, plan, trust_score),
                    chosen_by="model",
                    confidence=confidence,
                    reason=f"model predicted {action_name}",
                )
            log.info(
                "model below threshold or unknown action: action=%s conf=%.3f threshold=%.3f",
                action_name, confidence, settings.model_confidence_threshold,
            )

    # Fallback: rule-based
    plan = _FALLBACK_RESPONSES.get(incident.kind, [])
    return Decision(
        correlation_id=incident.correlation_id,
        commands=_build_commands(incident, plan, trust_score),
        chosen_by="rules",
        confidence=1.0,
        reason=f"rule-based response for {incident.kind.value}",
    )