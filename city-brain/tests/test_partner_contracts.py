"""Brain <-> partner wire formats, checked without running any partner.

Set TWIN_REPO to also validate every command the Brain can emit with the Twin's own
translation code (src/city_live/commands.py: build_command_effect).
"""

import os
import sys
from pathlib import Path

import pytest

from city_brain import catalog
from city_brain.adapters.twin_reading import from_twin, guardian_observation_id
from city_brain.decision.twin_actions import ResponderBook, plan_for, recovery_plan
from city_brain.detection.rules import DetectedIncident, IncidentKind
from city_brain.dispatch.trust_gate import TrustGate
from city_brain.schemas.guardian import GuardianVerdict, Verdict

TWIN_ROW = {
    "schema_version": "twin-reading-0.2-local", "sensor_id": "PRT-WLV-01", "zone_id": "PRT", "type": "water_level",
    "unit": "cm", "timestamp": "2026-10-18T00:15:35Z", "sim_tick": 935, "delivery_tick": 935, "reported_value": 110.0,
    "status": "OK", "available": True, "backup_ids": ["PRT-WLV-02"], "linked_device_ids": ["PRT-PMP-01"],
    "seq": 1880, "run_id": "twin-shared-001",
}


def test_twin_reading_is_mapped_without_loss():
    r = from_twin(TWIN_ROW)
    assert r.sensor_id == "PRT-WLV-01" and r.type == "water" and r.value == 110.0 and r.unit == "cm"
    assert r.twin_type == "water_level" and r.backup_ids == ["PRT-WLV-02"] and r.linked_device_ids == ["PRT-PMP-01"]
    assert r.severity_class == "critical"                    # 110 >= 1.25 x 80
    assert r.correlation_id == "twin-shared-001:PRT-WLV-01:1880"


def test_observation_id_matches_the_guardian_format():
    # aya883/guardian_layer adapters/twin.py: f"{run_id[-12:]}-{sensor_id}-{seq}"
    assert guardian_observation_id("twin-shared-001", "PRT-WLV-01", 1880) == "n-shared-001-PRT-WLV-01-1880"
    assert from_twin(TWIN_ROW).observation_id == "n-shared-001-PRT-WLV-01-1880"


def test_all_twin_sensor_types_are_understood():
    for spec in catalog.sensors().values():
        row = {**TWIN_ROW, "sensor_id": spec.sensor_id, "zone_id": spec.zone_id, "type": spec.type,
               "unit": spec.unit, "reported_value": sum(spec.normal_limits) / 2}
        r = from_twin(row)
        assert r is not None, spec.sensor_id
        assert r.severity_class == "normal", spec.sensor_id


@pytest.mark.parametrize("body,expected,allowed", [
    ({"verdict": "TRUSTED", "trust_score": 0.95, "reason_codes": [], "recommended_action": "NONE"}, Verdict.TRUSTED, True),
    ({"verdict": "TRUSTED", "trust_score": 0.5, "reason_codes": [], "recommended_action": "NONE"}, Verdict.TRUSTED, False),
    ({"verdict": "UNTRUSTED", "trust_score": 0.6, "reason_codes": ["BACKUP_DISAGREEMENT"], "recommended_action": "MONITOR"}, Verdict.REJECTED, False),
    ({"verdict": "UNTRUSTED", "trust_score": 1.0, "reason_codes": ["REPLAY_OR_OUT_OF_ORDER"], "recommended_action": "MONITOR"}, Verdict.SUSPICIOUS, False),
    ({"verdict": "UNTRUSTED", "trust_score": 0.0, "reason_codes": ["SENSOR_FAILED"], "recommended_action": "QUARANTINE"}, Verdict.REJECTED, False),
])
def test_guardian_verdict_mapping_and_gate(body, expected, allowed):
    body = {"verdict_id": "VRD-000001", "observation_id": "o", "device_id": "PRT-WLV-01", "risk_level": "LOW",
            "issued_at": "2026-11-06T12:00:00Z", **body}
    v = GuardianVerdict.from_guardian_layer(body, "corr", 1)
    assert v.verdict == expected
    assert TrustGate().check(v).allowed is allowed
    assert TrustGate().check(None).allowed is False          # no answer = UNTRUSTED


def _incident(kind, sensor, zone):
    spec = catalog.sensors().get(sensor)
    return DetectedIncident(kind=kind, correlation_id="c", sensor_id=sensor, zone=zone, severity_class="normal",
                            evidence={"linked_device_ids": list(spec.linked_device_ids) if spec else []})


def _all_planned_commands():
    book = ResponderBook()
    cases = [
        (IncidentKind.FLOOD, "PRT-WLV-01", "PRT"), (IncidentKind.FLOOD, "DTN-WLV-01", "DTN"),
        (IncidentKind.FLOOD, "RES-WLV-01", "RES"), (IncidentKind.OVERHEAT, "IND-TMP-01", "IND"),
        (IncidentKind.TRAFFIC_JAM, "DTN-TRF-01", "DTN"), (IncidentKind.TRAFFIC_JAM, "DTN-TRF-02", "DTN"),
        (IncidentKind.ACCIDENT, "CALL-IND", "IND"), (IncidentKind.FIRE, "CALL-RES", "RES"),
    ]
    out = []
    for kind, sensor, zone in cases:
        inc = _incident(kind, sensor, zone)
        out += plan_for(inc, None, book, set()).commands + recovery_plan(inc).commands
    return out


def test_every_command_targets_a_supported_twin_device():
    cmds = _all_planned_commands()
    assert cmds
    for c in cmds:
        a = catalog.actuators()[c.device_id]
        assert a.supported and c.command in a.commands, (c.device_id, c.command)


def test_unsupported_residential_signal_is_never_commanded():
    plan = plan_for(_incident(IncidentKind.TRAFFIC_JAM, "RES-TRF-01", "RES"), None, ResponderBook(), set())
    assert plan.commands == [] and any("RES-TLT-01" in n for n in plan.notes)


TWIN_REPO = os.environ.get("TWIN_REPO")


@pytest.mark.skipif(not TWIN_REPO, reason="set TWIN_REPO to a Twin checkout")
def test_commands_pass_the_twins_own_translation():
    sys.path.insert(0, str(Path(TWIN_REPO) / "src"))
    from city_live.commands import build_command_effect  # the Twin's code, not ours
    for c in _all_planned_commands():
        effect = build_command_effect(c.device_id, c.command, c.params)
        assert effect["target_id"], (c.device_id, c.command)
