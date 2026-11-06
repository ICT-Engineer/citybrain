"""End-to-end pipeline tests against the mocks (in-process)."""

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


def _reading(seq: int = 1, emergency_type: str | None = None) -> SensorReading:
    payload = {
        "sensor_id": "cam-1",
        "type": SensorType.EMERGENCY_CALL if emergency_type else SensorType.TRAFFIC,
        "value": 0.9,
        "unit": "ratio",
        "ts": datetime.now(UTC),
        "seq": seq,
        "sig": f"sig-{seq}",
        "severity_class": SeverityClass.CRITICAL if emergency_type else SeverityClass.NORMAL,
        "correlation_id": f"corr-{seq}",
        "zone": "downtown",
    }
    if emergency_type:
        payload["emergency"] = {"incident_type": emergency_type}
    return SensorReading.model_validate(payload)


class _DummyGuardian(GuardianClient):
    """In-process Guardian: routes requests to the mock app via TestClient."""

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
async def test_pipeline_no_incident_no_dispatch():
    """Non-emergency reading -> no incident -> nothing dispatched."""
    twin = _DummyTwin()
    p = Pipeline(guardian=_DummyGuardian(), twin=twin)
    result = await p.process(_reading(seq=1))
    assert result.discarded_reason == "no_incident"
    assert twin.dispatched() == []


@pytest.mark.asyncio
async def test_pipeline_trusted_dispatches(monkeypatch):
    """Emergency + trusted Guardian -> dispatch executed."""
    monkeypatch.delenv("GUARDIAN_CHAOS", raising=False)
    twin = _DummyTwin()
    p = Pipeline(guardian=_DummyGuardian(), twin=twin)
    result = await p.process(_reading(seq=2, emergency_type="accident"))
    assert result.incident_kind == "accident"
    assert result.verdict is not None
    assert result.verdict.verdict.value == "trusted"
    assert len(twin.dispatched()) == 2  # ambulance + police


@pytest.mark.asyncio
async def test_pipeline_rejected_no_dispatch(monkeypatch):
    """Rejected Guardian -> discard, nothing dispatched. Fail-safe §8."""
    monkeypatch.setenv("GUARDIAN_CHAOS", "rejected")
    twin = _DummyTwin()
    p = Pipeline(guardian=_DummyGuardian(), twin=twin)
    result = await p.process(_reading(seq=3, emergency_type="fire"))
    assert result.discarded_reason is not None
    assert twin.dispatched() == []


@pytest.mark.asyncio
async def test_pipeline_guardian_down_failsafe(monkeypatch):
    """Guardian down -> no dispatch. Fail-safe §8."""
    monkeypatch.setenv("GUARDIAN_CHAOS", "down")
    twin = _DummyTwin()
    p = Pipeline(guardian=_DummyGuardian(), twin=twin)
    result = await p.process(_reading(seq=4, emergency_type="accident"))
    assert result.discarded_reason is not None
    assert twin.dispatched() == []