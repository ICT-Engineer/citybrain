#!/usr/bin/env python3
"""Bootstrap the City Brain project. Run: python bootstrap.py"""

from pathlib import Path

FILES = {}

FILES["ARCHITECTURE.md"] = """# City Brain Layer — Architecture

**Challenge:** TSYP14 — City Brain Convergence
**Layer owned by this team:** City Brain (operational decision-maker)
**Status:** Draft, Phase 1 preparation

---

## 1. Role in the system

City Brain sits between the Twin (sensors/actuators) and Guardian (trust verifier). One job: consume real-time sensor data, decide the correct operational response, dispatch through Twin's actuator tools — only once Guardian confirms the reading is trustworthy.

---

## 2. System architecture

Twin broadcasts every reading to both City Brain and Guardian. Guardian returns a trust verdict. City Brain only dispatches to actuators with a trusted verdict attached.

---

## 3. Two processing paths

Critical path (parallel): compute decision immediately, hold it, dispatch only if Guardian says trusted.
Normal path (sequential): wait for Guardian verdict, then decide and dispatch.

Rule on both paths: nothing dispatched without a trusted Guardian verdict attached.

---

## 4. Data contract

### 4.1 Twin -> City Brain
sensor_id, type, value, unit, ts, seq, sig, severity_class, correlation_id, zone, emergency payload, action_ack.

### 4.2 Guardian -> City Brain
correlation_id, sensor_id, ts, seq, trust_score, verdict, sig, reason_codes.

### 4.3 City Brain -> Twin
actuator_id, action, params, correlation_id, trust_score, requested_by.

---

## 5. Incident types

- Accidents, Fires, Traffic jams, Flooding — see rules.py TODOs.

---

## 6. State machine

Critical: Detected -> Decided (held) -> Dispatched | Discarded
Normal:   Detected -> Verified -> Decided -> Dispatched

---

## 7. Technology

Python 3.12 + FastAPI, mcp SDK, Redis Streams, PostgreSQL, pytest, GitHub Actions, Docker.

---

## 8. Security

- Fail-safe default: Guardian unreachable -> no action
- Independent sig check
- Replay protection via monotonic seq
- Trust gate enforced twice
- Full audit log

---

## 9. Open questions for the triplet

- Who sets severity_class?
- Exact trust_score threshold?
- correlation_id format?
- Twin-side threshold enforcement?
"""

FILES["ROADMAP.md"] = """# City Brain — Build Roadmap

1. [x] Project skeleton
2. [ ] Mock Twin + mock Guardian
3. [ ] Ingestion layer
4. [ ] Incident detector
5. [ ] Decision engine + state machine
6. [ ] Trust gate + dispatch
7. [ ] Audit logging + adversarial tests
8. [ ] Real integration
"""

FILES["README.md"] = """# City Brain

Operational decision-maker layer for TSYP14.

## Quickstart
    cp .env.example .env
    docker compose up --build

## Local dev
    python -m venv .venv
    .venv\\Scripts\\activate
    pip install -e ".[dev]"
    pytest
"""

FILES["pyproject.toml"] = '''[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "city-brain"
version = "0.1.0"
description = "City Brain layer"
requires-python = ">=3.12,<3.13"
dependencies = [
    "fastapi>=0.115,<0.116",
    "uvicorn[standard]>=0.32,<0.33",
    "pydantic>=2.9,<3",
    "pydantic-settings>=2.6,<3",
    "redis>=5.2,<6",
    "asyncpg>=0.30,<0.31",
    "httpx>=0.27,<0.28",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.3,<9",
    "pytest-asyncio>=0.24,<0.25",
    "ruff>=0.7,<0.8",
    "mypy>=1.13,<2",
]

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP"]
'''

FILES["Dockerfile"] = """FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install -e ".[dev]"
EXPOSE 8000
CMD ["uvicorn", "city_brain.main:app", "--host", "0.0.0.0", "--port", "8000"]
"""

