"""Air-quality model (PM2.5), trained on the Twin layer's own air physics.

The Twin core evolves PM2.5 per zone every second (Twin README, Phase08):
    next = 10 + (previous - 10) / 1.01 + external_source + plant_contribution
    plant_contribution = 0.2 (industrial) / 0.05 (residential) while PLANT-01 is powered (F-IND closed)
`scripts/train_air_model.py` simulates that equation to build training data, then fits:
  * a classifier: action in {no_action, health_advisory, curtail_industry}
  * a regressor:  PM2.5 expected 10 minutes ahead if nothing is done
Feature extraction lives here so training and serving can never drift apart.
"""

from __future__ import annotations

import logging
import math
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

ZONES = ["DTN", "HSP", "IND", "PRT", "RES"]
PLANT_SHARE = {"IND": 0.2, "RES": 0.05}       # µg/m³ per second from PLANT-01 (Twin core)
BACKGROUND = 10.0
DECAY = 1.01
ACTIONS = ["no_action", "health_advisory", "curtail_industry"]
FEATURES = ["pm25", "delta_short", "delta_long", "plant_active", "plant_share",
            "zone_DTN", "zone_HSP", "zone_IND", "zone_PRT", "zone_RES", "hour_sin", "hour_cos"]
ADVISORY_LEVEL = 35.0                         # µg/m³: catalog normal max for the Brain's air monitors
CURTAIL_LEVEL = 75.0                          # µg/m³ forecast that justifies cutting the plant


def step(pm: float, source: float, zone: str, plant_active: bool) -> float:
    """One second of the Twin's air equation."""
    return BACKGROUND + (pm - BACKGROUND) / DECAY + source + (PLANT_SHARE.get(zone, 0.0) if plant_active else 0.0)


def features(pm25: float, history: list[float], zone: str, plant_active: bool, hour: float) -> list[float]:
    """history = previous samples (oldest first, 5 s apart), excluding the current one."""
    h = [*history[-6:], pm25]
    short = h[-1] - h[-2] if len(h) >= 2 else 0.0
    long = (h[-1] - h[0]) / max(1, len(h) - 1) if len(h) >= 2 else 0.0
    return [
        pm25, short, long, 1.0 if plant_active else 0.0, PLANT_SHARE.get(zone, 0.0),
        *[1.0 if zone == z else 0.0 for z in ZONES],
        math.sin(2 * math.pi * hour / 24), math.cos(2 * math.pi * hour / 24),
    ]


def label(pm_now: float, forecast10: float, zone: str, plant_active: bool) -> str:
    worst = max(pm_now, forecast10)
    if worst >= CURTAIL_LEVEL and plant_active and zone in PLANT_SHARE:
        return "curtail_industry"
    if worst > ADVISORY_LEVEL:
        return "health_advisory"
    return "no_action"


class AirModel:
    def __init__(self, path: str | Path = "models/air_quality_v1.pkl") -> None:
        self.path = Path(path)
        self.clf = self.reg = None
        self.metrics: dict = {}
        try:
            import joblib
            b = joblib.load(self.path)
            if b.get("features") != FEATURES:
                raise ValueError("feature list mismatch: retrain with scripts/train_air_model.py")
            self.clf, self.reg, self.metrics = b["classifier"], b["regressor"], b.get("metrics", {})
            log.info("air model loaded path=%s accuracy=%s", self.path, self.metrics.get("accuracy"))
        except Exception as e:                                   # noqa: BLE001
            log.warning("air model unavailable (%s): rules only", e)

    @property
    def ready(self) -> bool:
        return self.clf is not None

    def predict(self, pm25: float, history: list[float], zone: str, plant_active: bool, hour: float) -> dict:
        x = [features(pm25, history, zone, plant_active, hour)]
        if not self.ready:
            fc = pm25
            return {"action": label(pm25, fc, zone, plant_active), "confidence": 1.0, "forecast_10min": round(fc, 1),
                    "by": "rules"}
        proba = self.clf.predict_proba(x)[0]
        i = int(proba.argmax())
        return {"action": str(self.clf.classes_[i]), "confidence": round(float(proba[i]), 3),
                "forecast_10min": round(float(self.reg.predict(x)[0]), 1), "by": "air-model"}


@lru_cache(maxsize=1)
def get_air_model() -> AirModel:
    from city_brain.config import settings
    return AirModel(Path(settings.model_path).parent / "air_quality_v1.pkl")
