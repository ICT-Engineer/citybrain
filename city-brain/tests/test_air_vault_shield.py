"""Air model, offline vault and command shield."""

import json
import time

import pytest

from city_brain.decision.air_model import get_air_model, label, step
from city_brain.offline import OfflineVault, SealedLog
from city_brain.security.command_shield import CommandShield, SignatureVerifier, sign


# ---------------------------------------------------------------- air
def test_air_step_matches_twin_equation():
    # Twin README: industrial steady state with the plant on is 30.2 ug/m3
    pm = 10.0
    for _ in range(3000):
        pm = step(pm, 0.0, "IND", True)
    assert abs(pm - 30.2) < 0.05


def test_air_model_loads_and_is_accurate():
    m = get_air_model()
    assert m.ready and m.metrics["accuracy"] >= 0.95


@pytest.mark.parametrize("pm,hist,zone,plant,expected", [
    (12, [11, 12, 12], "DTN", True, "no_action"),
    (45, [44, 45, 45], "DTN", True, "health_advisory"),          # downtown: no plant to cut
    (40, [30, 33, 36], "IND", True, "curtail_industry"),         # rising fast next to the plant
])
def test_air_model_decisions(pm, hist, zone, plant, expected):
    assert get_air_model().predict(pm, hist, zone, plant, 12.0)["action"] == expected


def test_air_label_never_curtails_without_a_plant():
    assert label(200, 300, "DTN", True) == "health_advisory"
    assert label(200, 300, "IND", False) == "health_advisory"


# ---------------------------------------------------------------- vault
def test_vault_survives_restart_and_detects_tampering(tmp_path):
    key = b"k" * 32
    v = OfflineVault(tmp_path, key)
    rec = v.save({"correlation_id": "c1", "incident": "flood"},
                 [{"device_id": "PRT-PMP-01", "command": "start", "params": {"speed_pct": 100}, "request_id": "r1"}], "PRT-WLV-01")
    v.update(rec["vault_id"], attempts=1)
    again = OfflineVault(tmp_path, key)                            # restart: state rebuilt from disk
    assert [p["vault_id"] for p in again.pending()] == [rec["vault_id"]]
    assert again.pending_log.verify()["ok"]
    path = again.pending_log.path
    lines = path.read_text().splitlines()
    tampered = json.loads(lines[0]); tampered["record"]["commands"][0]["params"]["speed_pct"] = 0
    path.write_text(json.dumps(tampered) + "\n" + "\n".join(lines[1:]) + "\n")
    assert not SealedLog(path, key).verify()["ok"]


# ---------------------------------------------------------------- shield
def test_shield_blocks_invalid_and_rate_limits():
    sh = CommandShield()
    assert sh.check("PRT-PMP-01", "start", {"speed_pct": 100}) is None
    assert sh.check("PRT-PMP-01", "start", {"speed_pct": 150}) == "PARAMETER_OUT_OF_RANGE"
    assert sh.check("RES-TLT-01", "set_priority_corridor", {}) == "UNSUPPORTED_DEVICE"
    assert sh.check("DTN-TLT-01", "explode", {}) == "COMMAND_NOT_ALLOWED"
    assert sh.check("AMB-01", "dispatch", {"destination_node_id": "J-D1", "route_road_ids": ["R-NOPE"]}) == "BAD_ROUTE"
    results = [sh.check("DTN-TLT-02", "set_mode", {"mode": "normal"}, request_id=f"r{i}") for i in range(8)]
    assert results.count("RATE_LIMITED") == 2
    assert sh.check("DTN-TLT-02", "set_mode", {"mode": "normal"}, request_id="r0") is None   # retry of r0 is free


def test_signature_round_trip_and_attacks():
    key = b"secret-key"
    body = {"device_id": "PRT-PMP-01", "command": "start", "params": {"speed_pct": 100}, "request_id": "r1"}
    h = sign(key, "POST", "/api/v1/commands", body)
    v = SignatureVerifier(key)
    assert v.verify("POST", "/api/v1/commands", body, h) == (True, "OK")
    assert v.verify("POST", "/api/v1/commands", body, h)[1] == "NONCE_REPLAYED"
    h2 = sign(key, "POST", "/api/v1/commands", body)
    assert v.verify("POST", "/api/v1/commands", {**body, "params": {"speed_pct": 0}}, h2)[1] == "BODY_TAMPERED"
    assert SignatureVerifier(b"other").verify("POST", "/api/v1/commands", body, sign(key, "POST", "/api/v1/commands", body))[1] == "SIGNATURE_INVALID"
    old = sign(key, "POST", "/api/v1/commands", body, ts=int(time.time()) - 120)
    assert v.verify("POST", "/api/v1/commands", body, old)[1] == "SIGNATURE_EXPIRED"
