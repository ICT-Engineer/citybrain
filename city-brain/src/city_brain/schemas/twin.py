from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SensorType(StrEnum):
    TRAFFIC = "traffic"
    WATER = "water"
    POWER = "power"
    AIR = "air"
    EMERGENCY_CALL = "emergency_call"
    # Added for the Twin's 16-sensor inventory (flow / pressure / temperature families)
    FLOW = "flow"
    PRESSURE = "pressure"
    TEMPERATURE = "temperature"


class SeverityClass(StrEnum):
    CRITICAL = "critical"
    NORMAL = "normal"


class EmergencyCallPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    incident_type: str
    reported_location: str | None = None
    caller_info: str | None = None


class SensorReading(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sensor_id: str
    type: SensorType
    value: float | int | str | None = None
    unit: str | None = None
    ts: datetime
    seq: int = Field(..., ge=0)
    sig: str
    severity_class: SeverityClass
    correlation_id: str
    zone: str
    emergency: EmergencyCallPayload | None = None
    # --- Twin public-reading fields (twin-reading-0.2-local), all optional so the
    # original Redis/HTTP contract keeps working unchanged.
    run_id: str | None = None
    sim_tick: int | None = None
    twin_type: str | None = None              # water_level, traffic_count, temperature, ...
    status: str | None = None                 # OK / FAILED / OFFLINE ...
    available: bool | None = None
    backup_ids: list[str] = Field(default_factory=list)
    linked_device_ids: list[str] = Field(default_factory=list)
    observation_id: str | None = None         # id the Guardian gives this sample


class DispatchCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actuator_id: str
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str
    trust_score: float = Field(..., ge=0.0, le=1.0)
    requested_by: str = "city-brain"


class ActionAck(BaseModel):
    model_config = ConfigDict(extra="allow")
    correlation_id: str
    actuator_id: str
    status: str
    detail: str | None = None
