"""Built-in Guardian simulator (contract-faithful copy (aya883/guardian_layer interface v0.3/v0.4).

It is NOT the Guardian layer. It reproduces the Guardian's contract so the Brain can run
standalone:

  * polls the Twin's GET /api/v1/sensors with the guardian token, like the real
    Guardian's `GUARDIAN_PROFILE=twin`, skipping samples already judged
  * observation_id = f"{run_id[-12:]}-{sensor_id}-{seq}"  (src/guardian/adapters/twin.py)
  * Verdict JSON: verdict_id, observation_id, device_id, verdict TRUSTED|UNTRUSTED,
    trust_score, risk_level, reason_codes, recommended_action, issued_at
  * reason codes OUT_OF_RANGE, IMPLAUSIBLE_RATE, FROZEN_VALUE, BACKUP_DISAGREEMENT,
    SENSOR_FAILED, DEVICE_QUARANTINED; quarantine below trust 0.3, isolate at the Twin
  * GET /verdicts/{id} (404 OBSERVATION_UNKNOWN), POST /trust/verdict,
    GET /devices/{id}/trust (404 DEVICE_UNKNOWN), POST /observations, /timeline/events, /health
  * 503 during an injected outage (SCN-06): the Brain must treat it as UNTRUSTED

The checks are deliberately simple versions of the real engine's checks.
Run: uvicorn mocks.guardian_contract_mock:app --port 8320
"""

from __future__ import annotations

import asyncio
import itertools
import os
import statistics
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

from city_brain import catalog


def _twin_url() -> str:
    """Read at call time, so the Brain can mount this simulator and point it at its own Twin simulator."""
    return os.environ.get("SIM_TWIN_URL", os.environ.get("TWIN_URL", "http://127.0.0.1:8310")).rstrip("/")
