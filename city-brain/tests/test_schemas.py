from datetime import UTC, datetime

from city_brain.schemas.guardian import GuardianVerdict, Verdict
from city_brain.schemas.twin import SensorReading, SensorType, SeverityClass


def test_reading_accepts_valid():
    r = SensorReading(
        sensor_id="cam-12", type=SensorType.TRAFFIC, value=0.9, unit="ratio",
        ts=datetime.now(UTC), seq=1, sig="abc",
        severity_class=SeverityClass.NORMAL,
        correlation_id="550e8400-e29b-41d4-a716-446655440000",
        zone="downtown",
    )
    assert r.sensor_id == "cam-12"


def test_verdict_accepts_valid():
    v = GuardianVerdict(
        correlation_id="550e8400-e29b-41d4-a716-446655440000",
        sensor_id="cam-12", ts=datetime.now(UTC), seq=1,
        trust_score=0.95, verdict=Verdict.TRUSTED, sig="xyz",
    )
    assert v.trust_score == 0.95
