"""Shared city catalog: the Twin layer's own sensor, actuator, road and map data.

Every file in this folder is copied unchanged from the Twin repository
(dhouhaichaouii/CS-TSYP-TWIN-LAYER) so both layers describe the same city:

  twin_sensors.json         <- src/city_live/data/sensors.json        (the 16 PDF sensors)
  twin_device_catalog.json  <- src/city_live/data/device_catalog.json (zones + actuators)
  twin_core_catalog.json    <- src/city_core/data/catalog.json        (roads, junctions, responders)
  map-layout.json           <- src/city_dashboard/web/map-layout.json (map anchors)
  city-map.svg              <- src/city_dashboard/web/city-map.svg    (map artwork)

Sensor map positions use the exact rule of the Twin dashboard (web/map.js,
CityMap.renderSensors), so a sensor appears at the same point on both maps.
`tests/test_shared_geography.py` checks this against a Twin checkout.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Twin zone codes -> map district anchors (web/map.js: zones={DTN:..., PRT:'PRT'})
ZONE_TO_MAP_ANCHOR = {"DTN": "Z-DOWNTOWN", "IND": "Z-INDUSTRIAL", "RES": "Z-RESIDENTIAL", "PRT": "PRT"}
# The port is drawn as a separate schematic anchor in the Twin map (web/map.js init()).
PORT_ANCHOR = {"id": "PRT", "x": 1320, "y": 850, "district": "PRT", "selectable": True}

ZONE_NAMES = {
    "PRT": "Port / Harbor",
    "DTN": "Downtown",
    "IND": "Industrial",
    "RES": "Residential",
    "HSP": "Hospital / Health",
}

# Where responders go for an incident in each zone (road-graph junction).
# The port is not on the road graph in the Twin core; J-D2 is the closest junction.
ZONE_TO_JUNCTION = {"DTN": "J-D1", "IND": "J-I1", "RES": "J-R1", "HSP": "J-H1", "PRT": "J-D2"}

# Redundant actuators the Brain may fall back to when one is quarantined (SCN-04).
ACTUATOR_BACKUPS = {
    "PRT-PMP-01": ["PRT-PMP-02"],
    "PRT-PMP-02": ["PRT-PMP-01"],
    "DTN-TLT-01": ["J-D1"],
    "DTN-TLT-02": ["J-D2"],
    "AMB-01": ["AMB-02"],
    "AMB-02": ["AMB-01"],
}


@dataclass(frozen=True)
class SensorSpec:
    sensor_id: str
    zone_id: str
    type: str
    unit: str
    description: str
    normal_limits: tuple[float, float]
    hard_limits: tuple[float, float]
    interval: int
    linked_device_ids: tuple[str, ...]
    backup_ids: tuple[str, ...]


@dataclass(frozen=True)
class ActuatorSpec:
    device_id: str
    zone_id: str
    family: str
    type: str
    commands: tuple[str, ...]
    supported: bool
    target_id: str | None
    aliases: tuple[str, ...] = field(default_factory=tuple)


def _load(name: str):
    return json.loads((HERE / name).read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def sensors() -> dict[str, SensorSpec]:
    out = {}
    for s in _load("twin_sensors.json")["sensors"]:
        out[s["sensor_id"]] = SensorSpec(
            sensor_id=s["sensor_id"], zone_id=s["zone_id"], type=s["type"], unit=s["unit"],
            description=s.get("description", ""),
            normal_limits=tuple(s["normal_limits"]), hard_limits=tuple(s["hard_limits"]),
            interval=int(s.get("interval", 5)),
            linked_device_ids=tuple(s.get("linked_device_ids", [])),
            backup_ids=tuple(s.get("backup_ids", [])),
        )
    return out


# Limits for scenario-local fixtures that are not in the 16-sensor inventory
# (the Twin's SCN-05 adds RES-AIR-TEST-01, "a test fixture, not a seeded hardware device").
FIXTURE_LIMITS = {"air_quality": ((0.0, 35.0), (0.0, 150.0), "ug/m3")}


def spec_for(sensor_id: str, twin_type: str | None = None) -> SensorSpec | None:
    """Catalog spec, or a conservative synthetic spec for a known fixture type."""
    spec = sensors().get(sensor_id)
    if spec is not None:
        return spec
    if twin_type in FIXTURE_LIMITS:
        normal, hard, unit = FIXTURE_LIMITS[twin_type]
        return SensorSpec(sensor_id=sensor_id, zone_id=sensor_id.split("-", 1)[0], type=twin_type, unit=unit,
                          description="scenario fixture (not in the Twin inventory)", normal_limits=normal,
                          hard_limits=hard, interval=5, linked_device_ids=(), backup_ids=())
    return None


@lru_cache(maxsize=1)
def actuators() -> dict[str, ActuatorSpec]:
    out = {}
    for a in _load("twin_device_catalog.json")["actuators"]:
        out[a["device_id"]] = ActuatorSpec(
            device_id=a["device_id"], zone_id=a["zone_id"], family=a["family"], type=a["type"],
            commands=tuple(a.get("commands", [])), supported=bool(a.get("supported", False)),
            target_id=a.get("target_id"), aliases=tuple(a.get("aliases", [])),
        )
    return out


@lru_cache(maxsize=1)
def zones() -> dict[str, str]:
    """Twin zone code -> core district id (PRT -> Z-RIVER, ...)."""
    return dict(_load("twin_device_catalog.json")["zones"])


@lru_cache(maxsize=1)
def road_graph() -> dict[str, tuple[str, str]]:
    """road_id -> (endpoint_a, endpoint_b), from the Twin core catalog."""
    ends: dict[str, dict[str, str]] = {}
    for r in _load("twin_core_catalog.json")["relationships"]:
        if r["relation"] in ("endpoint_a", "endpoint_b") and r["source_id"].startswith("R-"):
            ends.setdefault(r["source_id"], {})[r["relation"]] = r["target_id"]
    return {rid: (e["endpoint_a"], e["endpoint_b"]) for rid, e in ends.items() if len(e) == 2}


def responders() -> list[str]:
    return [a.device_id for a in actuators().values() if a.family == "responder" and a.supported]


def route(start: str, goal: str, blocked: set[str] | frozenset[str] = frozenset(), max_roads: int = 5) -> list[str] | None:
    """Shortest road list from junction `start` to `goal`, avoiding blocked roads.

    Returns [] when already there, None when unreachable. The Twin accepts at
    most 5 roads per dispatch (src/city_live/commands.py).
    """
    if start == goal:
        return []
    adj: dict[str, list[tuple[str, str]]] = {}
    for rid, (a, b) in road_graph().items():
        if rid in blocked:
            continue
        adj.setdefault(a, []).append((rid, b))
        adj.setdefault(b, []).append((rid, a))
    q: deque[tuple[str, list[str]]] = deque([(start, [])])
    seen = {start}
    while q:
        node, path = q.popleft()
        for rid, nxt in adj.get(node, []):
            if nxt in seen:
                continue
            p = path + [rid]
            if nxt == goal:
                return p if len(p) <= max_roads else None
            seen.add(nxt)
            q.append((nxt, p))
    return None


@lru_cache(maxsize=1)
def map_layout() -> dict:
    return _load("map-layout.json")


def sensor_positions(sensor_ids: list[str] | None = None) -> dict[str, dict]:
    """Same placement as the Twin dashboard (web/map.js renderSensors).

    For each active sensor in the Twin frame order (alphabetical sensor_id), take the
    anchor of its zone and offset it by its index inside that zone:
        x = anchor.x + 20 + (index % 2) * 18
        y = anchor.y - 22 - floor(index / 2) * 18
    """
    layout = map_layout()
    anchors = {a["id"]: a for a in layout["anchors"]}
    anchors["PRT"] = PORT_ANCHOR
    ids = sorted(sensor_ids if sensor_ids is not None else sensors().keys())
    counts: dict[str, int] = {}
    out: dict[str, dict] = {}
    for sid in ids:
        spec = sensors().get(sid)
        zone = spec.zone_id if spec else sid.split("-", 1)[0]
        anchor = anchors.get(ZONE_TO_MAP_ANCHOR.get(zone, ""))
        if anchor is None:
            continue
        index = counts.get(zone, 0)
        counts[zone] = index + 1
        out[sid] = {
            "x": anchor["x"] + 20 + (index % 2) * 18,
            "y": anchor["y"] - 22 - (index // 2) * 18,
            "zone_id": zone,
            "anchor": anchor["id"],
        }
    return out


# PM2.5 monitors of the Brain's built-in simulator, one per Twin core air zone, drawn at the
# zone's map anchor (the Twin dashboard draws its air overlay on the same Z-* anchors).
AIR_MONITORS = {"AIR-DTN-01": "Z-DOWNTOWN", "AIR-HSP-01": "Z-HEALTH", "AIR-IND-01": "Z-INDUSTRIAL",
                "AIR-PRT-01": "Z-RIVER", "AIR-RES-01": "Z-RESIDENTIAL"}

# Six showcase actuators the Brain drives, spread across the city. Positions are in the Twin map
# frame: at the device's own Twin anchor (junction / feeder), or beside its zone's main asset
# when several devices share one physical target (both port pumps act on P-03).
SHOWCASE_ACTUATORS = {
    "PRT-PMP-01": {"x": 1215, "y": 790, "label": "Port pump 1", "icon": "pump"},
    "PRT-PMP-02": {"x": 1330, "y": 925, "label": "Port pump 2", "icon": "pump"},
    "IND-VLV-01": {"x": 1290, "y": 225, "label": "Coolant valve", "icon": "valve"},
    "F-IND":      {"x": 1345, "y": 625, "label": "Industrial feeder", "icon": "feeder"},
    "DTN-TLT-01": {"x": 610, "y": 750, "label": "Signal J-D1", "icon": "signal"},
    "DTN-TLT-02": {"x": 1040, "y": 720, "label": "Signal J-D2", "icon": "signal"},
}


def geography() -> dict:
    """Everything the Brain map needs, in the Twin's coordinate frame."""
    layout = map_layout()
    anchors = list(layout["anchors"]) + [PORT_ANCHOR]
    acts = {}
    by_id = {a["id"]: a for a in anchors}
    for a in actuators().values():
        anchor = by_id.get(a.target_id or "") or by_id.get(a.device_id)
        if anchor is None and a.zone_id in ZONE_TO_MAP_ANCHOR:
            anchor = by_id.get(ZONE_TO_MAP_ANCHOR[a.zone_id])
        if anchor is None and a.zone_id == "HSP":
            anchor = by_id.get("Z-HEALTH")
        if anchor is not None:
            acts[a.device_id] = {"x": anchor["x"], "y": anchor["y"], "family": a.family,
                                 "zone_id": a.zone_id, "supported": a.supported}
    return {
        "source": "Twin layer map-layout.json + web/map.js sensor rule (copied, not re-drawn)",
        "viewBox": layout["viewBox"],
        "districts": layout["districts"],
        "roads": layout["roads"],
        "anchors": anchors,
        "sensors": sensor_positions(),
        "actuators": acts,
        "showcase_actuators": SHOWCASE_ACTUATORS,
        "air_monitors": {sid: {"x": by_id[a]["x"], "y": by_id[a]["y"], "anchor": a} for sid, a in AIR_MONITORS.items()},
        "zone_names": ZONE_NAMES,
    }