TWIN_TOKEN = os.environ.get("TWIN_TOKEN_GUARDIAN", "token-guardian-secret-2026")
ADMIN = os.environ.get("GUARDIAN_TOKEN", "")
BRAIN = os.environ.get("GUARDIAN_TOKEN_BRAIN", "")
DEMO = not ADMIN                                     # like GUARDIAN_DEMO=1: no token configured -> open, local only
QUARANTINE_AT, RELEASE_AT = 0.3, 0.8
# Limits for scenario-local fixtures that are not in the 16-sensor inventory (SCN-05 RES-AIR-TEST-01)
FIXTURE_LIMITS = {"air_quality": ((0, 50), (0, 150))}


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class Engine:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.reset(None)
        self.outage_until = 0.0
        self.timeline: list[dict] = []
        self._tid = itertools.count(1)
        self.twin_status = {"last_ok_s_ago": None, "errors_in_a_row": 0}

    def reset(self, run_id) -> None:
        with getattr(self, "lock", threading.RLock()):
            self.run_id = run_id
            self.verdicts: dict[str, dict] = {}
            self.devices: dict[str, dict] = {}
            self.seen: dict[str, object] = {}
            self._vid = itertools.count(1)

    # ------------------------------------------------------------------ timeline
    def event(self, event: str, **fields) -> None:
        self.timeline.append({"id": next(self._tid), "timestamp": _now(), "layer": fields.pop("layer", "guardian"),
                              "event": event, **fields})
        self.timeline = self.timeline[-2000:]

    # ------------------------------------------------------------------ judging
    def _dev(self, did: str) -> dict:
        return self.devices.setdefault(did, {"device_id": did, "trust_score": 1.0, "quarantined": False,
                                             "history": [], "stale": False, "last_seen": None})

    def judge(self, obs_id: str, did: str, value: float, backup_ids: list[str], sensor_type: str = "",
              peers_now: dict[str, float] | None = None) -> dict:
        """`peers_now`: values sampled in the same poll, so a pair is compared like-for-like
        (never a fresh value against a backup value from before an outage)."""
        d = self._dev(did)
        peers_now = peers_now or {}
        spec = catalog.sensors().get(did)
        limits = (spec.normal_limits, spec.hard_limits, spec.type) if spec else None
        if limits is None and sensor_type in FIXTURE_LIMITS:
            limits = (*FIXTURE_LIMITS[sensor_type], sensor_type)
        reasons: list[str] = []
        if d["quarantined"]:
            reasons.append("DEVICE_QUARANTINED")
        elif limits is not None:
            (lo, hi), (hlo, hhi), stype = limits
            span = (hi - lo) or 1.0
            hist = d["history"]
            if not hlo <= value <= hhi:
                reasons.append("OUT_OF_RANGE")
            if hist and abs(value - hist[-1]) > 0.5 * (hhi - hlo):     # physically impossible step
                reasons.append("IMPLAUSIBLE_RATE")
            if stype in ("traffic_count", "temperature") and len(hist) >= 2 and hist[-1] == hist[-2] == value:
                reasons.append("FROZEN_VALUE")
            for b in backup_ids:
                bd = self.devices.get(b)
                if b not in peers_now or not bd or not bd["history"] or bd["quarantined"]:
                    continue                                  # only compare samples taken at the same moment
                other = peers_now[b]
                if abs(value - other) > 0.25 * span:
                    mine = abs(value - statistics.median(hist)) if hist else 0.0
                    theirs = abs(other - statistics.median(bd["history"]))
                    if mine >= theirs:
                        reasons.append("BACKUP_DISAGREEMENT")
        if reasons and "DEVICE_QUARANTINED" not in reasons:
            d["trust_score"] = round(max(0.0, d["trust_score"] - 0.7), 3)
        elif not reasons:
            d["trust_score"] = round(min(1.0, d["trust_score"] + 0.05), 3)
            d["history"] = (d["history"] + [value])[-6:]
        if reasons and "FROZEN_VALUE" in reasons:
            d["history"] = (d["history"] + [value])[-6:]
        d["last_seen"] = _now()
        verdict = self._verdict(obs_id, did, reasons, d)
        if not d["quarantined"] and d["trust_score"] < QUARANTINE_AT:
            self.quarantine(did, reasons[0] if reasons else "LOW_TRUST")
        return verdict

    def _verdict(self, obs_id: str, did: str, reasons: list[str], d: dict) -> dict:
        trusted = not reasons
        v = {"verdict_id": f"VRD-{next(self._vid):06d}", "observation_id": obs_id, "device_id": did,
             "verdict": "TRUSTED" if trusted else "UNTRUSTED", "trust_score": d["trust_score"],
             "risk_level": "LOW" if trusted else ("HIGH" if d["trust_score"] < QUARANTINE_AT else "MEDIUM"),
             "reason_codes": reasons,
             "recommended_action": "NONE" if trusted else ("QUARANTINE" if d["quarantined"] or d["trust_score"] < QUARANTINE_AT else "ISOLATE"),
             "issued_at": _now()}
        self.verdicts[obs_id] = v
        if len(self.verdicts) > 20000:
            for k in list(self.verdicts)[:5000]:
                self.verdicts.pop(k, None)
        if not trusted:
            self.event("verdict", observation_id=obs_id, device_id=did, verdict="UNTRUSTED",
                       trust_score=d["trust_score"], reason_codes=reasons)
        return v

    def failed(self, obs_id: str, did: str) -> None:
        d = self._dev(did)
        d["trust_score"] = 0.0
        self._verdict(obs_id, did, ["SENSOR_FAILED"], d)
        if not d["quarantined"]:
            self.quarantine(did, "SENSOR_FAILED")

    def quarantine(self, did: str, reason: str) -> None:
        d = self._dev(did)
        d["quarantined"] = True
        self.event("device_quarantined", device_id=did, reason=reason, trust_score=d["trust_score"])
        try:
            httpx.post(f"{_twin_url()}/api/v1/isolate", json={"device_id": did, "reason": reason},
                       headers={"Authorization": f"Bearer {TWIN_TOKEN}"}, timeout=3)
        except httpx.HTTPError:
            self.event("containment_failed", device_id=did)

    # ------------------------------------------------------------------ polling the Twin
    def poll(self) -> None:
        try:
            r = httpx.get(f"{_twin_url()}/api/v1/sensors", headers={"Authorization": f"Bearer {TWIN_TOKEN}"}, timeout=3)
            env = r.json()
        except (httpx.HTTPError, ValueError):
            self.twin_status["errors_in_a_row"] += 1
            return
        if not env.get("ok"):
            self.twin_status["errors_in_a_row"] += 1
            return
        self.twin_status.update(last_ok_s_ago=0, errors_in_a_row=0)
        with self.lock:
            if env.get("run_id") != self.run_id:
                if self.run_id is not None:
                    self.event("run_reset", old=self.run_id, new=env.get("run_id"))
                self.reset(env.get("run_id"))
            prefix = (self.run_id or "run")[-12:]
            rows = sorted([r for r in env["data"]["readings"] if isinstance(r, dict) and isinstance(r.get("sensor_id"), str)],
                          key=lambda r: (len(r.get("backup_ids", [])) == 0, r["sensor_id"]))
            peers_now = {r["sensor_id"]: float(r["reported_value"]) for r in rows
                         if isinstance(r.get("reported_value"), (int, float)) and not isinstance(r.get("reported_value"), bool) and r.get("available") is not False
                         and self.seen.get(r["sensor_id"]) != r.get("seq", r.get("sim_tick"))}
            import re as _re
            for row in rows:
                sid = row.get("sensor_id")
                v = row.get("reported_value")
                if not isinstance(sid, str) or not _re.match(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$", sid) \
                        or (v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)))):
                    continue                                   # malformed row: never reaches the engine (as the real Guardian)
                marker = row.get("seq", row.get("sim_tick"))
                if self.seen.get(sid) == marker:
                    continue
                self.seen[sid] = marker
                obs = f"{prefix}-{sid}-{marker}"
                status = str(row.get("status") or "").upper()
                if row.get("available") is False or status == "OFFLINE":
                    continue                                  # outage / already isolated: not judged
                if status in ("FAILED", "DEAD") or row.get("reported_value") is None:
                    self.failed(obs, sid)
                    continue
                self.judge(obs, sid, float(row["reported_value"]), row.get("backup_ids", []), str(row.get("type", "")),
                           peers_now)
                if sid not in catalog.sensors() and self.verdicts[obs]["verdict"] == "UNTRUSTED":
                    self.quarantine(sid, self.verdicts[obs]["reason_codes"][0])   # scenario fixture (SCN-05)


ENGINE = Engine()


def _auth(authorization: str | None, allowed: set[str]) -> None:
    if DEMO:
        return
    token = authorization.partition(" ")[2].strip() if authorization else ""
    roles = {ADMIN: "admin", BRAIN: "brain"} if BRAIN else {ADMIN: "admin"}
    if roles.get(token) not in allowed | {"admin"}:
        raise HTTPException(401, {"code": "UNAUTHORIZED", "message": "missing or invalid bearer token"})


def read_auth(authorization: str | None = Header(None)):
    _auth(authorization, {"brain"})


def admin_auth(authorization: str | None = Header(None)):
    _auth(authorization, set())


def _outage():
    if datetime.now().timestamp() < ENGINE.outage_until:
        raise HTTPException(503, {"code": "GUARDIAN_UNAVAILABLE", "message": "injected outage (SCN-06)"})


async def _loop():
    while True:
        await asyncio.sleep(1.0)
        if datetime.now().timestamp() >= ENGINE.outage_until:
            await asyncio.to_thread(ENGINE.poll)


@asynccontextmanager
async def lifespan(app):
    ENGINE.event("auth_disabled" if DEMO else "auth_enabled", note="guardian contract mock")
    task = asyncio.create_task(_loop())
    yield
    task.cancel()


app = FastAPI(title="Guardian contract mock", lifespan=lifespan)


@app.get("/health")
def health():
    _outage()
    return {"status": "ok", "devices": len(ENGINE.devices), "verdicts": len(ENGINE.verdicts), "twin": ENGINE.twin_status}


@app.get("/verdicts/{observation_id}", dependencies=[Depends(read_auth)])
def get_verdict(observation_id: str):
    _outage()
    v = ENGINE.verdicts.get(observation_id)
    if v is None:
        return JSONResponse(status_code=404, content={"error": {"code": "OBSERVATION_UNKNOWN", "message": observation_id}})
    return v


@app.post("/trust/verdict", dependencies=[Depends(read_auth)])
def trust_verdict(body: dict = Body(...)):
    return get_verdict(str(body.get("observation_id", "")))


@app.get("/devices/{device_id}/trust", dependencies=[Depends(read_auth)])
def device_trust(device_id: str):
    _outage()
    d = ENGINE.devices.get(device_id)
    if d is None:
        return JSONResponse(status_code=404, content={"detail": {"code": "DEVICE_UNKNOWN", "message": f"unknown device {device_id}"}})
    return {k: v for k, v in d.items() if k != "history"}


@app.post("/observations", dependencies=[Depends(admin_auth)])
def observations(body: dict = Body(...)):
    _outage()
    with ENGINE.lock:
        return ENGINE.judge(str(body["observation_id"]), str(body["device_id"]), float(body["value"]), body.get("backup_ids", []), str(body.get("sensor_type", "")))


@app.post("/devices/{device_id}/restore", dependencies=[Depends(admin_auth)])
def restore(device_id: str):
    d = ENGINE.devices.get(device_id)
    if d is None or not d["quarantined"]:
        raise HTTPException(409, {"code": "DEVICE_NOT_QUARANTINED", "message": device_id})
    d.update(quarantined=False, trust_score=RELEASE_AT, history=[])
    httpx.post(f"{_twin_url()}/api/v1/restore", json={"device_id": device_id},
               headers={"Authorization": f"Bearer {TWIN_TOKEN}"}, timeout=3)
    return {"device_id": device_id, "restored": True}


@app.post("/timeline/events", dependencies=[Depends(admin_auth)])
def post_event(body: dict = Body(...)):
    ENGINE.event(str(body.pop("event", "event")), **{k: v for k, v in body.items() if k not in ("id", "timestamp")})
    return {"id": ENGINE.timeline[-1]["id"]}


@app.get("/timeline/events", dependencies=[Depends(admin_auth)])
def get_events(since: int = 0):
    return [e for e in ENGINE.timeline if e["id"] > since][:500]


@app.get("/timeline", response_class=HTMLResponse)
def timeline_page():
    rows = "".join(f"<tr><td>{e['timestamp'][11:19]}</td><td>{e['layer']}</td><td>{e['event']}</td>"
                   f"<td>{e.get('device_id', '')}</td><td>{e.get('verdict', e.get('outcome', ''))}</td>"
                   f"<td>{', '.join(e.get('reason_codes', []) or e.get('actions', []))}</td></tr>"
                   for e in reversed(ENGINE.timeline[-200:]))
    return f"<meta http-equiv=refresh content=2><title>Guardian mock timeline</title><table border=1 cellpadding=4>{rows}</table>"


# ---------------------------------------------------------------- mock-only
@app.post("/mock/outage")
def mock_outage(on: bool = Query(True), seconds: float = Query(30.0)):
    ENGINE.outage_until = datetime.now().timestamp() + seconds if on else 0.0
    ENGINE.event("outage_injected" if on else "outage_cleared", seconds=seconds if on else 0)
    return {"outage": on, "seconds": seconds if on else 0}
