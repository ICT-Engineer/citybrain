"""Built-in Twin simulator (contract-faithful copy (twin-interface-0.1), for running the Brain standalone.

It is NOT the Twin layer. It reproduces the Twin's partner contract so the Brain can be
developed and demonstrated without merging:

  * same envelope {ok, run_id, data, error, timestamp} and error codes
    (NOT_AUTHORIZED, GUARDIAN_RESTRICTED, DEVICE_NOT_FOUND, INVALID_PARAM,
     IDEMPOTENCY_CONFLICT, DEVICE_UNAVAILABLE, INVALID_STATE)
  * same role tokens (Twin local-dev tokens by default) and role x tool matrix
  * same 16 sensors, zones, limits, backups and links (shared catalog copied from the Twin)
  * same public reading shape (twin-reading-0.2-local, no ground truth)
  * same actuator command translation rules as src/city_live/commands.py
  * the Twin's own scenarios T5, SCN-02..SCN-06 (scenarios/*.json triggers)

A tiny closed-loop model makes the Brain's commands visible: a running harbour pump
lowers the water, an open coolant valve cools the process, a priority corridor clears
traffic. Run: uvicorn mocks.twin_contract_mock:app --port 8310
"""

from __future__ import annotations

import asyncio
import os
import random
import re
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from fastapi import Body, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse

from city_brain import catalog

TOKENS = {
    os.environ.get("TWIN_TOKEN_BRAIN", "token-brain-secret-2026"): "brain",
    os.environ.get("TWIN_TOKEN_GUARDIAN", "token-guardian-secret-2026"): "guardian",
    os.environ.get("TWIN_TOKEN_OPERATOR", "token-operator-secret-2026"): "operator",
    os.environ.get("TWIN_TOKEN_EVALUATOR", "token-evaluator-secret-2026"): "evaluator",
}
PERMS = {
    "get_sensor_readings": {"brain", "guardian", "operator", "evaluator"},
    "get_city_state": {"brain", "guardian", "operator", "evaluator"},
    "get_events_since": {"brain", "guardian", "operator", "evaluator"},
    "send_actuator_command": {"brain", "operator"},
    "isolate_device": {"guardian", "operator"},
    "restore_device": {"guardian", "operator"},
}
IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}")
BASELINE = {
    "PRT-WLV-01": 50.0, "PRT-WLV-02": 50.0, "PRT-FLW-01": 60.0, "PRT-PRS-01": 3.0,
    "IND-TMP-01": 65.0, "IND-TMP-02": 65.0, "IND-PRS-01": 4.0, "IND-FLW-01": 60.0,
    "DTN-TRF-01": 12.0, "DTN-TRF-02": 12.0, "DTN-WLV-01": 15.0, "DTN-TMP-01": 20.0,
    "RES-WLV-01": 200.0, "RES-PRS-01": 4.0, "RES-FLW-01": 25.0, "RES-TRF-01": 12.0,
}
# PM2.5 monitors, one per Twin core air zone (Twin README Phase08: Z-DOWNTOWN, Z-HEALTH, Z-INDUSTRIAL,
# Z-RESIDENTIAL, Z-RIVER). Not part of the Twin's 16-sensor inventory: simulated here only.
AIR_SENSORS = {"AIR-DTN-01": "DTN", "AIR-HSP-01": "HSP", "AIR-IND-01": "IND", "AIR-PRT-01": "PRT", "AIR-RES-01": "RES"}
NOISE = {"air_quality": 0.4, "traffic_count": 1.2, "temperature": 0.15, "water_level": 0.3, "flow": 0.5, "pressure": 0.03}


def _now(epoch: datetime, tick: int) -> str:
    return (epoch + timedelta(seconds=tick)).isoformat().replace("+00:00", "Z")


