"""The Brain and the Twin must describe the same city, with sensors at the same map points.

TWIN_EXPECTED below was read from the live Twin dashboard DOM (circle[data-sensor] cx/cy)
on 2026-11-06, commit a80f0a9 + sensor fix. Set TWIN_REPO=/path/to/CS-TSYP-TWIN-LAYER to
also compare the copied catalog files byte-for-byte and check the placement rule in map.js.
"""

import hashlib
import os
from pathlib import Path

import pytest

from city_brain import catalog

TWIN_EXPECTED = {
    "DTN-TMP-01": (740, 603), "DTN-TRF-01": (758, 603), "DTN-TRF-02": (740, 585), "DTN-WLV-01": (758, 585),
    "IND-FLW-01": (1080, 178), "IND-PRS-01": (1098, 178), "IND-TMP-01": (1080, 160), "IND-TMP-02": (1098, 160),
    "PRT-FLW-01": (1340, 828), "PRT-PRS-01": (1358, 828), "PRT-WLV-01": (1340, 810), "PRT-WLV-02": (1358, 810),
    "RES-FLW-01": (205, 598), "RES-PRS-01": (223, 598), "RES-TRF-01": (205, 580), "RES-WLV-01": (223, 580),
}


def test_sixteen_sensors_from_the_twin_inventory():
    assert set(catalog.sensors()) == set(TWIN_EXPECTED)


def test_sensor_positions_match_the_twin_dashboard():
    pos = catalog.sensor_positions()
    assert {k: (v["x"], v["y"]) for k, v in pos.items()} == TWIN_EXPECTED


def test_geography_uses_twin_coordinate_frame():
    g = catalog.geography()
    assert g["viewBox"] == [0, 0, 1536, 1024]
    assert {r["id"] for r in g["roads"]} == {"R-LOW", "R-HIGH", "R-LINK", "R-DOWN", "R-IND"}
    assert any(a["id"] == "PRT" and (a["x"], a["y"]) == (1320, 850) for a in g["anchors"])


def test_routes_follow_the_twin_road_graph():
    assert catalog.route("J-H1", "J-D1") == ["R-LOW"]
    assert catalog.route("J-H1", "J-I1") == ["R-LOW", "R-IND"]
    assert catalog.route("J-H1", "J-D1", blocked={"R-LOW"}) == ["R-HIGH", "R-LINK", "R-DOWN"]
    assert catalog.route("J-H1", "J-I1", blocked={"R-LOW", "R-IND"}) is None


TWIN_REPO = os.environ.get("TWIN_REPO")
COPIES = {
    "twin_sensors.json": "src/city_live/data/sensors.json",
    "twin_device_catalog.json": "src/city_live/data/device_catalog.json",
    "twin_core_catalog.json": "src/city_core/data/catalog.json",
    "map-layout.json": "src/city_dashboard/web/map-layout.json",
    "city-map.svg": "src/city_dashboard/web/city-map.svg",
}


@pytest.mark.skipif(not TWIN_REPO, reason="set TWIN_REPO to a Twin checkout")
@pytest.mark.parametrize("mine,theirs", COPIES.items())
def test_catalog_files_are_unchanged_copies(mine, theirs):
    h = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
    assert h(Path(catalog.__file__).parent / mine) == h(Path(TWIN_REPO) / theirs)


@pytest.mark.skipif(not TWIN_REPO, reason="set TWIN_REPO to a Twin checkout")
def test_twin_map_js_still_uses_the_same_placement_rule():
    js = (Path(TWIN_REPO) / "src/city_dashboard/web/map.js").read_text(encoding="utf-8")
    assert "cx:a.x+20+(index%2)*18" in js and "cy:a.y-22-Math.floor(index/2)*18" in js
    assert "{id:'PRT',x:1320,y:850" in js
    assert "zones={DTN:'Z-DOWNTOWN',IND:'Z-INDUSTRIAL',RES:'Z-RESIDENTIAL',PRT:'PRT'}" in js
