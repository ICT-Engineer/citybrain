"""Tests for the two processing paths (§3.1 critical, §3.2 normal)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from city_brain.clients.guardian_client import GuardianClient
from city_brain.clients.twin_client import TwinClient
from city_brain.pipeline.orchestrator import Pipeline
from city_brain.schemas.twin import SensorReading, SensorType, SeverityClass
from mocks.guardian_mock import app as guardian_app
from mocks.twin_mock import app as twin_app


def _reading(seq: int, severity: SeverityClass, incident_type: str = "accident") -> SensorReading:
    return SensorReading.model_validate({
        "sensor_id": f"call-{seq}",
        "type": SensorType.EMERGENCY_CALL,
        "value": None,
        "unit": None,
        "ts": datetime.now(UTC),
        "seq": seq,
        "sig": f"sig-{seq}",
        "severity_class": severity,
        "correlation_id": f"corr-{seq}",
        "zone": "downtown",
        "emergency": {"incident_type": incident_type},
    })


class _DummyGuardian(GuardianClient):
    def __init__(self):
        self._c = TestClient(guardian_app)

    async def verify(self, reading):
        r = self._c.post("/verify", json={"reading": reading.model_dump(mode="json")})
        if r.status_code >= 500:
            return None
        from city_brain.schemas.guardian import GuardianVerdict
        return GuardianVerdict.model_validate(r.json())


class _DummyTwin(TwinClient):
    def __init__(self):
        self._c = TestClient(twin_app)
        self._c.post("/actuator/reset")

    async def dispatch(self, command):
        from city_brain.schemas.twin import ActionAck
        r = self._c.post("/actuator/dispatch", json=command.model_dump(mode="json"))
        return ActionAck.model_validate(r.json())

    def dispatched(self):
        return self._c.get("/actuator/dispatched").json()


@pytest.mark.asyncio
async def test_critical_path_dispatches(monkeypatch):
    monkeypatch.delenv("GUARDIAN_CHAOS", raising=False)
    twin = _DummyTwin()
    p = Pipeline(guardian=_DummyGuardian(), twin=twin)
    r = await p.process(_reading(1, SeverityClass.CRITICAL))
    assert r.path == "critical"
    assert r.state.value == "dispatched"
    assert r.incident_kind == "accident"
    assert len(twin.dispatched()) == 2  # ambulance + police
    assert r.saved_ms >= -50.0


@pytest.mark.asyncio
async def test_normal_path_dispatches(monkeypatch):
    monkeypatch.delenv("GUARDIAN_CHAOS", raising=False)
    twin = _DummyTwin()
    p = Pipeline(guardian=_DummyGuardian(), twin=twin)
    r = await p.process(_reading(2, SeverityClass.NORMAL))
    assert r.path == "normal"
    assert r.state.value == "dispatched"
    assert len(twin.dispatched()) == 2


@pytest.mark.asyncio
async def test_critical_near_miss_when_rejected(monkeypatch):
    """Critical reading + rejected Guardian -> discard, no dispatch."""
    monkeypatch.setenv("GUARDIAN_CHAOS", "rejected")
    twin = _DummyTwin()
    p = Pipeline(guardian=_DummyGuardian(), twin=twin)
    r = await p.process(_reading(3, SeverityClass.CRITICAL))
    assert r.path == "critical"
    assert r.state.value == "discarded"
    assert r.discarded_reason is not None
    assert twin.dispatched() == []


@pytest.mark.asyncio
async def test_critical_failsafe_when_guardian_down(monkeypatch):
    """Critical reading + Guardian down -> no dispatch (fail-safe §8)."""
    monkeypatch.setenv("GUARDIAN_CHAOS", "down")
    twin = _DummyTwin()
    p = Pipeline(guardian=_DummyGuardian(), twin=twin)
    r = await p.process(_reading(4, SeverityClass.CRITICAL))
    assert r.path == "critical"
    assert r.state.value == "discarded"
    assert twin.dispatched() == []