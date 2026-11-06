"""TSYP14 Phase 1 rules: input validation with clear errors (jury checklist) and the R3 timeline."""

import pytest

from city_brain.adapters.twin_reading import validate_twin_row
from city_brain.observability.timeline import Timeline

GOOD = {"sensor_id": "PRT-WLV-01", "type": "water_level", "reported_value": 55.0, "seq": 12,
        "timestamp": "2026-11-06T10:00:00Z", "unit": "cm", "zone_id": "PRT"}


def test_good_twin_row_is_accepted():
    assert validate_twin_row(GOOD) is None


@pytest.mark.parametrize("patch,code", [
    ({"reported_value": "high"}, "BAD_VALUE"),
    ({"reported_value": True}, "BAD_VALUE"),
    ({"type": "radiation"}, "UNKNOWN_TYPE"),
    ({"seq": -1}, "BAD_SEQ"),
    ({"sensor_id": "bad id/../x"}, "BAD_SENSOR_ID"),
    ({"timestamp": "yesterday"}, "BAD_TIMESTAMP"),
])
def test_malformed_rows_get_a_clear_reason(patch, code):
    assert validate_twin_row({**GOOD, **patch}).startswith(code)


def test_missing_field_and_non_object():
    row = dict(GOOD); row.pop("type")
    assert validate_twin_row(row) == "MISSING_FIELD:type"
    assert validate_twin_row(["not", "a", "dict"]) == "NOT_AN_OBJECT"


def test_timeline_keeps_order_and_cursor():
    t = Timeline()
    for stage in ("reading", "verdict", "decision", "action"):
        t.add(stage, "brain", "c1", stage)
    ev = t.since(0)
    assert [e["stage"] for e in ev] == ["reading", "verdict", "decision", "action"]
    assert all(e["timestamp"].endswith("Z") for e in ev)
    assert [e["stage"] for e in t.since(ev[1]["id"])] == ["decision", "action"]
