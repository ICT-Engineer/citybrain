from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from city_brain.audit.db import audit_db
from city_brain.mcp import get_tools
from city_brain.mcp.schemas import MCPTool
from city_brain.observability.live_events import get_live_events, log_live_event
from city_brain.observability.metrics import METRICS
from city_brain.pipeline.orchestrator import Pipeline, PipelineResult
from city_brain.schemas.guardian import GuardianVerdict
from city_brain.schemas.twin import DispatchCommand, SensorReading

router = APIRouter()
_pipeline = Pipeline()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/ingest", response_model=PipelineResult)
async def ingest(reading: SensorReading) -> PipelineResult:
    """Direct HTTP ingestion for testing."""
    log_live_event("Reading Arrived", f"Sensor: {reading.sensor_id} | Type: {reading.type.value} | Severity: {reading.severity_class.value}", "reading")
    
    result = await _pipeline.process(reading)
    
    if result.discarded_reason:
        log_live_event("Incident Discarded", f"Reason: {result.discarded_reason}", "discard")
    elif result.dispatched:
        log_live_event("Action Dispatched", f"Dispatched {len(result.dispatched)} actuator(s)", "dispatch")
        
    return result


@router.post("/verify_reading")
async def verify_reading(verdict: GuardianVerdict) -> dict[str, str]:
    return {"received": verdict.correlation_id}


@router.post("/dispatch")
async def dispatch(command: DispatchCommand) -> dict[str, str]:
    return {"dispatched": command.correlation_id}


@router.get("/mcp/tools", response_model=list[MCPTool])
async def mcp_tools() -> list[MCPTool]:
    """MCP-style tool discovery endpoint."""
    return get_tools()


@router.get("/metrics")
async def metrics() -> dict:
    """Operational metrics for observability and evaluation."""
    return METRICS.snapshot()


@router.get("/audit/{correlation_id}")
async def audit_trace(correlation_id: str) -> dict:
    """Full audit trail for one incident, read from PostgreSQL."""
    events = await audit_db.fetch_trace(correlation_id)
    return {
        "correlation_id": correlation_id,
        "count": len(events),
        "events": events,
    }


@router.get("/audit")
async def audit_count() -> dict:
    """Total number of audit events stored."""
    return {"total_events": await audit_db.count()}


# --- R3 Requirement: Live Timeline & Simulation ---

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    html_path = Path(__file__).parent.parent / "static" / "dashboard.html"
    if html_path.exists():
        content = html_path.read_text(encoding="utf-8")
        # Force le navigateur à ne pas mettre en cache la page HTML
        return HTMLResponse(content=content, headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0"
        })
    return "<h1>Dashboard HTML not found</h1>"

@router.get("/api/live-events")
async def api_live_events():
    return get_live_events()

@router.post("/api/simulate")
async def simulate_incident(incident_type: str = Query("accident")):
    """Generate a realistic reading to trigger the pipeline (for demo)."""
    sensor_id = f"sim-{random.randint(1000, 9999)}"
    corr_id = f"corr-sim-{random.randint(100000, 999999)}"
    zone = random.choice(["downtown", "harbor", "north", "south"])
    now = datetime.now(UTC).isoformat()
    
    # 1. TRAFIC (3 lectures consécutives)
    if incident_type == "traffic_jam":
        fixed_sensor = f"sim-traffic-{random.randint(1000, 9999)}"
        for i in range(1, 4):
            payload = {
                "sensor_id": fixed_sensor,
                "type": "traffic",
                "ts": datetime.now(UTC).isoformat(),
                "seq": i,
                "sig": f"sig-{i}",
                "severity_class": "normal",
                "correlation_id": f"{corr_id}-{i}",
                "zone": zone,
                "value": 0.9,
                "unit": "ratio",
            }
            reading = SensorReading.model_validate(payload)
            log_live_event("Reading Arrived", f"Sensor: {fixed_sensor} (Tick {i}/3) | Type: traffic", "reading")
            
            result = await _pipeline.process(reading)
            if result.discarded_reason:
                log_live_event("Incident Discarded", f"Reason: {result.discarded_reason}", "discard")
            elif result.dispatched:
                log_live_event("Action Dispatched", f"Dispatched {len(result.dispatched)} actuator(s) for traffic_jam", "dispatch")
            
            await asyncio.sleep(0.5)
            
        return {"status": "simulated_traffic", "correlation_id": corr_id}

    # 2. INONDATION (Capteur d'eau > seuil)
    if incident_type == "flood":
        payload = {
            "sensor_id": f"water-sensor-{random.randint(100, 999)}",
            "type": "water",
            "ts": now,
            "seq": random.randint(1, 1000),
            "sig": "mock-sig",
            "severity_class": "critical",
            "correlation_id": corr_id,
            "zone": zone,
            "value": 4.5, # > WATER_HARD_THRESHOLD (3.0)
            "unit": "meters",
        }
        reading = SensorReading.model_validate(payload)
        log_live_event("Reading Arrived", f"Sensor: {payload['sensor_id']} | Type: water | Severity: critical", "reading")
        
        result = await _pipeline.process(reading)
        if result.discarded_reason:
            log_live_event("Incident Discarded", f"Reason: {result.discarded_reason}", "discard")
        elif result.dispatched:
            log_live_event("Action Dispatched", f"Dispatched {len(result.dispatched)} actuator(s) for flood", "dispatch")
            
        return {"status": "simulated_flood", "correlation_id": corr_id}

    # 3. FEU / ACCIDENT (Emergency Call)
    payload = {
        "sensor_id": sensor_id,
        "type": "emergency_call",
        "ts": now,
        "seq": random.randint(1, 1000),
        "sig": "mock-sig",
        "severity_class": "critical",
        "correlation_id": corr_id,
        "zone": zone,
        "emergency": {"incident_type": incident_type},
        "value": None,
    }

    reading = SensorReading.model_validate(payload)
    log_live_event("Reading Arrived", f"Sensor: {sensor_id} | Type: emergency_call | Severity: critical", "reading")
    
    result = await _pipeline.process(reading)
    
    if result.discarded_reason:
        log_live_event("Incident Discarded", f"Reason: {result.discarded_reason}", "discard")
    elif result.dispatched:
        log_live_event("Action Dispatched", f"Dispatched {len(result.dispatched)} actuator(s) for {incident_type}", "dispatch")
        
    return {"status": "simulated", "correlation_id": corr_id}