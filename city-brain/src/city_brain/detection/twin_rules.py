"""Incident detection for Twin readings, driven by the Twin's own sensor catalog.

Thresholds are NOT hard-coded: they come from each sensor's `normal_limits` in the
shared catalog (src/city_brain/catalog/twin_sensors.json, copied from the Twin), so
the Brain and the Twin agree on what "abnormal" means for every one of the 16 sensors.

Each sensor has at most one active incident. A new incident fires on the transition
normal -> abnormal; it clears (and a recovery plan may run) once the value is back
inside 95 % of the normal band. This stops the Brain from re-dispatching every sample.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from city_brain import catalog
from city_brain.detection.rules import DetectedIncident, IncidentKind
from city_brain.schemas.twin import SensorReading

log = logging.getLogger(__name__)

TRAFFIC_SUSTAIN_SAMPLES = 2      # traffic above normal for 2 samples in a row = jam


@dataclass
class TwinDetection:
    incident: DetectedIncident | None = None   # a new incident to handle
    cleared: DetectedIncident | None = None    # a previously active incident that just ended
    note: str = ""


@dataclass
class SensorMemory:
    values: list[float] = field(default_factory=list)
    above_count: int = 0
    active: DetectedIncident | None = None


class TwinDetector:
    def __init__(self) -> None:
        self.mem: dict[str, SensorMemory] = {}

    def reset(self) -> None:
        self.mem.clear()

    def active(self) -> dict[str, DetectedIncident]:
        return {sid: m.active for sid, m in self.mem.items() if m.active is not None}

    def trend(self, sensor_id: str) -> float:
        """Normalised change over the last samples, in [-1, 1] (model feature value_trend)."""
        m = self.mem.get(sensor_id)
        spec = catalog.sensors().get(sensor_id)
        if not m or len(m.values) < 2 or spec is None:
            return 0.0
        lo, hi = spec.normal_limits
        span = (hi - lo) or 1.0
        return max(-1.0, min(1.0, (m.values[-1] - m.values[0]) / span))

    def _kind_for(self, spec: catalog.SensorSpec, value: float) -> IncidentKind | None:
        lo, hi = spec.normal_limits
        t = spec.type
        if t == "water_level":
            if value > hi:
                return IncidentKind.FLOOD
            if spec.sensor_id.startswith("RES-") and value < lo:
                return IncidentKind.LOW_RESERVOIR
            return None
        if t in ("traffic_count", "traffic_counter"):
            return IncidentKind.TRAFFIC_JAM if value > hi else None
        if t == "temperature":
            if spec.linked_device_ids and value > hi:
                return IncidentKind.OVERHEAT
            return IncidentKind.EQUIPMENT_ANOMALY if (value > hi or value < lo) else None
        if t in ("pressure", "flow"):
            return IncidentKind.EQUIPMENT_ANOMALY if (value > hi or value < lo) else None
        if t == "air_quality":
            return IncidentKind.AIR_POLLUTION if value > hi else None
        return None

    def _back_to_normal(self, spec: catalog.SensorSpec, value: float) -> bool:
        lo, hi = spec.normal_limits
        margin = 0.05 * (hi - lo)
        return (lo + margin if lo > 0 else lo) <= value <= hi - margin

    def observe(self, reading: SensorReading, *, evidence_sensor: str | None = None) -> TwinDetection:
        """Update memory with one usable reading and report incident transitions.

        `evidence_sensor` lets a backup sensor's reading speak for the primary
        (SCN-03: the dead IND-TMP-01 is replaced by IND-TMP-02).
        """
        sid = evidence_sensor or reading.sensor_id
        spec = catalog.spec_for(reading.sensor_id, reading.twin_type)
        if spec is None or not isinstance(reading.value, (int, float)):
            return TwinDetection(note="not a catalog sensor or no numeric value")
        value = float(reading.value)
        m = self.mem.setdefault(sid, SensorMemory())
        m.values = (m.values + [value])[-5:]
        kind = self._kind_for(spec, value)

        if m.active is not None:
            if kind is None and self._back_to_normal(spec, value):
                ended, m.active, m.above_count = m.active, None, 0
                return TwinDetection(cleared=ended, note="back inside the normal band")
            return TwinDetection(note="incident already active")

        if kind is None:
            m.above_count = 0
            return TwinDetection()

        if kind == IncidentKind.TRAFFIC_JAM:
            m.above_count += 1
            critical = reading.severity_class.value == "critical"
            if m.above_count < TRAFFIC_SUSTAIN_SAMPLES and not critical:
                return TwinDetection(note=f"traffic above normal ({m.above_count}/{TRAFFIC_SUSTAIN_SAMPLES})")

        lo, hi = spec.normal_limits
        inc = DetectedIncident(
            kind=kind,
            correlation_id=reading.correlation_id,
            sensor_id=sid,
            zone=reading.zone,
            severity_class=reading.severity_class.value,
            evidence={
                "sensor_id": reading.sensor_id,
                "value": value,
                "unit": spec.unit,
                "normal_limits": [lo, hi],
                "observation_id": reading.observation_id,
                "linked_device_ids": list(reading.linked_device_ids or spec.linked_device_ids),
            },
        )
        return TwinDetection(incident=inc)

    def mark_active(self, sensor_id: str, incident: DetectedIncident) -> None:
        self.mem.setdefault(sensor_id, SensorMemory()).active = incident
