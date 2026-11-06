"""Tests for the trained decision model + engine integration."""

from datetime import UTC, datetime

from city_brain.decision.engine import decide
from city_brain.decision.model import get_model
from city_brain.detection.rules import DetectedIncident, IncidentKind
from city_brain.schemas.twin import (
    EmergencyCallPayload,
    SensorReading,
    SensorType,
    SeverityClass,
)


def _reading(
    stype: SensorType,
    value: float | None = None,
    severity: SeverityClass = SeverityClass.NORMAL,
    incident_type: str | None = None,
) -> SensorReading:
    emergency = (
        EmergencyCallPayload(incident_type=incident_type) if incident_type else None
    )
    return SensorReading(
        sensor_id="s1",
        type=stype,
        value=value,
        unit="ratio" if value is not None else None,
        ts=datetime.now(UTC),
        seq=1,
        sig="sig1",
        severity_class=severity,
        correlation_id="corr-1",
        zone="downtown",
        emergency=emergency,
    )


def test_model_loads():
    m = get_model()
    assert m.ready, "model should be loaded"


def test_model_predicts_accident():
    m = get_model()
    r = _reading(SensorType.EMERGENCY_CALL, severity=SeverityClass.CRITICAL, incident_type="accident")
    result = m.predict(r)
    assert result is not None
    action, conf = result
    assert action == "dispatch_ambulance_police"
    assert conf > 0.5


def test_model_predicts_fire():
    m = get_model()
    r = _reading(SensorType.EMERGENCY_CALL, severity=SeverityClass.CRITICAL, incident_type="fire")
    result = m.predict(r)
    assert result is not None
    action, _ = result
    assert action == "dispatch_fire_ambulance"


def test_model_predicts_traffic_jam():
    m = get_model()
    r = _reading(SensorType.TRAFFIC, value=0.95)
    result = m.predict(r)
    assert result is not None
    action, _ = result
    assert action == "retime_traffic_signal"


def test_model_predicts_flood():
    m = get_model()
    r = _reading(SensorType.WATER, value=0.85, severity=SeverityClass.CRITICAL)
    result = m.predict(r)
    assert result is not None
    action, _ = result
    assert action == "open_valve"


def test_model_predicts_no_action_for_air():
    m = get_model()
    r = _reading(SensorType.AIR, value=0.3)
    result = m.predict(r)
    assert result is not None
    action, _ = result
    assert action == "no_action"


def test_engine_uses_model():
    inc = DetectedIncident(
        kind=IncidentKind.ACCIDENT,
        correlation_id="corr-1",
        sensor_id="s1",
        zone="downtown",
        severity_class="critical",
    )
    r = _reading(SensorType.EMERGENCY_CALL, severity=SeverityClass.CRITICAL, incident_type="accident")
    d = decide(inc, trust_score=0.95, reading=r)
    assert d.chosen_by == "model"
    assert len(d.commands) == 2
    assert d.commands[0].actuator_id == "ambulance@downtown"
    assert d.commands[1].actuator_id == "police@downtown"


def test_engine_fallback_when_no_reading():
    """Without a reading, engine must use rule-based fallback."""
    inc = DetectedIncident(
        kind=IncidentKind.FIRE,
        correlation_id="corr-2",
        sensor_id="s2",
        zone="downtown",
        severity_class="critical",
    )
    d = decide(inc, trust_score=0.95, reading=None)
    assert d.chosen_by == "rules"
    assert any(c.actuator_id.startswith("fire_brigade") for c in d.commands)