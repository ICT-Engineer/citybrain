"""Routes for the merge-ready City Brain: map dashboard, Twin/Guardian status, controls."""

from __future__ import annotations

from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from city_brain import catalog
from city_brain.clients.twin_contract import NETWORK
from city_brain.config import settings
from city_brain.observability.metrics import METRICS

router = APIRouter()
STATIC = Path(__file__).resolve().parent.parent / "static"
CATALOG_DIR = Path(catalog.__file__).resolve().parent
_pipeline = None          # set by main.py at startup
_poller = None


def bind(pipeline, poller) -> None:
    global _pipeline, _poller
    _pipeline, _poller = pipeline, poller


def _p():
    if _pipeline is None:
        raise HTTPException(503, "pipeline not started")
    return _pipeline


@router.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/city")


@router.get("/city", response_class=HTMLResponse)
async def city_dashboard():
    return HTMLResponse((STATIC / "cortex.html").read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


@router.get("/city/classic", response_class=HTMLResponse)
async def city_dashboard_classic():
    return HTMLResponse((STATIC / "twin_dashboard.html").read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store"})


@router.get("/map/city-map.svg", include_in_schema=False)
async def map_svg():
    return FileResponse(CATALOG_DIR / "city-map.svg", media_type="image/svg+xml")


@router.get("/api/geography")
async def geography():
    return catalog.geography()


@router.get("/api/contract")
async def contract():
    return {
        "twin": {"url": settings.twin_url, "transport": settings.twin_transport,
                 "mcp_url": settings.twin_mcp_url if settings.twin_transport == "mcp" else None,
                 "contract": "twin-interface-0.1", "token_set": bool(settings.twin_token_brain)},
        "guardian": {"url": settings.guardian_url, "mode": settings.guardian_mode,
                     "contract": "guardian interface v0.3/v0.4", "token_set": bool(settings.guardian_token_brain)},
        "mock_mode": settings.mock_mode,
        "standalone_simulator": settings.simulator,
        "connected_to_other_layers": not settings.simulator and not settings.mock_mode,
        "catalog": {"sensors": len(catalog.sensors()), "actuators": len(catalog.actuators()),
                    "roads": len(catalog.road_graph()), "source": "copied from the Twin repository"},
    }


@router.get("/api/overview")
async def overview():
    p = _p()
    st = p.state
    pos = catalog.sensor_positions()
    sensors = []
    for sid, spec in sorted(catalog.sensors().items()):
        row = st.readings.get(sid, {})
        sensors.append({
            "sensor_id": sid, "zone_id": spec.zone_id, "type": spec.type, "unit": spec.unit,
            "description": spec.description, "normal_limits": spec.normal_limits,
            "value": row.get("reported_value"), "status": row.get("status"), "available": row.get("available"),
            "seq": row.get("seq"), "sim_tick": row.get("sim_tick"),
            "position": pos.get(sid), "trust": st.trust.get(sid), "verdict": st.verdicts.get(sid),
            "incident": (p.detector.active().get(sid).kind.value if p.detector.active().get(sid) else None),
        })
    extra = [r for sid, r in st.readings.items() if sid not in catalog.sensors() and sid not in catalog.AIR_MONITORS]
    air = []
    from city_brain.decision.air_model import get_air_model
    plant = st.actuator_state.get("F-IND", {}).get("command") != "open"
    for sid, anchor in catalog.AIR_MONITORS.items():
        row = st.readings.get(sid, {})
        v = row.get("reported_value")
        zone = row.get("zone_id") or sid.split("-")[1]
        m = p.detector.mem.get(sid)
        pred = (get_air_model().predict(float(v), (m.values[:-1] if m else []), zone, plant, 12.0)
                if isinstance(v, (int, float)) else None)
        air.append({"sensor_id": sid, "zone_id": zone, "anchor": anchor, "value": v, "status": row.get("status"),
                    "trust": st.trust.get(sid), "prediction": pred,
                    "incident": (p.detector.active().get(sid).kind.value if p.detector.active().get(sid) else None)})
    return {
        "run_id": st.run_id, "twin": st.twin_status, "guardian": st.guardian_status,
        "mock_mode": settings.mock_mode, "simulator": settings.simulator, "sensors": sensors, "fixtures": extra,
        "actuators": {d: {"family": a.family, "zone_id": a.zone_id, "supported": a.supported,
                          "last": st.actuator_state.get(d), "restricted": d in st.restricted,
                          "trust": st.trust.get(d)} for d, a in catalog.actuators().items()},
        "responders": {"location": p.book.location, "missions": p.book.missions},
        "blocked_roads": sorted(st.blocked_roads), "alerts": st.alerts,
        "decisions": list(st.decisions)[:40], "commands": list(st.commands)[:40],
        "metrics": METRICS.snapshot(), "air": air, "plant_powered": plant,
        "vault": {"states": _vault_states(p), "pending": p.vault.pending()[:10]},
        "shield": _shield_view(),
        "network": {"uplink_down": NETWORK["uplink_down"]},
        "input_rejections": st.input_rejections,
    }


def _vault_states(p) -> dict:
    out: dict[str, int] = {}
    for v in p.vault.items.values():
        out[v["state"]] = out.get(v["state"], 0) + 1
    return out


def _shield_view() -> dict:
    from city_brain.security.command_shield import RATE_LIMIT, CommandShield, get_shield
    sh = get_shield()
    return {"key_id": sh.key_id, "algorithm": "HMAC-SHA256 (method, path, timestamp, nonce, body SHA-256)",
            "rate_limit_per_device_per_min": RATE_LIMIT, "stats": sh.stats,
            "transport": CommandShield.transport(settings.twin_url)}


@router.get("/api/vault")
async def vault():
    """Offline vault and decision journal, with a full integrity check of both files."""
    return _p().vault.summary()


@router.post("/api/sim/network")
async def sim_network(uplink: str = Query("down")):
    """Simulate the link Brain -> Twin dropping ("down") or coming back ("up"). Simulator only."""
    if not settings.simulator:
        raise HTTPException(409, "network simulation is only available with the built-in simulator")
    NETWORK["uplink_down"] = uplink == "down"
    from city_brain.observability.timeline import TIMELINE
    TIMELINE.add("network", "brain", None, "uplink Brain -> Twin CUT (simulated)" if NETWORK["uplink_down"] else "uplink restored")
    from city_brain.observability.live_events import log_live_event
    log_live_event("Network", "uplink to the Twin CUT (simulated)" if NETWORK["uplink_down"] else "uplink restored", "held")
    return {"uplink_down": NETWORK["uplink_down"]}


@router.get("/timeline", response_class=HTMLResponse)
async def timeline_page():
    """R3: live timeline screen (reading -> Guardian verdict -> Brain decision -> actuator action)."""
    return HTMLResponse((STATIC / "timeline.html").read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


@router.get("/api/timeline")
async def timeline(since: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=2000)):
    from city_brain.observability.timeline import TIMELINE
    return {"events": TIMELINE.since(since, limit)}


@router.post("/api/security/selftest")
async def security_selftest():
    """SEC-01: prove the shield. Built-in simulator only (it plays the receiving Twin)."""
    if not settings.simulator:
        raise HTTPException(409, "self-test needs the built-in simulator")
    from city_brain.clients.twin_contract import request_id_for
    from city_brain.observability.timeline import TIMELINE
    from city_brain.security.command_shield import get_shield, sign
    p, sh, out = _p(), get_shield(), {}
    # 1) out-of-range parameter: blocked locally, never sent
    env = await p.twin.send_actuator_command("PRT-PMP-01", "start", {"speed_pct": 250}, "selftest-range", {})
    out["out_of_range"] = (env.get("error") or {}).get("code")
    # 2) unknown device: blocked locally
    env = await p.twin.send_actuator_command("PRT-PMP-99", "start", {"speed_pct": 50}, "selftest-unknown", {})
    out["unknown_device"] = (env.get("error") or {}).get("code")
    # 3) tampered body: signature made for one body, another body sent -> receiver refuses
    body = {"device_id": "DTN-TLT-02", "command": "set_mode", "params": {"mode": "normal"}, "request_id": "selftest-tamper"}
    path = httpx.URL(settings.twin_url).path.rstrip("/") + "/api/v1/commands"
    h = sign(sh.key, "POST", path, body)
    tampered = {**body, "params": {"mode": "stop"}}
    async with httpx.AsyncClient(timeout=5) as c:
        r = await c.post(settings.twin_url + "/api/v1/commands", json=tampered,
                         headers={**h, "Authorization": f"Bearer {settings.twin_token_brain}"})
        out["tampered_body"] = (r.status_code, (r.json().get("error") or {}).get("message"))
        # 4) replay: a valid signed request sent twice -> second refused
        good = {**body, "request_id": "selftest-replay-" + request_id_for("x", "DTN-TLT-02", "set_mode", {})}
        h2 = sign(sh.key, "POST", path, good)
        r1 = await c.post(settings.twin_url + "/api/v1/commands", json=good, headers={**h2, "Authorization": f"Bearer {settings.twin_token_brain}"})
        r2 = await c.post(settings.twin_url + "/api/v1/commands", json=good, headers={**h2, "Authorization": f"Bearer {settings.twin_token_brain}"})
        out["first_send"] = r1.status_code
        out["replayed"] = (r2.status_code, (r2.json().get("error") or {}).get("message"))
        # 5) unsigned command -> refused
        r3 = await c.post(settings.twin_url + "/api/v1/commands", json={**body, "request_id": "selftest-unsigned"},
                          headers={"Authorization": f"Bearer {settings.twin_token_brain}"})
        out["unsigned"] = (r3.status_code, (r3.json().get("error") or {}).get("message"))
    TIMELINE.add("action", "brain", "SEC-01", f"shield self-test: {out}")
    return out


@router.post("/api/emergency")
async def emergency(kind: str = Query("accident"), zone: str = Query("DTN")):
    if zone not in catalog.ZONE_TO_JUNCTION:
        raise HTTPException(422, f"zone must be one of {sorted(catalog.ZONE_TO_JUNCTION)}")
    d = await _p().emergency_call(kind, zone)
    return d.__dict__


@router.post("/api/responders/release")
async def release():
    return {"released": await _p().release_all()}


# ---------------------------------------------------------------- mock-only controls
MOCK_SCENARIOS = {"t5", "scn-02", "scn-03", "scn-04", "scn-05", "scn-06", "air-smog", "air-haze", "reset"}


@router.post("/api/mock/scenario/{sid}")
async def mock_scenario(sid: str):
    """Start one of the Twin's scenarios on the bundled Twin mock (never on a real Twin)."""
    if not settings.mock_mode:
        raise HTTPException(409, "scenario controls are disabled outside mock mode: start scenarios on the Twin itself")
    if sid not in MOCK_SCENARIOS:
        raise HTTPException(404, f"unknown scenario {sid}")
    async with httpx.AsyncClient(timeout=5) as c:
        r = await c.post(f"{settings.twin_url}/mock/scenarios/{sid}")
        if sid in ("scn-06", "reset"):
            try:
                await c.post(f"{settings.guardian_url}/mock/outage", params={"on": "true" if sid == "scn-06" else "false"})
            except httpx.HTTPError:
                pass
    if sid == "reset":
        p = _p()
        p.on_run(None)
    return r.json()
