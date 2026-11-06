"""Turn raw readings into typed incidents.

ARCHITECTURE.md §5. Rules per sensor type.
State kept in-memory: traffic jam streak per sensor.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum

from city_brain.schemas.twin import SensorReading, SensorType

log = logging.getLogger(__name__)


class IncidentKind(StrEnum):
    ACCIDENT = "accident"
    FIRE = "fire"
    TRAFFIC_JAM = "traffic_jam"
    FLOOD = "flood"
    # Added for the Twin's sensor families (see detection/twin_rules.py)
    MEDICAL = "medical"
    OVERHEAT = "overheat"
    EQUIPMENT_ANOMALY = "equipment_anomaly"
    AIR_POLLUTION = "air_pollution"
    LOW_RESERVOIR = "low_reservoir"


@dataclass
class DetectedIncident:
    """Internal representation — not a wire format."""

    kind: IncidentKind
    correlation_id: str
    sensor_id: str
    zone: str
    severity_class: str
    evidence: dict = field(default_factory=dict)


# Thresholds — §5. Calibrate with the Twin team.
TRAFFIC_JAM_TICKS = 3
TRAFFIC_JAM_VALUE = 0.8
WATER_HARD_THRESHOLD = 3.0

# In-memory state (per-process; restart clears it — acceptable for v1)
_traffic_streak: dict[str, int] = {}


def _reset_state() -> None:
    """Test helper: clear all in-memory tracking."""
    _traffic_streak.clear()


def detect(reading: SensorReading) -> DetectedIncident | None:
    """Return an incident if this reading triggers a rule, else None. §5."""

    # --- §5.1 Accidents, §5.2 Fires: emergency_call feed ---
    if reading.type == SensorType.EMERGENCY_CALL and reading.emergency is not None:
        itype = reading.emergency.incident_type.lower()
        if itype == "accident":
            return DetectedIncident(
                kind=IncidentKind.ACCIDENT,
                correlation_id=reading.correlation_id,
                sensor_id=reading.sensor_id,
                zone=reading.zone,
                severity_class=reading.severity_class.value,
                evidence={
                    "incident_type": itype,
                    "reported_location": reading.emergency.reported_location,
                },
            )
        if itype == "fire":
            return DetectedIncident(
                kind=IncidentKind.FIRE,
                correlation_id=reading.correlation_id,
                sensor_id=reading.sensor_id,
                zone=reading.zone,
                severity_class=reading.severity_class.value,
                evidence={
                    "incident_type": itype,
                    "reported_location": reading.emergency.reported_location,
                },
            )
        log.debug("emergency_call unknown incident_type=%s", itype)
        return None

    # --- §5.3 Traffic jam: sustained high value on traffic sensor ---
    if reading.type == SensorType.TRAFFIC and isinstance(reading.value, (int, float)):
        if reading.value >= TRAFFIC_JAM_VALUE:
            _traffic_streak[reading.sensor_id] = _traffic_streak.get(reading.sensor_id, 0) + 1
        else:
            _traffic_streak[reading.sensor_id] = 0

        if _traffic_streak[reading.sensor_id] >= TRAFFIC_JAM_TICKS:
            streak = _traffic_streak[reading.sensor_id]
            _traffic_streak[reading.sensor_id] = 0  # reset so we don't re-fire every tick
            return DetectedIncident(
                kind=IncidentKind.TRAFFIC_JAM,
                correlation_id=reading.correlation_id,
                sensor_id=reading.sensor_id,
                zone=reading.zone,
                severity_class=reading.severity_class.value,
                evidence={"streak": streak, "value": reading.value},
            )

    # --- §5.4 Flood: water level above hard threshold ---
    if reading.type == SensorType.WATER and isinstance(reading.value, (int, float)):
        if reading.value >= WATER_HARD_THRESHOLD:
            return DetectedIncident(
                kind=IncidentKind.FLOOD,
                correlation_id=reading.correlation_id,
                sensor_id=reading.sensor_id,
                zone=reading.zone,
                severity_class=reading.severity_class.value,
                evidence={"water_level": reading.value, "threshold": WATER_HARD_THRESHOLD},
            )

    return None