

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent / "src"))

from city_brain.decision.model import get_model
from city_brain.schemas.twin import SensorReading, SensorType, SeverityClass
from datetime import datetime, timezone

model = get_model()

def test_reading(name: str, reading: SensorReading, expected: str | None = None):
    result = model.predict(reading)
    if result is None:
        print(f"❌ {name}: MODEL RETURNED NONE")
        return False
    
    action, confidence = result
    status = "✅" if (expected is None or action == expected) else "⚠️"
    
    print(f"{status} {name}")
    print(f"   → Action: {action} (confidence: {confidence:.2f})")
    if expected and action != expected:
        print(f"   → Expected: {expected}")
    return True

def make_reading(**kwargs):
    defaults = {
        "sensor_id": "test-1",
        "type": SensorType.EMERGENCY_CALL,
        "value": None,
        "unit": None,
        "ts": datetime.now(timezone.utc),
        "seq": 1,
        "sig": "test",
        "severity_class": SeverityClass.CRITICAL,
        "correlation_id": "corr-test",
        "zone": "downtown",
        "emergency": {"incident_type": "accident"},
    }
    defaults.update(kwargs)
    return SensorReading.model_validate(defaults)

print("=" * 60)
print("STRESS TEST — Decision Model Robustness")
print("=" * 60)

print("\n[1] STANDARD CASES (should work perfectly)")
test_reading("Standard accident", make_reading(), "dispatch_ambulance_police")
test_reading("Standard fire", make_reading(emergency={"incident_type": "fire"}), "dispatch_fire_ambulance")
test_reading("Standard flood", make_reading(
    type=SensorType.WATER, value=4.5, severity_class=SeverityClass.NORMAL, emergency=None
), "open_valve")
test_reading("Standard traffic", make_reading(
    type=SensorType.TRAFFIC, value=0.9, severity_class=SeverityClass.NORMAL, emergency=None
), "retime_traffic_signal")

print("\n[2] UNKNOWN INCIDENT TYPES (real Twin might send these)")
test_reading("Medical emergency", make_reading(emergency={"incident_type": "medical"}))
test_reading("Gas leak", make_reading(emergency={"incident_type": "gas"}))
test_reading("Building collapse", make_reading(emergency={"incident_type": "collapse"}))
test_reading("Unknown XYZ", make_reading(emergency={"incident_type": "xyz_unknown"}))

print("\n[3] EXTREME VALUES (real Twin might have different scales)")
test_reading("Water 500 (cm instead of m)", make_reading(
    type=SensorType.WATER, value=500.0, severity_class=SeverityClass.CRITICAL, emergency=None
))
test_reading("Water 0.1 (low)", make_reading(
    type=SensorType.WATER, value=0.1, severity_class=SeverityClass.NORMAL, emergency=None
))
test_reading("Traffic 100 (vehicles/min)", make_reading(
    type=SensorType.TRAFFIC, value=100.0, severity_class=SeverityClass.CRITICAL, emergency=None
))
test_reading("Traffic -5 (negative - sensor error)", make_reading(
    type=SensorType.TRAFFIC, value=-5.0, severity_class=SeverityClass.NORMAL, emergency=None
))

print("\n[4] EDGE CASES (missing data, weird fields)")
test_reading("Emergency call without incident_type", make_reading(emergency={"incident_type": "other"}))
test_reading("Fire with value set (weird but possible)", make_reading(
    emergency={"incident_type": "fire"}, value=0.5
))
test_reading("Air quality normal", make_reading(
    type=SensorType.AIR, value=0.3, severity_class=SeverityClass.NORMAL, emergency=None
))
test_reading("Power reading", make_reading(
    type=SensorType.POWER, value=0.9, severity_class=SeverityClass.NORMAL, emergency=None
))

print("\n[5] SEVERITY VARIATIONS (same incident, different severity)")
test_reading("Accident CRITICAL", make_reading(severity_class=SeverityClass.CRITICAL), "dispatch_ambulance_police")
test_reading("Accident NORMAL", make_reading(severity_class=SeverityClass.NORMAL), "dispatch_ambulance_police")
test_reading("Fire NORMAL", make_reading(
    emergency={"incident_type": "fire"}, severity_class=SeverityClass.NORMAL
), "dispatch_fire_ambulance")

print("\n" + "=" * 60)
print("END OF STRESS TEST")
print("=" * 60)