"""Train the City Brain air-quality model on the Twin layer's PM2.5 equation.

    python scripts/train_air_model.py          # writes models/air_quality_v1.pkl (+ .metrics.json)

Data: 600 simulated episodes per zone. Each episode has a random start level, random pollution
episodes (external source 0..1.2 µg/m³/s, like the Twin's 0..100 bounded air driver at small
values), the plant powered or not, and sensor noise. Samples are taken every 5 s like a monitor.
Target action uses the PM2.5 that the Twin equation predicts 10 minutes ahead if nothing is done.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import GradientBoostingRegressor, RandomForestClassifier
from sklearn.metrics import accuracy_score, classification_report, mean_absolute_error
from sklearn.model_selection import train_test_split

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from city_brain.decision.air_model import ACTIONS, FEATURES, ZONES, features, label, step  # noqa: E402


def forecast(pm: float, source: float, zone: str, plant: bool, seconds: int = 600) -> float:
    for _ in range(seconds):
        pm = step(pm, source, zone, plant)
    return pm


def episodes(n_per_zone: int, rng: random.Random):
    X, y, f10 = [], [], []
    for zone in ZONES:
        for _ in range(n_per_zone):
            plant = rng.random() < 0.8
            pm = rng.uniform(8, 120)
            source = 0.0
            hist: list[float] = []
            hour = rng.uniform(0, 24)
            for t in range(900):                               # 15 minutes
                if t % 120 == 0:                               # a new pollution phase every 2 minutes
                    source = rng.choice([0.0, 0.0, 0.0, rng.uniform(0.05, 0.4), rng.uniform(0.4, 1.2)])
                pm = step(pm, source, zone, plant)
                if t % 5 == 0:
                    obs = max(0.0, pm + rng.gauss(0, 0.4))
                    if len(hist) >= 3:
                        fc = forecast(pm, source, zone, plant)
                        X.append(features(obs, hist, zone, plant, (hour + t / 3600) % 24))
                        y.append(label(obs, fc, zone, plant))
                        f10.append(fc)
                    hist = (hist + [obs])[-6:]
    return np.array(X), np.array(y), np.array(f10)


def main() -> None:
    rng = random.Random(2026)
    X, y, f10 = episodes(120, rng)
    Xtr, Xte, ytr, yte, ftr, fte = train_test_split(X, y, f10, test_size=0.2, random_state=7, stratify=y)
    clf = RandomForestClassifier(n_estimators=100, max_depth=12, min_samples_leaf=3, n_jobs=-1, random_state=7)
    clf.fit(Xtr, ytr)
    reg = GradientBoostingRegressor(n_estimators=250, max_depth=4, learning_rate=0.08, random_state=7)
    reg.fit(Xtr, ftr)
    pred = clf.predict(Xte)
    metrics = {
        "samples": int(len(X)), "train": int(len(Xtr)), "test": int(len(Xte)),
        "accuracy": round(float(accuracy_score(yte, pred)), 4),
        "forecast_mae_ug_m3": round(float(mean_absolute_error(fte, reg.predict(Xte))), 2),
        "class_counts": {a: int((y == a).sum()) for a in ACTIONS},
        "report": classification_report(yte, pred, output_dict=True, zero_division=0),
        "source": "Twin core PM2.5 equation: next = 10 + (prev-10)/1.01 + source + plant",
    }
    out = ROOT / "models" / "air_quality_v1.pkl"
    joblib.dump({"classifier": clf, "regressor": reg, "features": FEATURES, "actions": ACTIONS, "metrics": metrics}, out, compress=3)
    (ROOT / "models" / "air_quality_v1.metrics.json").write_text(json.dumps(metrics, indent=2))
    print(json.dumps({k: v for k, v in metrics.items() if k != "report"}, indent=2))
    print(classification_report(yte, pred, zero_division=0))


if __name__ == "__main__":
    main()
