"""End to end: Brain + contract mocks, one test per Twin scenario (scenarios/*.json "Expected: Brain").

Starts the Twin mock, the Guardian mock and the Brain as real HTTP services on free ports,
with a fast simulation clock. Skip with SKIP_E2E=1. Takes about two minutes.
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("SKIP_E2E") == "1", reason="SKIP_E2E=1")
ROOT = Path(__file__).resolve().parents[1]


def _port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read())


def _post(url):
    req = urllib.request.Request(url, method="POST", data=b"")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


@pytest.fixture(scope="module")
def city():
    # One program: the Brain with its built-in Twin and Guardian simulators (SIMULATOR=true).
    bp = _port()
    env = {**os.environ, "PYTHONPATH": f"{ROOT / 'src'}{os.pathsep}{ROOT}", "MOCK_TICK_S": "0.2",
           "SIMULATOR": "true", "PORT": str(bp),
           "TWIN_POLL_INTERVAL_S": "0.2", "GUARDIAN_VERDICT_WAIT_S": "3", "GUARDIAN_TIMELINE_ENABLED": "true",
           "LOG_LEVEL": "WARNING", "DATABASE_URL": "postgresql://x:x@127.0.0.1:1/x"}
    uv = [sys.executable, "-m", "uvicorn", "--log-level", "warning", "--host", "127.0.0.1"]
    procs = [subprocess.Popen(uv + ["city_brain.main:app", "--port", str(bp)], cwd=ROOT, env=env)]
    base = f"http://127.0.0.1:{bp}"
    tp = f"{bp}/sim/twin"
    for _ in range(60):
        try:
            if _get(base + "/api/overview")["twin"].get("connected"):
                break
        except Exception:
            pass
        time.sleep(0.5)
    yield {"brain": base, "twin": f"http://127.0.0.1:{tp}"}
    for p in procs:
        p.terminate()
    for p in procs:
        p.wait(timeout=10)


def _run(city, sid, until, timeout=40):
    _post(city["brain"] + "/api/mock/scenario/reset")
    time.sleep(1.5)
    _post(city["brain"] + f"/api/mock/scenario/{sid}")
    start = time.time()
    seen = []
    while time.time() - start < timeout:
        ov = _get(city["brain"] + "/api/overview")
        seen = [d for d in ov["decisions"] if d["ts"] >= ov["decisions"][-1]["ts"]] if ov["decisions"] else []
        if until(ov):
            return ov
        time.sleep(0.5)
    raise AssertionError(f"{sid}: condition not met; last decisions: {json.dumps(seen[:5])[:1500]}")


def _applied(ov, device, command):
    return any(c["device_id"] == device and c["command"] == command and c["outcome"] == "APPLIED" for c in ov["commands"])


def test_t5_fake_flood_is_refused(city):
    ov = _run(city, "t5", lambda o: any(d["sensor_id"] == "PRT-WLV-01" and d["outcome"] == "DISCARDED" for d in o["decisions"]))
    time.sleep(3)
    ov = _get(city["brain"] + "/api/overview")
    assert not any(c["device_id"].startswith(("PRT-PMP", "V-12")) for c in ov["commands"])
    d = next(d for d in ov["decisions"] if d["sensor_id"] == "PRT-WLV-01" and d["outcome"] == "DISCARDED")
    assert d["verdict"] == "REJECTED" and "BACKUP_DISAGREEMENT" in d["reason_codes"]


def test_scn02_real_flood_starts_the_linked_pump(city):
    ov = _run(city, "scn-02", lambda o: _applied(o, "PRT-PMP-01", "start"))
    starts = [c for c in ov["commands"] if c["command"] == "start"]
    assert [c["device_id"] for c in starts] == ["PRT-PMP-01"]        # one response, not one per redundant sensor
    _run_recover = lambda o: _applied(o, "PRT-PMP-01", "stop")
    for _ in range(80):
        if _run_recover(_get(city["brain"] + "/api/overview")):
            break
        time.sleep(0.5)
    else:
        raise AssertionError("pump never stopped after the water went back to normal")


def test_scn03_ignores_bad_streams_and_uses_the_backup(city):
    ov = _run(city, "scn-03", lambda o: _applied(o, "IND-VLV-01", "set_opening"), timeout=60)
    outcomes = {(d["sensor_id"], d["outcome"]) for d in ov["decisions"]}
    assert ("IND-TMP-01", "BACKUP_USED") in outcomes
    assert any(d["sensor_id"] == "IND-TMP-02" and d["incident"] == "overheat" and d["outcome"] == "DISPATCHED" for d in ov["decisions"])


def test_scn04_restricted_pump_falls_back_to_backup(city):
    ov = _run(city, "scn-04", lambda o: _applied(o, "PRT-PMP-02", "start"))
    assert any(c["device_id"] == "PRT-PMP-01" and c["error"] == "GUARDIAN_RESTRICTED" for c in ov["commands"])
    assert not _applied(ov, "PRT-PMP-01", "start")


def test_scn05_priority_corridor_and_spoof_suppressed(city):
    ov = _run(city, "scn-05", lambda o: _applied(o, "DTN-TLT-01", "set_priority_corridor")
              and any(d["sensor_id"] == "RES-AIR-TEST-01" and d["outcome"] == "DISCARDED" for d in o["decisions"]))
    assert not any(c["device_id"].startswith("RES-") for c in ov["commands"])


def test_scn06_outage_holds_without_fabricating(city):
    ov = _run(city, "scn-06", lambda o: any(d["outcome"] == "HELD" for d in o["decisions"]))
    held = [d for d in ov["decisions"] if d["outcome"] == "HELD"]
    assert all(not d["commands"] for d in held)


def test_emergency_call_routes_responders_on_the_road_graph(city):
    _post(city["brain"] + "/api/mock/scenario/reset")          # end any injected outage from SCN-06
    for _ in range(40):
        if _get(city["brain"] + "/api/overview")["run_id"]:
            break
        time.sleep(0.25)
    d = _post(city["brain"] + "/api/emergency?kind=accident&zone=IND")
    assert d["outcome"] == "DISPATCHED"
    routes = {c["device_id"]: c["params"]["route_road_ids"] for c in d["commands"]}
    assert routes == {"AMB-01": ["R-LOW", "R-IND"], "POL-01": ["R-LOW", "R-IND"]}
    rel = _post(city["brain"] + "/api/responders/release")["released"]
    assert {r["device_id"] for r in rel} == {"AMB-01", "POL-01"} and all(r["outcome"] == "APPLIED" for r in rel)
