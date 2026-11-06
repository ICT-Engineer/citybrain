#!/usr/bin/env python3
"""Run one Test Book scenario (R2): one command, one log file in logs/.

    python scenarios/run.py --scenario t5            # one scenario
    python scenarios/run.py --all                    # every City Brain scenario
    python scenarios/run.py --list
    python scenarios/run.py --scenario scn-02 --speed 1.0 --keep   # real-time, leave the Brain running

Each run starts the City Brain ALONE (with its built-in Twin and Guardian simulators,
R1), triggers the scenario, records the R3 timeline (reading -> verdict -> decision -> action),
checks the expected outputs written in TESTBOOK.md and writes logs/<id>-<UTC time>.log ending
with RESULT PASS or RESULT FAIL. Exit code 0 = all checks passed.

Nothing here talks to the real Twin or Guardian layers.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRAIN = ROOT
LOGS = ROOT / "logs"


# ------------------------------------------------------------------ helpers
def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Brain:
    def __init__(self, speed: float, port: int | None = None) -> None:
        self.port = port or _port()
        self.base = f"http://127.0.0.1:{self.port}"
        py = BRAIN / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        self.python = str(py) if py.exists() else sys.executable
        self.data = LOGS / ".runs" / f"{self.port}"
        env = {**os.environ, "PYTHONPATH": f"{BRAIN / 'src'}{os.pathsep}{BRAIN}", "SIMULATOR": "true", "PORT": str(self.port),
               "MOCK_TICK_S": str(speed), "TWIN_POLL_INTERVAL_S": str(min(1.0, speed)), "LOG_LEVEL": "WARNING",
               "GUARDIAN_VERDICT_WAIT_S": "4", "BRAIN_DATA_DIR": str(self.data), "OFFLINE_DECISION_TTL_S": "300",
               "DATABASE_URL": "postgresql://none:none@127.0.0.1:1/none"}
        self.proc = subprocess.Popen([self.python, "-m", "uvicorn", "city_brain.main:app", "--host", "127.0.0.1",
                                      "--port", str(self.port), "--log-level", "warning"], cwd=BRAIN, env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(80):
            try:
                if self.get("/api/overview")["twin"].get("connected") and len([s for s in self.get("/api/overview")["sensors"] if s["value"] is not None]) == 16:
                    return
            except Exception:
                pass
            time.sleep(0.25)
        raise RuntimeError("City Brain did not start (is .venv installed? run SETUP_AND_RUN.cmd once)")

    def get(self, path: str):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return json.loads(r.read())

    def post(self, path: str):
        req = urllib.request.Request(self.base + path, method="POST", data=b"")
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def applied(ov, device, command=None, why=None):
    return [c for c in ov["commands"] if c["device_id"] == device and c["outcome"] == "APPLIED"
            and (command is None or c["command"] == command) and (why is None or why in (c.get("why") or ""))]


def decisions(ov, sensor=None, outcome=None, incident=None):
    return [d for d in ov["decisions"] if (sensor is None or d["sensor_id"] == sensor)
            and (outcome is None or d["outcome"] == outcome) and (incident is None or d["incident"] == incident)]


def wait(brain: Brain, cond, timeout: float):
    end = time.time() + timeout
    ov = brain.get("/api/overview")
    while time.time() < end:
        ov = brain.get("/api/overview")
        if cond(ov):
            return ov, True
        time.sleep(0.5)
    return ov, False


# ------------------------------------------------------------------ scenarios (see TESTBOOK.md)
def s_calm(b, speed):
    time.sleep(60 * speed)
    ov = b.get("/api/overview")
    return ov, [("all 16 Twin sensors and 5 air monitors reporting", sum(s["value"] is not None for s in ov["sensors"]) == 16
                 and sum(a["value"] is not None for a in ov["air"]) == 5),
                ("no incident on a calm day", ov["metrics"]["incidents_detected_total"] == 0),
                ("no actuator command sent", not ov["commands"])]


def s_t5(b, speed):
    b.post("/api/mock/scenario/t5")
    ov, _ = wait(b, lambda o: decisions(o, "PRT-WLV-01", "DISCARDED"), 40 * speed + 10)
    ov, _ = wait(b, lambda o: decisions(o, "PRT-WLV-01", "BACKUP_USED"), 15 * speed + 8)
    d = (decisions(ov, "PRT-WLV-01", "DISCARDED") or [{}])[-1]
    return ov, [("fake 180 cm reading refused (UNTRUSTED from Guardian)", d.get("verdict") == "REJECTED"),
                ("reason code recorded", bool(d.get("reason_codes"))),
                ("no pump or valve command sent", not [c for c in ov["commands"] if c["device_id"].startswith(("PRT-PMP", "V-12"))]),
                ("switches to backup PRT-WLV-02 once PRT-WLV-01 is quarantined", bool(decisions(ov, "PRT-WLV-01", "BACKUP_USED")))]


def s_scn02(b, speed):
    b.post("/api/mock/scenario/scn-02")
    ov, ok = wait(b, lambda o: applied(o, "PRT-PMP-01", "start"), 60 * speed + 15)
    ov2, ok2 = wait(b, lambda o: applied(o, "PRT-PMP-01", "stop"), 160 * speed + 20)
    starts = [c for c in ov2["commands"] if c["command"] == "start" and c["outcome"] == "APPLIED"]
    d = (decisions(ov2, incident="flood", outcome="DISPATCHED") or [{}])[-1]
    return ov2, [("TRUSTED flood verdict", d.get("verdict") == "TRUSTED"),
                 ("START sent to the linked pump PRT-PMP-01", ok),
                 ("exactly one pump started (redundant sensor corroborates, no double response)", len(starts) == 1),
                 ("pump stopped when the water was back to normal", ok2)]


def s_scn03(b, speed):
    b.post("/api/mock/scenario/scn-03")
    ov, ok = wait(b, lambda o: applied(o, "IND-VLV-01", "set_opening"), 90 * speed + 20)
    return ov, [("dead IND-TMP-01 stream ignored, backup IND-TMP-02 used", bool(decisions(ov, "IND-TMP-01", "BACKUP_USED"))),
                ("coolant valve IND-VLV-01 opened on the backup's TRUSTED reading",
                 ok and bool(decisions(ov, "IND-TMP-02", "DISPATCHED", "overheat"))),
                ("frozen DTN-TRF-01 stream ignored after quarantine", bool(decisions(ov, "DTN-TRF-01", "IGNORED")))]


def s_scn04(b, speed):
    b.post("/api/mock/scenario/scn-04")
    ov, ok = wait(b, lambda o: applied(o, "PRT-PMP-02", "start"), 60 * speed + 15)
    return ov, [("Twin refused PRT-PMP-01 with GUARDIAN_RESTRICTED, rejection recorded",
                 any(c["device_id"] == "PRT-PMP-01" and c["error"] == "GUARDIAN_RESTRICTED" for c in ov["commands"])),
                ("START sent to the backup pump PRT-PMP-02", ok),
                ("PRT-PMP-01 never started", not applied(ov, "PRT-PMP-01", "start"))]


def s_scn05(b, speed):
    b.post("/api/mock/scenario/scn-05")
    ov, ok = wait(b, lambda o: applied(o, "DTN-TLT-01", "set_priority_corridor") and decisions(o, "RES-AIR-TEST-01", "DISCARDED"),
                  60 * speed + 15)
    return ov, [("priority corridor on DTN-TLT-01 for the real accident", bool(applied(ov, "DTN-TLT-01", "set_priority_corridor"))),
                ("spoofed air alarm RES-AIR-TEST-01 refused", bool(decisions(ov, "RES-AIR-TEST-01", "DISCARDED"))),
                ("no command for the spoofed alarm", not [c for c in ov["commands"] if c["device_id"] in ("F-IND", "RES-VLV-01")])]


def s_scn06(b, speed):
    b.post("/api/mock/scenario/scn-06")
    ov, ok = wait(b, lambda o: decisions(o, outcome="HELD"), 30 * speed + 15)
    held = decisions(ov, outcome="HELD")
    ov2, ok2 = wait(b, lambda o: applied(o, "PRT-PMP-01", "start"), 75)
    return ov2, [("decision HELD while Guardian is unreachable", ok),
                 ("no command sent or queued while held", all(not d["commands"] for d in held)),
                 ("acts normally once Guardian is back", ok2)]


def s_air01(b, speed):
    b.post("/api/mock/scenario/air-smog")
    ov, ok = wait(b, lambda o: applied(o, "F-IND", "open"), 120 * speed + 20)
    d = (decisions(ov, "AIR-IND-01", "DISPATCHED") or [{}])[-1]
    return ov, [("PM2.5 rise in the industrial zone detected", bool(decisions(ov, "AIR-IND-01"))),
                ("air model chose curtail_industry", d.get("chosen_by") == "model" and "curtail_industry" in " ".join(d.get("notes", []))),
                ("industrial feeder F-IND opened (plant source removed)", ok),
                ("hospital feeder F-ESS never touched", not [c for c in ov["commands"] if c["device_id"] == "F-ESS"])]


def s_air02(b, speed):
    b.post("/api/mock/scenario/air-haze")
    ov, ok = wait(b, lambda o: decisions(o, "AIR-DTN-01", "ADVISORY"), 120 * speed + 20)
    return ov, [("downtown haze gets a health advisory", ok),
                ("no plant curtailment for a zone without the plant", not applied(ov, "F-IND"))]


def s_net01(b, speed):
    b.post("/api/sim/network?uplink=down")
    b.post("/api/mock/scenario/scn-02")
    ov, ok = wait(b, lambda o: decisions(o, outcome="SAVED_OFFLINE"), 60 * speed + 15)
    pend = ov["vault"]["states"].get("PENDING", 0)
    b.post("/api/sim/network?uplink=up")
    ov2, ok2 = wait(b, lambda o: applied(o, "PRT-PMP-01", "start", why="offline vault"), 30)
    v = b.get("/api/vault")
    return ov2, [("decision taken while the uplink is down is SAVED_OFFLINE", ok),
                 ("sealed in data/offline/pending_decisions.jsonl (PENDING)", pend >= 1),
                 ("delivered automatically with the same request_id when the link returns", ok2),
                 ("vault marks it SENT", ov2["vault"]["states"].get("SENT", 0) >= 1),
                 ("hash chain and seals intact", v["integrity"]["ok"] and v["journal_integrity"]["ok"])]


def s_sec01(b, speed):
    r = b.post("/api/security/selftest")
    ov = b.get("/api/overview")
    return ov, [("out-of-range parameter blocked before sending", r["out_of_range"] == "BLOCKED_BY_SHIELD:PARAMETER_OUT_OF_RANGE"),
                ("unknown device blocked before sending", r["unknown_device"] == "BLOCKED_BY_SHIELD:UNKNOWN_DEVICE"),
                ("tampered body refused by the receiver", r["tampered_body"][0] == 401 and "BODY_TAMPERED" in r["tampered_body"][1]),
                ("valid signed command accepted once", r["first_send"] == 200),
                ("replayed command refused", r["replayed"][0] == 401 and "NONCE_REPLAYED" in r["replayed"][1]),
                ("unsigned command refused", r["unsigned"][0] == 401 and "SIGNATURE_MISSING" in r["unsigned"][1])]


def s_inp01(b, speed):
    b.post("/sim/twin/mock/bad-readings")
    ov, ok = wait(b, lambda o: sum(o["input_rejections"].values()) >= 5, 10)
    rej = ov["input_rejections"]
    return ov, [("5 malformed Twin readings refused with a clear reason", ok),
                ("reasons recorded", {"BAD_VALUE", "UNKNOWN_TYPE", "BAD_SEQ", "BAD_SENSOR_ID", "BAD_TIMESTAMP"} <= set(rej)),
                ("Brain kept running and kept reading the 16 sensors", sum(s["value"] is not None for s in ov["sensors"]) == 16),
                ("no command caused by bad input", not ov["commands"])]


def s_emg01(b, speed):
    d = b.post("/api/emergency?kind=accident&zone=IND")
    routes = {c["device_id"]: c["params"].get("route_road_ids") for c in d["commands"]}
    rel = b.post("/api/responders/release")["released"]
    ov = b.get("/api/overview")
    return ov, [("accident in Industrial dispatches an ambulance and the police", d["outcome"] == "DISPATCHED"
                 and set(routes) == {"AMB-01", "POL-01"}),
                ("route follows the Twin road graph J-H1 -> R-LOW -> R-IND -> J-I1", all(r == ["R-LOW", "R-IND"] for r in routes.values())),
                ("units released at mission end", {r["device_id"] for r in rel} == {"AMB-01", "POL-01"})]


SCENARIOS = {
    "calm-01": ("Calm day: the Brain must not overreact", s_calm),
    "t5": ("Fake flood at the port", s_t5),
    "scn-02": ("Real flash flood: start the linked pump", s_scn02),
    "scn-03": ("Dead and frozen sensors: use the backup", s_scn03),
    "scn-04": ("Quarantined pump: use the backup pump", s_scn04),
    "scn-05": ("Real accident + spoofed air alarm", s_scn05),
    "scn-06": ("Guardian outage: hold, never fabricate", s_scn06),
    "air-01": ("Industrial smog: air model curtails the plant", s_air01),
    "air-02": ("Downtown haze: advisory only", s_air02),
    "net-01": ("Link lost after deciding: offline vault", s_net01),
    "sec-01": ("Command shield: tampering, replay, bad commands", s_sec01),
    "inp-01": ("Malformed partner input", s_inp01),
    "emg-01": ("Emergency call: route responders", s_emg01),
}


def run(sid: str, speed: float, keep: bool = False) -> bool:
    name, fn = SCENARIOS[sid]
    LOGS.mkdir(exist_ok=True)
    started = datetime.now(UTC)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    path = LOGS / f"{sid}-{stamp}.log"
    print(f"[{sid}] {name} ... ", end="", flush=True)
    b = Brain(speed)
    try:
        ov, checks = fn(b, speed)
        time.sleep(1.0)
        events = b.get("/api/timeline?since=0&limit=2000")["events"]
        vault = b.get("/api/vault")
    finally:
        if not keep:
            b.stop()
    ok = all(c for _, c in checks)
    with path.open("w", encoding="utf-8") as f:
        f.write(f"# scenario: {sid} - {name}\n# trigger: python scenarios/run.py --scenario {sid}\n")
        f.write("# layer: City Brain (standalone, built-in Twin and Guardian simulators: R1)\n")
        f.write(f"# started: {started.isoformat()}  sim speed: {speed}s per tick\n")
        f.write("# ---- R3 timeline (reading -> verdict -> decision -> action), one JSON event per line ----\n")
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
        f.write("# ---- decisions ----\n")
        for d in reversed(ov["decisions"]):
            f.write(json.dumps({k: d[k] for k in ("ts", "sensor_id", "incident", "path", "outcome", "verdict", "trust_score",
                                                    "reason_codes", "chosen_by", "confidence", "notes")}, ensure_ascii=False) + "\n")
        f.write(f"# ---- offline vault: {json.dumps(vault['states'])} integrity={vault['integrity']['ok']} ----\n")
        f.write("# ---- expected outputs (TESTBOOK.md) ----\n")
        for label, c in checks:
            f.write(f"# [{'PASS' if c else 'FAIL'}] {label}\n")
        f.write(f"# RESULT {'PASS' if ok else 'FAIL'}\n")
    print(("PASS" if ok else "FAIL") + f"  -> {path.relative_to(ROOT)}")
    for label, c in checks:
        if not c:
            print(f"      FAIL: {label}")
    if keep:
        print(f"      Brain still running: {b.base}/city  and  {b.base}/timeline")
    return ok


def main(default: str | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", default=default)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--speed", type=float, default=0.25, help="seconds per simulated tick (1.0 = real time)")
    ap.add_argument("--keep", action="store_true", help="leave the Brain running after the scenario")
    a = ap.parse_args()
    if a.list:
        for k, (n, _) in SCENARIOS.items():
            print(f"{k:8} {n}")
        return 0
    ids = list(SCENARIOS) if a.all else [a.scenario]
    if ids == [None] or any(i not in SCENARIOS for i in ids):
        ap.error(f"--scenario must be one of {', '.join(SCENARIOS)} (or --all)")
    results = [run(i, a.speed, a.keep and len(ids) == 1) for i in ids]
    if len(ids) > 1:
        print(f"\n{sum(results)}/{len(results)} scenarios passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
