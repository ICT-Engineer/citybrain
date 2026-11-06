"""Wrapper around the trained decision model (v2).

Loads the RandomForest .pkl at import time, exposes a predict() that
takes a SensorReading and returns (action_name, confidence).

Feature extraction matches scripts/train_model.py FEATURES list.
Falls back to None on any error, so callers can decide what to do.
"""

from __future__ import annotations

import logging
from pathlib import Path

import joblib
import numpy as np

from city_brain.config import settings
from city_brain.schemas.twin import SensorReading, SensorType

log = logging.getLogger(__name__)


# Must match FEATURES in scripts/train_model.py
FEATURES = [
    "is_emergency_call",
    "is_traffic",
    "is_water",
    "is_air",
    "is_power",
    "is_critical",
    "value_normalized",
    "incident_accident",
    "incident_fire",
    "incident_flood",
    "incident_other",
    "hour_of_day",
    "day_of_week",
    "zone_index",
    "value_trend",
    "recent_incidents_zone",
]


def _zone_index(zone: str) -> float:
    """Stable zone name -> 0..1.

    FIX: the original used Python's built-in hash(), which is randomized per process
    (PYTHONHASHSEED), so the same zone gave a different feature after every restart and
    decisions were not reproducible. crc32 is stable across runs and machines. The
    shipped model was trained on hash() values (uniform noise in [0, 1)), so any stable
    value in that range stays inside its training distribution.
    """
    import zlib

    return (zlib.crc32(zone.encode("utf-8")) % 1000) / 1000.0


class DecisionModel:
    """Trained classifier + feature extractor."""

    def __init__(self, path: str | None = None) -> None:
        self.path = Path(path or settings.model_path)
        self._model = None
        self._actions: list[str] = []
        self._load()

    def _load(self) -> None:
        try:
            bundle = joblib.load(self.path)
            self._model = bundle["model"]
            self._actions = bundle["actions"]
            n_features = len(bundle.get("features", []))
            log.info(
                "decision model loaded path=%s actions=%d features=%d",
                self.path, len(self._actions), n_features,
            )
            expected = len(FEATURES)
            if n_features and n_features != expected:
                log.error(
                    "MODEL FEATURE MISMATCH: .pkl has %d features, wrapper has %d. "
                    "Retrain with `python scripts/train_model.py`.",
                    n_features, expected,
                )
                self._model = None
        except Exception as e:
            log.error("failed to load decision model from %s: %s", self.path, e)
            self._model = None

    @property
    def ready(self) -> bool:
        return self._model is not None

    def _extract_features(self, reading: SensorReading) -> list[float]:
        stype = reading.type
        sev = reading.severity_class
        it = (reading.emergency.incident_type.lower() if reading.emergency else "")
        val = reading.value if isinstance(reading.value, (int, float)) else None

        hour = reading.ts.hour
        day = reading.ts.weekday()

        return [
            1.0 if stype == SensorType.EMERGENCY_CALL else 0.0,
            1.0 if stype == SensorType.TRAFFIC else 0.0,
            1.0 if stype == SensorType.WATER else 0.0,
            1.0 if stype == SensorType.AIR else 0.0,
            1.0 if stype == SensorType.POWER else 0.0,
            1.0 if sev.value == "critical" else 0.0,
            float(val) if val is not None else 0.0,
            1.0 if it == "accident" else 0.0,
            1.0 if it == "fire" else 0.0,
            1.0 if it == "flood" else 0.0,
            1.0 if it in ("medical", "other", "") else 0.0,
            hour / 23.0,
            day / 6.0,
            _zone_index(reading.zone),
            0.0,   # value_trend — placeholder, needs history
            0.0,   # recent_incidents_zone — placeholder, needs history
        ]

    def predict(self, reading: SensorReading) -> tuple[str, float] | None:
        """Return (action_name, confidence) or None if the model can't decide."""
        if not self.ready:
            return None
        try:
            x = np.array([self._extract_features(reading)])
            pred = self._model.predict(x)[0]
            proba = float(self._model.predict_proba(x).max())
            return str(pred), proba
        except Exception as e:
            log.warning("model.predict failed: %s", e)
            return None


_model_singleton: DecisionModel | None = None


def get_model() -> DecisionModel:
    global _model_singleton
    if _model_singleton is None:
        _model_singleton = DecisionModel()
    return _model_singleton