class MockCity:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.reset()

    def reset(self, run_id: str | None = None) -> None:
        with self.lock:
            self.run_n = getattr(self, "run_n", 0) + 1
            self.run_id = run_id or f"mock-twin-{self.run_n:03d}"
            self.epoch = datetime.now(UTC).replace(microsecond=0)
            self.tick = 0
            self.seq = 0
            self.rng = random.Random(1405 + self.run_n)
            self.truth = dict(BASELINE)
            self.target: dict[str, float] = {}           # slow ramps (real rain, real accident)
            self.faults: dict[str, dict] = {}             # sensor -> {"type": FALSE_READING|STUCK|DEAD, ...}
            self.quarantined: dict[str, str] = {}
            self.latest: dict[str, dict] = {}
            self.events: list[dict] = []
            self.actuators = {"pump": {"PRT-PMP-01": 0.0, "PRT-PMP-02": 0.0, "P-03": 0.0},
                              "valve": {"V-12": 33.3, "IND-VLV-01": 33.3, "DTN-VLV-01": 33.3, "RES-VLV-01": 33.3},
                              "signal": {"DTN-TLT-01": "normal", "DTN-TLT-02": "normal", "J-D1": "normal", "J-D2": "normal"},
                              "feeder": {"F-ESS": True, "F-URBAN": True, "F-IND": True}}
            self.units = {u: {"at": "J-H1", "mission": None, "route": [], "eta": 0, "dest": None} for u in catalog.responders()}
            self.commands: dict[str, tuple[str, dict]] = {}
            self.brain_outage_until = -1
            self.fixtures: dict[str, dict] = {}
            self.air = {"DTN": 10.0, "HSP": 10.0, "IND": 30.2, "PRT": 10.0, "RES": 15.05}   # Twin steady state
            self.air_source = {z: 0.0 for z in self.air}
            self.signatures = {"verified": 0, "rejected": 0, "last": None}
            self.scenario: dict | None = None
            self.blocked: set[str] = set()
            self._event("SCENARIO_LIFECYCLE", {"note": "run started", "run_id": self.run_id})

    # ------------------------------------------------------------------ helpers
    def _event(self, etype: str, payload: dict) -> None:
        self.events.append({"seq": len(self.events) + 1, "event_type": etype, "sim_tick": self.tick,
                            "timestamp": _now(self.epoch, self.tick), "payload": payload})
        self.events = self.events[-500:]

    def env(self, data=None, error=None) -> dict:
        return {"ok": error is None, "run_id": self.run_id, "data": data if error is None else None,
                "error": error, "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z")}

    def err(self, code: str, message: str) -> dict:
        return self.env(error={"code": code, "message": message})

    def pump_running(self) -> bool:
        return any(v > 0 and d not in self.quarantined for d, v in self.actuators["pump"].items())

    # ------------------------------------------------------------------ physics-lite
    def step(self) -> None:
        with self.lock:
            self.tick += 1
            t = self.truth
            for sid, goal in list(self.target.items()):
                cur = t[sid]
                rate = 2.5 if sid.startswith("PRT-WLV") else 4.0 if "TRF" in sid else 1.0
                t[sid] = min(goal, cur + rate) if goal > cur else max(goal, cur - rate)
                if abs(t[sid] - goal) < 1e-6:
                    self.target.pop(sid)
            # harbour: a running pump drains towards the base level
            if self.pump_running():
                for sid in ("PRT-WLV-01", "PRT-WLV-02"):
                    if sid not in self.target and t[sid] > BASELINE[sid]:
                        t[sid] = max(BASELINE[sid], t[sid] - 2.0)
                t["PRT-FLW-01"], t["PRT-PRS-01"] = 140.0, 4.6
            else:
                t["PRT-FLW-01"], t["PRT-PRS-01"] = 60.0, 3.0
            # process heat: an open coolant valve cools towards the base temperature
            if self.actuators["valve"]["IND-VLV-01"] >= 90:
                for sid in ("IND-TMP-01", "IND-TMP-02"):
                    if sid not in self.target and t[sid] > BASELINE[sid]:
                        t[sid] = max(BASELINE[sid], t[sid] - 1.0)
                t["IND-FLW-01"] = 110.0
            else:
                t["IND-FLW-01"] = 60.0
            # traffic: a priority corridor clears the congested approach
            for sid, sig in (("DTN-TRF-01", "DTN-TLT-01"), ("DTN-TRF-02", "DTN-TLT-02")):
                if self.actuators["signal"][sig] == "priority" and sid not in self.target and t[sid] > 20:
                    t[sid] = max(20.0, t[sid] - 5.0)
            # PM2.5: the Twin core equation, plant contribution only while F-IND is closed (PLANT-01 powered)
            from city_brain.decision.air_model import step as air_step
            plant = self.actuators["feeder"]["F-IND"] and "F-IND" not in self.quarantined
            for z in self.air:
                self.air[z] = air_step(self.air[z], self.air_source[z], z, plant)
            # responders move one road every 4 ticks
            for u, s in self.units.items():
                if s["route"] and s["eta"] > 0:
                    s["eta"] -= 1
                    if s["eta"] == 0:
                        road = s["route"].pop(0)
                        a, b = catalog.road_graph()[road]
                        s["at"] = b if s["at"] == a else a
                        s["eta"] = 4 if s["route"] else 0
                        if not s["route"]:
                            self._event("ACTION_COMPLETED", {"device_id": u, "arrived_at": s["at"], "mission_ref": s["mission"]})
            # scenario end
            if self.scenario and self.tick >= self.scenario["until"]:
                for sid in self.scenario.get("clear_faults", []):
                    self.faults.pop(sid, None)
                for z in self.scenario.get("air_reset", []):
                    self.air_source[z] = 0.0
                self._event("SCENARIO_LIFECYCLE", {"scenario_id": self.scenario["id"], "state": "COMPLETED"})
                self.scenario = None
            # sampling
            for sid, spec in catalog.sensors().items():
                if self.tick % spec.interval == 0 or sid not in self.latest:
                    self._sample(sid, spec.type, spec.unit, spec.zone_id, list(spec.backup_ids), list(spec.linked_device_ids))
            for sid, z in AIR_SENSORS.items():
                if self.tick % 5 == 0 or sid not in self.latest:
                    self._sample(sid, "air_quality", "ug/m3", z, [], ["F-IND"] if z in ("IND", "RES") else [],
                                 fixture={"actual": self.air[z], "fault": self.faults.get(sid)})
            for sid, fx in self.fixtures.items():
                if self.tick % 5 == 0 or sid not in self.latest:
                    self._sample(sid, fx["type"], fx["unit"], fx["zone_id"], [], [], fixture=fx)

    def _sample(self, sid, stype, unit, zone, backups, linked, fixture=None) -> None:
        self.seq += 1
        true = fixture["actual"] if fixture else self.truth[sid]
        reported: float | None = round(true + self.rng.gauss(0, NOISE.get(stype, 0.1)), 2)
        status, available = "OK", True
        f = (fixture or {}).get("fault") or self.faults.get(sid)
        if f:
            if f["type"] == "FALSE_READING":
                reported = float(f["fixed_value"])
            elif f["type"] == "STUCK":
                f.setdefault("held", self.latest.get(sid, {}).get("reported_value", reported))
                reported = f["held"]
            elif f["type"] == "DEAD":
                reported, status = None, "FAILED"
        if sid in self.quarantined:
            reported, status, available = None, "OFFLINE", False
        self.latest[sid] = {
            "schema_version": "twin-reading-0.2-local", "sensor_id": sid, "zone_id": zone, "type": stype, "unit": unit,
            "timestamp": _now(self.epoch, self.tick), "sim_tick": self.tick, "delivery_tick": self.tick,
            "reported_value": reported, "status": status, "available": available,
            "backup_ids": backups, "linked_device_ids": linked, "seq": self.seq, "run_id": self.run_id,
        }

    # ------------------------------------------------------------------ commands
    def command(self, role: str, device_id: str, command: str, params: dict, request_id: str, context: dict | None) -> dict:
        with self.lock:
            if role == "brain" and self.tick <= self.brain_outage_until:
                return self.err("INVALID_STATE", "Brain communication is unavailable in this controlled scenario.")
            key = (device_id, command, params, context or {})
            prev = self.commands.get(request_id)
            if prev is not None:
                if prev[0] != repr(key):
                    return self.err("IDEMPOTENCY_CONFLICT", "request_id reused with different command data")
                return self.env(prev[1])
            act = catalog.actuators().get(device_id)
            if act is None or not act.supported or act.target_id is None:
                return self.err("DEVICE_NOT_FOUND", f"Unknown or unsupported device '{device_id}'.")
            if device_id in self.quarantined:
                return self.err("GUARDIAN_RESTRICTED", "Device is isolated by the Guardian.")
            ok, code = self._apply(act, command.lower(), params)
            if not ok:
                return self.err(code, f"{command} rejected for {device_id}")
            data = {"request_id": request_id, "device_id": device_id, "command": command, "status": "APPLIED",
                    "effective_revision": self.tick, "effect_id": f"live-{len(self.commands) + 1:010d}",
                    "context": context or {}, "timestamp": _now(self.epoch, self.tick)}
            self.commands[request_id] = (repr(key), data)
            self._event("ACTION_COMPLETED", {"request_id": request_id, "device_id": device_id, "command": command,
                                             "params": params, "outcome": "APPLIED", "source": role})
            return self.env(data)

    def _apply(self, act, cmd: str, params: dict) -> tuple[bool, str]:
        def only(required=(), optional=()):
            keys = set(params)
            return set(required) <= keys and not (keys - set(required) - set(optional))
        fam, dev = act.family, act.device_id
        if fam == "pump":
            if cmd == "stop" and only():
                self.actuators["pump"][dev] = 0.0
                return True, ""
            if cmd in ("start", "set_speed") and only(("speed_pct",)) and isinstance(params["speed_pct"], (int, float)) \
                    and 0 <= params["speed_pct"] <= 100:
                self.actuators["pump"][dev] = float(params["speed_pct"])
                return True, ""
            return False, "INVALID_PARAM"
        if fam == "valve":
            if cmd == "set_opening" and only(("open_pct",)) and isinstance(params["open_pct"], (int, float)) \
                    and 0 <= params["open_pct"] <= 100:
                self.actuators["valve"][dev] = float(params["open_pct"])
                return True, ""
            return False, "INVALID_PARAM"
        if fam == "signal":
            if cmd == "set_priority_corridor" and only():
                mode = "priority"
            elif cmd == "set_mode" and only(("mode",)) and str(params["mode"]).lower() in ("normal", "priority", "stop"):
                mode = str(params["mode"]).lower()
            elif cmd == "set_phase" and only(("phase",)) and str(params["phase"]).lower() == "all_red":
                mode = "stop"
            else:
                return False, "INVALID_PARAM"
            self.actuators["signal"][dev] = mode
            return True, ""
        if fam == "feeder":
            if cmd in ("open", "close", "connect", "disconnect") and only():
                self.actuators["feeder"][dev] = cmd in ("close", "connect")
                return True, ""
            return False, "INVALID_PARAM"
        if fam == "responder":
            unit = self.units[dev]
            if cmd == "dispatch" and only(("destination_node_id", "route_road_ids"), ("mission_ref",)):
                route, dest = params["route_road_ids"], params["destination_node_id"]
                mission = params.get("mission_ref") or f"m-{dev.lower()}-01"
                if not isinstance(route, list) or len(route) > 5 or not all(isinstance(r, str) and IDENT.fullmatch(r) for r in route) \
                        or not IDENT.fullmatch(str(dest)) or not IDENT.fullmatch(mission):
                    return False, "INVALID_PARAM"
                if unit["mission"] is not None:
                    return False, "DEVICE_UNAVAILABLE"
                node = unit["at"]
                for road in route:                       # connected, usable path to the destination
                    ends = catalog.road_graph().get(road)
                    if ends is None or node not in ends or road in self.blocked:
                        return False, "INVALID_PARAM"
                    node = ends[1] if node == ends[0] else ends[0]
                if node != dest:
                    return False, "INVALID_PARAM"
                unit.update(mission=mission, route=list(route), eta=4 if route else 0, dest=dest)
                return True, ""
            if cmd == "release" and only(("mission_ref",)):
                if unit["mission"] != params["mission_ref"]:
                    return False, "INVALID_PARAM"
                unit.update(mission=None, route=[], eta=0, dest=None)
                return True, ""
            return False, "INVALID_PARAM"
        return False, "DEVICE_NOT_FOUND"

    # ------------------------------------------------------------------ scenarios (Twin scenarios/*.json)
    def run_scenario(self, sid: str) -> dict:
        with self.lock:
            if sid == "reset":
                self.reset()
                return {"scenario": "reset", "run_id": self.run_id}
            t = self.tick
            if sid == "t5":          # FALSE_READING 180 on PRT-WLV-01 while the real level stays normal
                self.faults["PRT-WLV-01"] = {"type": "FALSE_READING", "fixed_value": 180.0}
                self.scenario = {"id": "T5", "until": t + 40, "clear_faults": ["PRT-WLV-01"]}
            elif sid == "scn-02":    # real rain: both harbour sensors rise to 110 cm
                self.target.update({"PRT-WLV-01": 110.0, "PRT-WLV-02": 110.0})
                self.scenario = {"id": "SCN-02", "until": t + 60}
            elif sid == "scn-03":    # DTN-TRF-01 STUCK, IND-TMP-01 DEAD, process really overheats
                self.faults["DTN-TRF-01"] = {"type": "STUCK"}
                self.faults["IND-TMP-01"] = {"type": "DEAD"}
                self.target.update({"IND-TMP-01": 96.0, "IND-TMP-02": 96.0})
                self.scenario = {"id": "SCN-03", "until": t + 90, "clear_faults": ["DTN-TRF-01", "IND-TMP-01"]}
            elif sid == "scn-04":    # Guardian quarantines PRT-PMP-01, then a real surge needs a pump
                self.quarantined["PRT-PMP-01"] = "integrity_check_failed"
                self.target.update({"PRT-WLV-01": 110.0, "PRT-WLV-02": 110.0})
                self.scenario = {"id": "SCN-04", "until": t + 60}
            elif sid == "scn-05":    # real accident congestion + spoofed scenario-local air fixture
                self.target["DTN-TRF-01"] = 105.0
                self.fixtures["RES-AIR-TEST-01"] = {"type": "air_quality", "unit": "ug/m3", "zone_id": "RES", "actual": 18.0,
                                                    "fault": {"type": "FALSE_READING", "fixed_value": 180.0}}
                self.scenario = {"id": "SCN-05", "until": t + 60}
            elif sid == "air-smog":  # industrial smog: a strong external source over the industrial zone
                self.air_source["IND"] = 0.6
                self.scenario = {"id": "AIR-SMOG", "until": t + 240, "air_reset": ["IND"]}
            elif sid == "air-haze":  # traffic haze downtown: moderate source, no plant involved
                self.air_source["DTN"] = 0.45
                self.scenario = {"id": "AIR-HAZE", "until": t + 240, "air_reset": ["DTN"]}
            elif sid == "scn-06":    # inter-layer outage: Brain channel blocked for 30 s (Guardian mock goes down too)
                self.brain_outage_until = t + 30
                self.target.update({"PRT-WLV-01": 110.0, "PRT-WLV-02": 110.0})
                self.scenario = {"id": "SCN-06", "until": t + 30}
            else:
                return {"error": f"unknown scenario {sid}"}
            self._event("SCENARIO_LIFECYCLE", {"scenario_id": self.scenario["id"], "state": "RUNNING"})
            return {"scenario": self.scenario["id"], "started_at_tick": t, "run_id": self.run_id}


