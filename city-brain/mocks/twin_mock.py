"""Mock Twin — fake sensors + actuators for local testing.

Port 8002. Not for production. Emulates the minimum surface City Brain
needs: emit readings, receive dispatch commands, ack them back.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import FastAPI
from pydantic import BaseModel

from city_brain.schemas.twin import (
    ActionAck,
    DispatchCommand,
    SensorReading,
    SensorType,
    SeverityClass,
)
from city_brain.ingestion.stream import publish_reading

logging.basicConfig(level="INFO")
log = logging.getLogger("twin_mock")

app = FastAPI(title="Mock Twin", version="0.1.0")

# In-memory state
_latest: SensorReading | None = None
_dispatched: list[DispatchCommand] = []


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "role": "twin-mock"}


@app.post("/readings/emit")
async def emit(reading: SensorReading) -> dict[str, str]:
    global _latest
    _latest = reading
    log.info("emitted reading sensor=%s type=%s seq=%s", reading.sensor_id, reading.type, reading.seq)
    return {"accepted": reading.correlation_id}


@app.get("/readings/latest")
async def latest() -> SensorReading | None:
    return _latest


@app.post("/actuator/dispatch")
async def dispatch(command: DispatchCommand) -> ActionAck:
    _dispatched.append(command)
    log.info(
        "dispatch actuator=%s action=%s corr=%s trust=%.2f",
        command.actuator_id,
        command.action,
        command.correlation_id,
        command.trust_score,
    )
    return ActionAck(
        correlation_id=command.correlation_id,
        actuator_id=command.actuator_id,
        status="executed",
        ts=datetime.now(timezone.utc),
        detail="mock executed",
    )


@app.get("/actuator/dispatched")
async def dispatched() -> list[DispatchCommand]:
    return _dispatched


@app.post("/actuator/reset")
async def reset() -> dict[str, str]:
    global _latest
    _latest = None
    _dispatched.clear()
    return {"status": "reset"}


# Convenience endpoint: fabricate a reading without knowing the schema
@app.post("/readings/fabricate")
async def fabricate(
    sensor_id: str = "cam-1",
    type: SensorType = SensorType.TRAFFIC,
    value: float = 0.9,
    zone: str = "downtown",
    severity: SeverityClass = SeverityClass.NORMAL,
    seq: int = 1,
) -> SensorReading:
    r = SensorReading(
        sensor_id=sensor_id,
        type=type,
        value=value,
        unit="ratio",
        ts=datetime.now(timezone.utc),
        seq=seq,
        sig=f"mock-sig-{sensor_id}-{seq}",
        severity_class=severity,
        correlation_id=f"mock-{sensor_id}-{seq}",
        zone=zone,
    )
    global _latest
    _latest = r
    return r
@app.post("/readings/publish")
async def publish(
    sensor_id: str = "cam-redis-1",
    type: SensorType = SensorType.EMERGENCY_CALL,
    value: float | None = None,
    zone: str = "downtown",
    severity: SeverityClass = SeverityClass.CRITICAL,
    incident_type: str = "accident",
    seq: int = 1,
) -> dict:
    """Publish a reading to the Redis stream (real ingestion path)."""
    from datetime import datetime, timezone
    import uuid

    reading = SensorReading(
        sensor_id=sensor_id,
        type=type,
        value=value,
        unit=None,
        ts=datetime.now(timezone.utc),
        seq=seq,
        sig=f"mock-sig-{sensor_id}-{seq}",
        severity_class=severity,
        correlation_id=f"corr-{uuid.uuid4().hex[:8]}",
        zone=zone,
        emergency={"incident_type": incident_type} if type == SensorType.EMERGENCY_CALL else None,
    )
    msg_id = await publish_reading(reading)
    log.info("published to stream msg_id=%s corr=%s", msg_id, reading.correlation_id)
    return {"msg_id": msg_id, "correlation_id": reading.correlation_id}