FILES["docker-compose.yml"] = """services:
  api:
    build: .
    ports: ["8000:8000"]
    environment:
      DATABASE_URL: postgresql://citybrain:citybrain@postgres:5432/citybrain
      REDIS_URL: redis://redis:6379/0
      TRUST_THRESHOLD: "0.7"
    depends_on:
      postgres: {condition: service_healthy}
      redis: {condition: service_healthy}
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: citybrain
      POSTGRES_PASSWORD: citybrain
      POSTGRES_DB: citybrain
    ports: ["5432:5432"]
    volumes: [pgdata:/var/lib/postgresql/data]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U citybrain"]
      interval: 5s
      retries: 10
  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      retries: 10
volumes:
  pgdata:
"""

FILES[".env.example"] = """DATABASE_URL=postgresql://citybrain:citybrain@localhost:5432/citybrain
REDIS_URL=redis://localhost:6379/0
GUARDIAN_URL=http://localhost:8001
TRUST_THRESHOLD=0.7
TWIN_URL=http://localhost:8002
LOG_LEVEL=INFO
"""

FILES[".gitignore"] = """__pycache__/
*.py[cod]
.venv/
venv/
.env
.pytest_cache/
.mypy_cache/
.ruff_cache/
"""

FILES[".dockerignore"] = """.git
.venv
__pycache__
.env
tests
"""

FILES[".github/workflows/ci.yml"] = """name: CI
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.12"}
      - run: pip install -e ".[dev]"
      - run: ruff check src tests
      - run: pytest -q
"""

FILES["src/city_brain/__init__.py"] = '__version__ = "0.1.0"\n'

FILES["src/city_brain/config.py"] = '''from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql://citybrain:citybrain@localhost:5432/citybrain"
    redis_url: str = "redis://localhost:6379/0"
    guardian_url: str = "http://localhost:8001"
    trust_threshold: float = 0.7
    twin_url: str = "http://localhost:8002"
    log_level: str = "INFO"


settings = Settings()
'''

FILES["src/city_brain/schemas/__init__.py"] = '''from city_brain.schemas.guardian import GuardianVerdict, Verdict
from city_brain.schemas.twin import ActionAck, DispatchCommand, SensorReading, SensorType, SeverityClass

__all__ = ["ActionAck", "DispatchCommand", "GuardianVerdict", "SensorReading", "SensorType", "SeverityClass", "Verdict"]
'''

FILES["src/city_brain/schemas/twin.py"] = '''from __future__ import annotations
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
'''

FILES["src/city_brain/schemas/guardian.py"] = '''from __future__ import annotations
from datetime import datetime
from enum import StrEnum
from pydantic import BaseModel, ConfigDict, Field


class Verdict(StrEnum):
    TRUSTED = "trusted"
    SUSPICIOUS = "suspicious"
    REJECTED = "rejected"


class GuardianVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    correlation_id: str
    sensor_id: str
    ts: datetime
    seq: int
    trust_score: float = Field(..., ge=0.0, le=1.0)
    verdict: Verdict
    sig: str
    reason_codes: list[str] = Field(default_factory=list)
'''

FILES["src/city_brain/ingestion/__init__.py"] = '''from city_brain.ingestion.stream import RedisStreamConsumer, SensorStreamConsumer
__all__ = ["RedisStreamConsumer", "SensorStreamConsumer"]
'''

FILES["src/city_brain/ingestion/stream.py"] = '''from __future__ import annotations
import abc
import logging
from collections.abc import AsyncIterator
from city_brain.schemas.twin import SensorReading

log = logging.getLogger(__name__)


class SensorStreamConsumer(abc.ABC):
    @abc.abstractmethod
    async def start(self) -> None: ...
    @abc.abstractmethod
    async def stop(self) -> None: ...
    @abc.abstractmethod
    def readings(self) -> AsyncIterator[SensorReading]: ...


class RedisStreamConsumer(SensorStreamConsumer):
    def __init__(self) -> None:
        self._running = False

    async def start(self) -> None:
        # TODO: connect redis.asyncio, XGROUP CREATE MKSTREAM
        self._running = True
        log.info("RedisStreamConsumer started")

    async def stop(self) -> None:
        self._running = False

    async def readings(self) -> AsyncIterator[SensorReading]:
        # TODO: XREADGROUP loop
        if False:  # pragma: no cover
            yield SensorReading.model_validate({})
        return
'''

FILES["src/city_brain/detection/__init__.py"] = '''from city_brain.detection.rules import DetectedIncident, IncidentKind, detect
__all__ = ["DetectedIncident", "IncidentKind", "detect"]
'''