CITY = MockCity()


def _role(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    return TOKENS.get(token.strip()) if scheme.lower() == "bearer" else None


def _denied(role, tool):
    if role not in PERMS[tool]:
        code = 401 if role is None else 403
        return JSONResponse(status_code=code, content=CITY.err("NOT_AUTHORIZED", "A valid Bearer credential for this tool is required."))
    return None


async def _ticker():
    while True:
        await asyncio.sleep(float(os.environ.get("MOCK_TICK_S", "1.0")))
        CITY.step()


@asynccontextmanager
async def lifespan(app):
    CITY.step()
    task = asyncio.create_task(_ticker())
    yield
    task.cancel()


app = FastAPI(title="Twin contract mock (twin-interface-0.1)", lifespan=lifespan)


@app.get("/health/live")
def live():
    return {"status": "UP", "service": "twin-contract-mock"}


@app.get("/health/ready")
def ready():
    return CITY.env({"status": "READY", "service": "twin-contract-mock", "checks": {"livecity": {"status": "UP"}}})


@app.get("/api/v1/sensors")
def sensors(zone_id: str | None = None, sensor_id: str | None = None, sensor_type: str | None = None,
            authorization: str | None = Header(None)):
    if (d := _denied(_role(authorization), "get_sensor_readings")):
        return d
    with CITY.lock:
        rows = [r for r in CITY.latest.values()
                if (not zone_id or r["zone_id"] == zone_id) and (not sensor_id or r["sensor_id"] == sensor_id)
                and (not sensor_type or r["type"] == sensor_type)]
        return CITY.env({"sim_tick": CITY.tick, "count": len(rows), "readings": sorted(rows, key=lambda r: r["sensor_id"])})


@app.get("/api/v1/state")
def state(zone_id: str | None = None, authorization: str | None = Header(None)):
    if (d := _denied(_role(authorization), "get_city_state")):
        return d
    with CITY.lock:
        alerts = []
        if max(CITY.truth["PRT-WLV-01"], CITY.truth["PRT-WLV-02"]) > 80:
            alerts.append("HARBOR_SURGE_WARNING")
        alerts += [f"ROAD_BLOCKED_{r}" for r in sorted(CITY.blocked)]
        return CITY.env({"sim_tick": CITY.tick, "active_incidents": alerts,
                         "quarantined_devices_count": len(CITY.quarantined),
                         "zones": {z: {"name": n} for z, n in catalog.ZONE_NAMES.items()}})


@app.get("/api/v1/events")
def events(seq: int = 0, authorization: str | None = Header(None)):
    if (d := _denied(_role(authorization), "get_events_since")):
        return d
    with CITY.lock:
        ev = [e for e in CITY.events if e["seq"] > seq]
        return CITY.env({"cursor": CITY.events[-1]["seq"] if CITY.events else 0, "events": ev[:200]})


REQUIRE_SIGNATURE = os.environ.get("SIM_REQUIRE_SIGNATURE", "true").lower() == "true"
_VERIFIER = None


def _verifier():
    global _VERIFIER
    if _VERIFIER is None:
        from city_brain.security.command_shield import SignatureVerifier, load_key
        _VERIFIER = SignatureVerifier(load_key())
    return _VERIFIER


@app.post("/api/v1/commands")
def commands(request: Request, body: dict = Body(...), authorization: str | None = Header(None)):
    role = _role(authorization)
    if (d := _denied(role, "send_actuator_command")):
        return d
    if REQUIRE_SIGNATURE and role == "brain":      # how the Twin should check Brain commands (reference)
        ok, why = _verifier().verify("POST", request.url.path, body, request.headers)
        CITY.signatures["verified" if ok else "rejected"] += 1
        CITY.signatures["last"] = why
        if not ok:
            return JSONResponse(status_code=401, content=CITY.err("NOT_AUTHORIZED", f"command signature check failed: {why}"))
    if not body.get("device_id") or not body.get("command") or not body.get("request_id"):
        return JSONResponse(status_code=400, content=CITY.err("INVALID_PARAM", "device_id, command and request_id are required"))
    env = CITY.command(role, body["device_id"], body["command"], body.get("params") or {}, body["request_id"], body.get("context"))
    code = 200 if env["ok"] else {"GUARDIAN_RESTRICTED": 403, "DEVICE_NOT_FOUND": 404, "IDEMPOTENCY_CONFLICT": 409,
                                  "INVALID_STATE": 409}.get(env["error"]["code"], 400)
    return JSONResponse(status_code=code, content=env)


@app.post("/api/v1/isolate")
def isolate(body: dict = Body(...), authorization: str | None = Header(None)):
    if (d := _denied(_role(authorization), "isolate_device")):
        return d
    with CITY.lock:
        dev = body.get("device_id")
        if dev not in catalog.sensors() and dev not in catalog.actuators() and dev not in CITY.fixtures and dev not in AIR_SENSORS:
            return JSONResponse(status_code=404, content=CITY.err("DEVICE_NOT_FOUND", f"Unknown device '{dev}'."))
        CITY.quarantined[dev] = str(body.get("reason", ""))[:200]
        CITY._event("STATUS_CHANGED", {"device_id": dev, "status": "QUARANTINED", "reason": body.get("reason")})
        return CITY.env({"device_id": dev, "quarantined": True})


@app.post("/api/v1/restore")
def restore(body: dict = Body(...), authorization: str | None = Header(None)):
    if (d := _denied(_role(authorization), "restore_device")):
        return d
    with CITY.lock:
        dev = body.get("device_id")
        if dev not in CITY.quarantined:
            return JSONResponse(status_code=404, content=CITY.err("DEVICE_NOT_FOUND", f"'{dev}' is not isolated."))
        CITY.quarantined.pop(dev)
        CITY._event("STATUS_CHANGED", {"device_id": dev, "status": "RESTORED"})
        return CITY.env({"device_id": dev, "quarantined": False})


# ---------------------------------------------------------------- mock-only (not part of the Twin contract)
@app.post("/mock/scenarios/{sid}")
def mock_scenario(sid: str):
    return CITY.run_scenario(sid)


@app.post("/mock/bad-readings")
def mock_bad_readings():
    """Inject readings a partner should never send (INP-01): the Brain must refuse each with a clear reason."""
    with CITY.lock:
        CITY.seq += 1
        base = {"zone_id": "DTN", "unit": "cm", "timestamp": _now(CITY.epoch, CITY.tick), "sim_tick": CITY.tick,
                "status": "OK", "available": True, "backup_ids": [], "linked_device_ids": [], "run_id": CITY.run_id}
        CITY.latest["BAD-STRING-01"] = {**base, "sensor_id": "BAD-STRING-01", "type": "water_level", "reported_value": "12,5", "seq": CITY.seq}
        CITY.latest["BAD-TYPE-01"] = {**base, "sensor_id": "BAD-TYPE-01", "type": "plasma_level", "reported_value": 1.0, "seq": CITY.seq}
        CITY.latest["BAD-SEQ-01"] = {**base, "sensor_id": "BAD-SEQ-01", "type": "water_level", "reported_value": 5.0, "seq": -4}
        CITY.latest["bad id/../x"] = {**base, "sensor_id": "bad id/../x", "type": "water_level", "reported_value": 5.0, "seq": CITY.seq}
        CITY.latest["BAD-TS-01"] = {**base, "sensor_id": "BAD-TS-01", "type": "water_level", "reported_value": 5.0, "seq": CITY.seq,
                                    "timestamp": "yesterday at noon"}
        return {"injected": 5}


@app.get("/mock/truth")
def mock_truth():
    """Ground truth, for tests only. The real Twin never exposes this to the Brain."""
    with CITY.lock:
        return {"tick": CITY.tick, "truth": CITY.truth, "actuators": CITY.actuators, "units": CITY.units,
                "air": CITY.air, "air_source": CITY.air_source, "signatures": CITY.signatures,
                "quarantined": CITY.quarantined, "scenario": CITY.scenario}


@app.post("/mock/step")
def mock_step(n: int = Query(1, ge=1, le=600)):
    for _ in range(n):
        CITY.step()
    return {"tick": CITY.tick}
