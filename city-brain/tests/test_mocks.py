"""Smoke tests for the mocks — they run in-process via TestClient."""

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from city_brain.schemas.twin import SensorType, SeverityClass
from mocks.guardian_mock import app as guardian_app
from mocks.twin_mock import app as twin_app


def _reading_payload(sensor_id: str = "cam-1", seq: int = 1) -> dict:
    return {
        "sensor_id": sensor_id,
        "type": SensorType.TRAFFIC.value,
        "value": 0.9,
        "unit": "ratio",
        "ts": datetime.now(UTC).isoformat(),
        "seq": seq,
        "sig": f"sig-{sensor_id}-{seq}",
        "severity_class": SeverityClass.NORMAL.value,
        "correlation_id": f"corr-{sensor_id}-{seq}",
        "zone": "downtown",
    }


def test_twin_health():
    c = TestClient(twin_app)
    assert c.get("/health").json()["status"] == "ok"


def test_twin_emit_and_latest():
    c = TestClient(twin_app)
    c.post("/actuator/reset")
    r = c.post("/readings/emit", json=_reading_payload())
    assert r.status_code == 200
    latest = c.get("/readings/latest").json()
    assert latest["sensor_id"] == "cam-1"


def test_twin_dispatch_records_call():
    c = TestClient(twin_app)
    c.post("/actuator/reset")
    cmd = {
        "actuator_id": "ambulance@downtown",
        "action": "dispatch",
        "params": {"priority": "high"},
        "correlation_id": "corr-1",
        "trust_score": 0.95,
        "requested_by": "city-brain",
    }
    r = c.post("/actuator/dispatch", json=cmd)
    assert r.status_code == 200
    assert r.json()["status"] == "executed"
    dispatched = c.get("/actuator/dispatched").json()
    assert len(dispatched) == 1


def test_guardian_health():
    c = TestClient(guardian_app)
    assert c.get("/health").json()["status"] == "ok"


def test_guardian_verify_trusted_by_default(monkeypatch):
    monkeypatch.delenv("GUARDIAN_CHAOS", raising=False)
    c = TestClient(guardian_app)
    r = c.post("/verify", json={"reading": _reading_payload()})
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "trusted"
    assert body["trust_score"] == 0.95


def test_guardian_chaos_rejected(monkeypatch):
    monkeypatch.setenv("GUARDIAN_CHAOS", "rejected")
    c = TestClient(guardian_app)
    r = c.post("/verify", json={"reading": _reading_payload()})
    assert r.json()["verdict"] == "rejected"


def test_guardian_chaos_down(monkeypatch):
    monkeypatch.setenv("GUARDIAN_CHAOS", "down")
    c = TestClient(guardian_app)
    r = c.post("/verify", json={"reading": _reading_payload()})
    assert r.status_code == 500