FILES["src/city_brain/detection/rules.py"] = '''from __future__ import annotations
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from city_brain.schemas.twin import SensorReading

log = logging.getLogger(__name__)


class IncidentKind(StrEnum):
    ACCIDENT = "accident"
    FIRE = "fire"
    TRAFFIC_JAM = "traffic_jam"
    FLOOD = "flood"


@dataclass
class DetectedIncident:
    kind: IncidentKind
    correlation_id: str
    sensor_id: str
    zone: str
    severity_class: str
    evidence: dict = field(default_factory=dict)


def detect(reading: SensorReading) -> DetectedIncident | None:
    # TODO §5.1 emergency_call accident -> ACCIDENT
    # TODO §5.2 emergency_call fire    -> FIRE
    # TODO §5.3 sustained traffic      -> TRAFFIC_JAM
    # TODO §5.4 water level + rate     -> FLOOD
    return None
'''

FILES["src/city_brain/decision/__init__.py"] = '''from city_brain.decision.engine import Decision, decide
from city_brain.decision.state_machine import IncidentState, IncidentStateMachine
__all__ = ["Decision", "IncidentState", "IncidentStateMachine", "decide"]
'''

FILES["src/city_brain/decision/state_machine.py"] = '''from __future__ import annotations
from enum import StrEnum


class IncidentState(StrEnum):
    DETECTED = "detected"
    DECIDED_HELD = "decided_held"
    DISPATCHED = "dispatched"
    DISCARDED = "discarded"
    VERIFIED = "verified"
    DECIDED = "decided"


_ALLOWED: dict[IncidentState, set[IncidentState]] = {
    IncidentState.DETECTED: {IncidentState.DECIDED_HELD, IncidentState.VERIFIED},
    IncidentState.DECIDED_HELD: {IncidentState.DISPATCHED, IncidentState.DISCARDED},
    IncidentState.VERIFIED: {IncidentState.DECIDED},
    IncidentState.DECIDED: {IncidentState.DISPATCHED},
    IncidentState.DISPATCHED: set(),
    IncidentState.DISCARDED: set(),
}


class InvalidTransition(RuntimeError):
    pass


class IncidentStateMachine:
    def __init__(self, correlation_id: str) -> None:
        self.correlation_id = correlation_id
        self.state = IncidentState.DETECTED

    def transition(self, to: IncidentState) -> None:
        if to not in _ALLOWED[self.state]:
            raise InvalidTransition(f"{self.correlation_id}: {self.state} -> {to} not allowed")
        self.state = to
'''

FILES["src/city_brain/decision/engine.py"] = '''from __future__ import annotations
from dataclasses import dataclass, field
from city_brain.detection.rules import DetectedIncident, IncidentKind
from city_brain.schemas.twin import DispatchCommand


@dataclass
class Decision:
    correlation_id: str
    commands: list[DispatchCommand] = field(default_factory=list)


_RESPONSES: dict[IncidentKind, list[tuple[str, str, dict]]] = {
    IncidentKind.ACCIDENT: [("ambulance@{zone}", "dispatch", {})],
    IncidentKind.FIRE: [("fire_brigade@{zone}", "dispatch", {})],
    IncidentKind.TRAFFIC_JAM: [("traffic_signal@{zone}", "retime", {})],
    IncidentKind.FLOOD: [("valve@{zone}", "adjust", {})],
}


def decide(incident: DetectedIncident, trust_score: float) -> Decision:
    commands = [
        DispatchCommand(
            actuator_id=tmpl.format(zone=incident.zone),
            action=action,
            params=params,
            correlation_id=incident.correlation_id,
            trust_score=trust_score,
        )
        for tmpl, action, params in _RESPONSES.get(incident.kind, [])
    ]
    return Decision(correlation_id=incident.correlation_id, commands=commands)
'''

FILES["src/city_brain/dispatch/__init__.py"] = '''from city_brain.dispatch.trust_gate import TrustGate, TrustGateResult
__all__ = ["TrustGate", "TrustGateResult"]
'''

