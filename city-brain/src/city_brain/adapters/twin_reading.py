"""Twin public reading (twin-reading-0.2-local) -> Brain SensorReading.

A Twin reading looks like (GET /api/v1/sensors -> data.readings[]):

    {"sensor_id": "PRT-WLV-01", "zone_id": "PRT", "type": "water_level", "unit": "cm",
     "timestamp": "...Z", "sim_tick": 935, "delivery_tick": 935, "reported_value": 180.0,
     "status": "OK", "available": true, "backup_ids": ["PRT-WLV-02"],
     "linked_device_ids": ["PRT-PMP-01"], "seq": 1880, "run_id": "twin-shared-001"}

Only public fields are used: the Twin strips ground truth before any partner sees it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from city_brain import catalog
from city_brain.schemas.twin import SensorReading, SensorType, SeverityClass

# Twin sensor family -> Brain SensorType
TYPE_MAP: dict[str, SensorType] = {
    "water_level": SensorType.WATER,
    "traffic_count": SensorType.TRAFFIC,
    "traffic_counter": SensorType.TRAFFIC,      # name used in the Twin interface card
    "air_quality": SensorType.AIR,
    "power_load": SensorType.POWER,
    "flow": SensorType.FLOW,
    "pressure": SensorType.PRESSURE,
    "temperature": SensorType.TEMPERATURE,
    "emergency_call": SensorType.EMERGENCY_CALL,
}


def guardian_observation_id(run_id: str | None, sensor_id: str, seq: int | None, sim_tick: int | None = None) -> str:
    """Same id the Guardian layer gives a polled Twin sample.

    aya883/guardian_layer src/guardian/adapters/twin.py:
        prefix = (run_id or "run")[-12:];  marker = row.get("seq", row.get("sim_tick"))
        observation_id = f"{prefix}-{sensor_id}-{marker}"
    """
    prefix = (run_id or "run")[-12:]
    marker = seq if seq is not None else sim_tick
    return f"{prefix}-{sensor_id}-{marker}"


def severity_for(sensor_id: str, twin_type: str, value: float | None) -> SeverityClass:
    """Critical when a reading is far outside its normal band (catalog limits).

    - water_level: >= 1.25 x normal max (harbour 80 cm -> critical from 100 cm), so a
      flood takes the parallel critical path early.
    - traffic_count: >= midpoint of normal max and hard max (DTN: 60/120 -> 90 veh/min).
    - others: beyond the midpoint between the normal limit and the hard limit.
    """
    spec = catalog.spec_for(sensor_id, twin_type)
    if spec is None or value is None:
        return SeverityClass.NORMAL
    lo, hi = spec.normal_limits
    hlo, hhi = spec.hard_limits
    if twin_type == "water_level":
        return SeverityClass.CRITICAL if value >= hi * 1.25 else SeverityClass.NORMAL
    if twin_type in ("traffic_count", "traffic_counter"):
        return SeverityClass.CRITICAL if value >= (hi + hhi) / 2 else SeverityClass.NORMAL
    if value > (hi + hhi) / 2 or value < (lo + hlo) / 2:
        return SeverityClass.CRITICAL
    return SeverityClass.NORMAL


_ID = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


def validate_twin_row(row) -> str | None:
    """Clear reason when a partner sends something the Brain cannot use, else None.

    Codes: NOT_AN_OBJECT, MISSING_FIELD:<name>, BAD_SENSOR_ID, UNKNOWN_TYPE:<type>,
    BAD_VALUE (non-numeric, bool, NaN or infinite), BAD_SEQ, BAD_TIMESTAMP.
    A reported_value of null is allowed: it is how the Twin reports a dead (FAILED) sensor.
    """
    import math
    if not isinstance(row, dict):
        return "NOT_AN_OBJECT"
    for f in ("sensor_id", "type"):
        if f not in row:
            return f"MISSING_FIELD:{f}"
    if not isinstance(row["sensor_id"], str) or not _ID.match(row["sensor_id"]):
        return "BAD_SENSOR_ID"
    if str(row["type"]) not in TYPE_MAP:
        return f"UNKNOWN_TYPE:{str(row['type'])[:24]}"
    v = row.get("reported_value")
    if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)):
        return "BAD_VALUE"
    for f in ("seq", "sim_tick"):
        if row.get(f) is not None and (isinstance(row[f], bool) or not isinstance(row[f], int) or row[f] < 0):
            return "BAD_SEQ"
    ts = row.get("timestamp")
    if ts is not None:
        try:
            datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            return "BAD_TIMESTAMP"
    return None


def from_twin(row: dict, run_id: str | None = None) -> SensorReading | None:
    """Convert one Twin reading. Returns None for rows the Brain cannot use (see validate_twin_row)."""
    if validate_twin_row(row) is not None:
        return None
    sid = row.get("sensor_id")
    twin_type = str(row.get("type", ""))
    stype = TYPE_MAP.get(twin_type)
    if not isinstance(sid, str) or stype is None:
        return None
    run = row.get("run_id") or run_id
    seq = row.get("seq")
    sim_tick = row.get("sim_tick")
    value = row.get("reported_value")
    value = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    ts_raw = row.get("timestamp")
    try:
        ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00")) if ts_raw else datetime.now(UTC)
    except ValueError:
        ts = datetime.now(UTC)
    marker = seq if isinstance(seq, int) else (sim_tick if isinstance(sim_tick, int) else 0)
    obs_id = guardian_observation_id(run, sid, seq if isinstance(seq, int) else None,
                                     sim_tick if isinstance(sim_tick, int) else None)
    return SensorReading(
        sensor_id=sid,
        type=stype,
        value=value,
        unit=row.get("unit"),
        ts=ts,
        seq=max(0, int(marker)),
        sig=obs_id,                               # Twin readings are unsigned; the observation id is the stable reference
        severity_class=severity_for(sid, twin_type, value),
        correlation_id=f"{run or 'run'}:{sid}:{marker}",
        zone=str(row.get("zone_id") or (catalog.sensors()[sid].zone_id if sid in catalog.sensors() else "UNKNOWN")),
        run_id=run,
        sim_tick=sim_tick if isinstance(sim_tick, int) else None,
        twin_type=twin_type,
        status=str(row.get("status") or "") or None,
        available=row.get("available") if isinstance(row.get("available"), bool) else None,
        backup_ids=[b for b in row.get("backup_ids", []) if isinstance(b, str)],
        linked_device_ids=[d for d in row.get("linked_device_ids", []) if isinstance(d, str)],
        observation_id=obs_id,
    )
