"""Turn an incident into concrete Twin actuator commands (twin-interface-0.1).

The original Brain emitted invented actuators ("valve@north", "fire_brigade@south").
Here every command targets a real device in the Twin catalog with the exact command
names and parameters the Twin accepts (src/city_live/commands.py):

  pump      start {"speed_pct": 0-100} | stop {} | set_speed {"speed_pct"}
  valve     set_opening {"open_pct": 0-100}
  signal    set_priority_corridor {} | set_mode {"mode": normal|priority|stop} | set_phase {"phase": "all_red"}
  feeder    open {} | close {}
  responder dispatch {"destination_node_id", "route_road_ids" (<=5), "mission_ref"} | release {"mission_ref"}

The trained model still makes the call where it applies (flood / traffic / emergency
calls). Its feature `value_normalized` is fed in the model's own 0..1 scale, derived
from the Twin catalog limits, so "above the normal limit" lands on the model's own
decision boundary (water 0.7, traffic 0.8, as in scripts/train_model.py).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from city_brain import catalog
from city_brain.config import settings
from city_brain.decision.model import get_model
from city_brain.detection.rules import DetectedIncident, IncidentKind
from city_brain.schemas.twin import SensorReading, SensorType

log = logging.getLogger(__name__)

# Model action -> Brain plan family
MODEL_TO_PLAN = {
    "open_valve": "flood_response",
    "retime_traffic_signal": "priority_corridor",
    "dispatch_ambulance_police": "responders_accident",
    "dispatch_fire_ambulance": "responders_fire",
    "no_action": "none",
}
RULE_PLAN = {
    IncidentKind.FLOOD: "flood_response",
    IncidentKind.TRAFFIC_JAM: "priority_corridor",
    IncidentKind.ACCIDENT: "responders_accident",
    IncidentKind.FIRE: "responders_fire",
    IncidentKind.MEDICAL: "responders_medical",
    IncidentKind.OVERHEAT: "cooling_response",
    IncidentKind.EQUIPMENT_ANOMALY: "advisory",
    IncidentKind.AIR_POLLUTION: "air_response",
    IncidentKind.LOW_RESERVOIR: "advisory",
}
MODEL_BOUNDARY = {SensorType.WATER: 0.7, SensorType.TRAFFIC: 0.8}


@dataclass
class PlannedCommand:
    device_id: str
    command: str
    params: dict[str, Any]
    alternatives: list[str] = field(default_factory=list)   # backups tried if this one is quarantined
    why: str = ""


@dataclass
class TwinPlan:
    correlation_id: str
    plan: str
    commands: list[PlannedCommand] = field(default_factory=list)
    chosen_by: str = "rules"
    confidence: float = 1.0
    reason: str = ""
    notes: list[str] = field(default_factory=list)


def normalized_for_model(reading: SensorReading) -> float | None:
    """Twin value -> model scale: the catalog normal max maps to the model boundary."""
    spec = catalog.sensors().get(reading.sensor_id)
    if spec is None or not isinstance(reading.value, (int, float)):
        return None
    boundary = MODEL_BOUNDARY.get(reading.type)
    if boundary is None:
        return None
    lo, hi = spec.normal_limits
    span = (hi - lo) or 1.0
    return max(0.0, min(1.0, boundary * (float(reading.value) - lo) / span))


def model_opinion(reading: SensorReading | None, trend: float = 0.0) -> tuple[str, float] | None:
    if reading is None:
        return None
    if reading.type not in (SensorType.WATER, SensorType.TRAFFIC, SensorType.EMERGENCY_CALL):
        return None                                              # the model was never trained on these families
    update: dict[str, Any] = {}
    if reading.type != SensorType.EMERGENCY_CALL:
        norm = normalized_for_model(reading)
        if norm is None:
            return None
        update["value"] = norm
    return get_model().predict(reading.model_copy(update=update))


# --------------------------------------------------------------------------- builders
def _linked(incident: DetectedIncident, family: str) -> list[str]:
    ids = incident.evidence.get("linked_device_ids") or []
    if not ids and incident.sensor_id in catalog.sensors():
        ids = list(catalog.sensors()[incident.sensor_id].linked_device_ids)
    acts = catalog.actuators()
    return [d for d in ids if d in acts and acts[d].family == family]


def _flood(incident: DetectedIncident, plan: TwinPlan) -> None:
    for pump in _linked(incident, "pump"):
        plan.commands.append(PlannedCommand(pump, "start", {"speed_pct": 100},
                                            alternatives=catalog.ACTUATOR_BACKUPS.get(pump, []),
                                            why="discharge the rising water"))
    for valve in _linked(incident, "valve"):
        plan.commands.append(PlannedCommand(valve, "set_opening", {"open_pct": 100},
                                            why="open the drain / outlet valve fully"))
    if not plan.commands:
        plan.notes.append(f"no pump or valve linked to {incident.sensor_id} in the Twin catalog")


def _cooling(incident: DetectedIncident, plan: TwinPlan) -> None:
    for valve in _linked(incident, "valve"):
        plan.commands.append(PlannedCommand(valve, "set_opening", {"open_pct": 100},
                                            why="maximum coolant flow"))
    if not plan.commands:
        plan.notes.append(f"no coolant valve linked to {incident.sensor_id}")


def _corridor(incident: DetectedIncident, plan: TwinPlan) -> None:
    acts = catalog.actuators()
    signals = _linked(incident, "signal")
    if not signals:
        # fall back to any supported signal in the zone
        signals = [a.device_id for a in acts.values()
                   if a.family == "signal" and a.zone_id == incident.zone and a.supported and a.device_id.startswith(incident.zone)]
    for sig in signals:
        if not acts[sig].supported:
            plan.notes.append(f"{sig} is marked unsupported in the Twin catalog (target_id null): no command sent")
            continue
        plan.commands.append(PlannedCommand(sig, "set_priority_corridor", {},
                                            alternatives=catalog.ACTUATOR_BACKUPS.get(sig, []),
                                            why="give the congested approach a priority corridor"))


def _air(incident: DetectedIncident, plan: TwinPlan) -> None:
    """PM2.5 above the normal band: ask the air model (trained on the Twin's PM2.5 equation)."""
    from city_brain.decision.air_model import get_air_model
    ev = incident.evidence
    zone = incident.zone
    out = get_air_model().predict(float(ev["value"]), list(ev.get("history", [])), zone,
                                  bool(ev.get("plant_active", True)), float(ev.get("hour", 12.0)))
    ev["air_model"] = out
    plan.chosen_by, plan.confidence = ("model" if out["by"] == "air-model" else "rules"), out["confidence"]
    plan.reason = (f"air model: {out['action']} ({out['confidence']:.2f}), PM2.5 now {ev['value']:.0f}, "
                   f"forecast in 10 min {out['forecast_10min']:.0f} ug/m3")
    if out["action"] == "curtail_industry":
        if catalog.actuators().get("F-IND") and catalog.actuators()["F-IND"].supported:
            plan.commands.append(PlannedCommand("F-IND", "open", {},
                                                why="cut PLANT-01 power: removes the industrial PM2.5 source (hospital feeder F-ESS untouched)"))
            ev["air_action"] = "curtail_industry"
    else:
        plan.notes.append(f"public health advisory for {catalog.ZONE_NAMES.get(zone, zone)}: no actuator can lower this source")


def _restore(incident: DetectedIncident, plan: TwinPlan) -> None:
    """Return equipment to normal once the condition has cleared (closed loop)."""
    if incident.kind == IncidentKind.FLOOD:
        for pump in _linked(incident, "pump"):
            plan.commands.append(PlannedCommand(pump, "stop", {}, alternatives=catalog.ACTUATOR_BACKUPS.get(pump, []),
                                                why="water back to normal"))
    elif incident.kind == IncidentKind.TRAFFIC_JAM:
        for sig in _linked(incident, "signal"):
            if catalog.actuators()[sig].supported:
                plan.commands.append(PlannedCommand(sig, "set_mode", {"mode": "normal"}, why="traffic back to normal"))
    elif incident.kind == IncidentKind.AIR_POLLUTION and incident.evidence.get("air_action") == "curtail_industry":
        plan.commands.append(PlannedCommand("F-IND", "close", {}, why="air back to normal: restore PLANT-01 power"))
    elif incident.kind == IncidentKind.OVERHEAT:
        for valve in _linked(incident, "valve"):
            plan.commands.append(PlannedCommand(valve, "set_opening", {"open_pct": 50}, why="temperature back to normal"))


class ResponderBook:
    """Tracks which responder units the Brain has sent, and where they are."""

    def __init__(self) -> None:
        self.location: dict[str, str] = {u: "J-H1" for u in catalog.responders()}   # all start at the hospital junction
        self.missions: dict[str, str] = {}             # unit -> mission_ref
        self._n = 0

    def reset(self) -> None:
        self.__init__()

    def free(self, kind: str) -> list[str]:
        units = [u for u in catalog.responders() if u not in self.missions and u.startswith(kind)]
        return units

    def next_mission(self, unit: str) -> str:
        self._n += 1
        return f"m-brain-{unit.lower()}-{self._n:03d}"

    def plan_dispatch(self, unit: str, zone: str, blocked: set[str]) -> PlannedCommand | None:
        dest = catalog.ZONE_TO_JUNCTION.get(zone, "J-D1")
        path = catalog.route(self.location.get(unit, "J-H1"), dest, blocked)
        if path is None:
            return None
        return PlannedCommand(unit, "dispatch",
                              {"destination_node_id": dest, "route_road_ids": path, "mission_ref": self.next_mission(unit)},
                              why=f"send {unit} to {dest} via {'-'.join(path) or 'current position'}")

    def confirm(self, cmd: PlannedCommand) -> None:
        if cmd.command == "dispatch":
            self.missions[cmd.device_id] = cmd.params["mission_ref"]
            self.location[cmd.device_id] = cmd.params["destination_node_id"]
        elif cmd.command == "release":
            self.missions.pop(cmd.device_id, None)


def _responders(incident: DetectedIncident, plan: TwinPlan, book: ResponderBook, blocked: set[str], kinds: list[str]) -> None:
    for kind in kinds:
        free = book.free(kind)
        if not free:
            plan.notes.append(f"no free {kind} unit")
            continue
        cmd = book.plan_dispatch(free[0], incident.zone, blocked)
        if cmd is None:
            plan.notes.append(f"no open road to {incident.zone} for {free[0]} (blocked: {sorted(blocked)})")
            continue
        cmd.alternatives = [u for u in free[1:]]
        plan.commands.append(cmd)
    if incident.kind == IncidentKind.FIRE:
        plan.notes.append("the Twin catalog has no fire-brigade unit: ambulance + police are sent")


# --------------------------------------------------------------------------- entry points
def plan_for(incident: DetectedIncident, reading: SensorReading | None, book: ResponderBook,
             blocked: set[str], trend: float = 0.0) -> TwinPlan:
    rule_family = RULE_PLAN.get(incident.kind, "advisory")
    plan = TwinPlan(correlation_id=incident.correlation_id, plan=rule_family,
                    reason=f"rule: {incident.kind.value} on {incident.sensor_id}")

    opinion = model_opinion(reading, trend)
    if opinion is not None:
        action, conf = opinion
        fam = MODEL_TO_PLAN.get(action)
        if fam and conf >= settings.model_confidence_threshold:
            if fam == rule_family:
                plan.chosen_by, plan.confidence = "model", conf
                plan.reason = f"model predicted {action} ({conf:.2f}), agrees with the catalog rule"
            elif fam == "none":
                plan.notes.append(f"model said no_action ({conf:.2f}); catalog limit is exceeded, so the rule wins (safety)")
            else:
                plan.notes.append(f"model suggested {action} ({conf:.2f}); kept rule plan {rule_family}")

    if rule_family == "flood_response":
        _flood(incident, plan)
    elif rule_family == "cooling_response":
        _cooling(incident, plan)
    elif rule_family == "priority_corridor":
        _corridor(incident, plan)
    elif rule_family == "air_response":
        _air(incident, plan)
    elif rule_family == "responders_accident":
        _responders(incident, plan, book, blocked, ["AMB", "POL"])
    elif rule_family == "responders_fire":
        _responders(incident, plan, book, blocked, ["AMB", "POL"])
    elif rule_family == "responders_medical":
        _responders(incident, plan, book, blocked, ["AMB"])
    else:
        plan.notes.append(f"{incident.kind.value}: advisory only, no actuator in the Twin catalog for this condition")
    return plan


def recovery_plan(incident: DetectedIncident) -> TwinPlan:
    plan = TwinPlan(correlation_id=incident.correlation_id + ":recovery", plan="recovery",
                    reason=f"{incident.kind.value} on {incident.sensor_id} cleared")
    _restore(incident, plan)
    return plan