FILES["src/city_brain/dispatch/trust_gate.py"] = '''from __future__ import annotations
from dataclasses import dataclass
from city_brain.config import settings
from city_brain.schemas.guardian import GuardianVerdict, Verdict


@dataclass
class TrustGateResult:
    allowed: bool
    reason: str


class TrustGate:
    def __init__(self, threshold: float | None = None) -> None:
        self.threshold = threshold if threshold is not None else settings.trust_threshold

    def check(self, verdict: GuardianVerdict | None) -> TrustGateResult:
        if verdict is None:
            return TrustGateResult(False, "guardian_unreachable")
        if verdict.verdict != Verdict.TRUSTED:
            return TrustGateResult(False, f"verdict_{verdict.verdict}")
        if verdict.trust_score < self.threshold:
            return TrustGateResult(False, "below_threshold")
        return TrustGateResult(True, "trusted")
'''

FILES["src/city_brain/audit/__init__.py"] = '''from city_brain.audit.log import AuditLogger
__all__ = ["AuditLogger"]
'''

FILES["src/city_brain/audit/log.py"] = '''from __future__ import annotations
import logging
from typing import Any

log = logging.getLogger(__name__)


class AuditLogger:
    async def record(self, event_type: str, correlation_id: str, payload: dict[str, Any]) -> None:
        log.info("AUDIT %s correlation_id=%s payload=%s", event_type, correlation_id, payload)
'''

FILES["src/city_brain/api/__init__.py"] = '''from city_brain.api.routes import router
__all__ = ["router"]
'''

FILES["src/city_brain/api/routes.py"] = '''from __future__ import annotations
from fastapi import APIRouter
from city_brain.schemas.guardian import GuardianVerdict
from city_brain.schemas.twin import DispatchCommand

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/verify_reading")
async def verify_reading(verdict: GuardianVerdict) -> dict[str, str]:
    return {"received": verdict.correlation_id}


@router.post("/dispatch")
async def dispatch(command: DispatchCommand) -> dict[str, str]:
    return {"dispatched": command.correlation_id}
'''

FILES["src/city_brain/main.py"] = '''from __future__ import annotations
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from city_brain.api.routes import router
from city_brain.config import settings

logging.basicConfig(level=settings.log_level)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("City Brain starting")
    yield
    log.info("City Brain stopping")


app = FastAPI(title="City Brain", version="0.1.0", lifespan=lifespan)
app.include_router(router)
'''

FILES["tests/__init__.py"] = ""

FILES["tests/conftest.py"] = '''import pytest
from fastapi.testclient import TestClient
from city_brain.main import app


@pytest.fixture
def client():
    return TestClient(app)
'''

FILES["tests/test_schemas.py"] = '''from datetime import datetime, timezone
from city_brain.schemas.twin import SensorReading, SensorType, SeverityClass
from city_brain.schemas.guardian import GuardianVerdict, Verdict


def test_reading_accepts_valid():
    r = SensorReading(
        sensor_id="cam-12", type=SensorType.TRAFFIC, value=0.9, unit="ratio",
        ts=datetime.now(timezone.utc), seq=1, sig="abc",
        severity_class=SeverityClass.NORMAL,
        correlation_id="550e8400-e29b-41d4-a716-446655440000",
        zone="downtown",
    )
    assert r.sensor_id == "cam-12"


def test_verdict_accepts_valid():
    v = GuardianVerdict(
        correlation_id="550e8400-e29b-41d4-a716-446655440000",
        sensor_id="cam-12", ts=datetime.now(timezone.utc), seq=1,
        trust_score=0.95, verdict=Verdict.TRUSTED, sig="xyz",
    )
    assert v.trust_score == 0.95
'''

FILES["tests/test_smoke.py"] = '''def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
'''

FILES["scripts/scenario_runner.py"] = '''"""Adversarial scenario runner. §8. TODO: implement."""

SCENARIOS = ["spoofed_reading", "frozen_sensor", "guardian_unreachable",
             "duplicate_event", "replayed_reading"]


def main() -> None:
    for name in SCENARIOS:
        print(f"[TODO] run scenario: {name}")


if __name__ == "__main__":
    main()
'''


def main() -> None:
    created = 0
    for rel, content in FILES.items():
        path = Path(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(f"  + {rel}")
        created += 1
    print(f"\nDone. {created} files created.")


if __name__ == "__main__":
    main()