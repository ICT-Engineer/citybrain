"""Train the City Brain decision model (v3).

Improvements over v2:
  - Hybrid training: synthetic + real NYC data
  - Real data from NYC Open Data:
      * 10 000 motor vehicle collisions (h9gi-nx95)
      * ~4 000 fire/emergency dispatches (8m42-w767)
  - Class-balanced subsampling of real data to avoid the 93%
    dispatch_ambulance_police bias observed in raw NYC records
  - Same 16-feature schema as v2 (no downstream changes needed)
  - Reported as train/test split with per-class metrics

Inputs:
    data/real_samples.csv  (produced by scripts/load_real_dataset.py)
Output:
    models/city_brain_v1.pkl

Run:  python scripts/train_model.py
"""

from __future__ import annotations

import csv
import random
from collections import defaultdict
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, classification_report
from sklearn.model_selection import train_test_split

# ---------------------------------------------------------------------------
# Label space
# ---------------------------------------------------------------------------
ACTIONS = [
    "dispatch_ambulance_police",
    "dispatch_fire_ambulance",
    "retime_traffic_signal",
    "open_valve",
    "no_action",
]

# ---------------------------------------------------------------------------
# Feature space — DO NOT change; must match src/city_brain/decision/model.py
# ---------------------------------------------------------------------------
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

SENSOR_TYPES = ["emergency_call", "traffic", "water", "air", "power"]
ZONES = ["downtown", "harbor", "north", "south", "airport", "industrial"]


# ---------------------------------------------------------------------------
# Synthetic data generator (unchanged from v2)
# ---------------------------------------------------------------------------

def _seasonal_weights(month: int) -> dict[str, float]:
    if month in (6, 7, 8):
        return {"accident": 0.35, "fire": 0.40, "flood": 0.05, "medical": 0.15, "other": 0.05}
    if month in (10, 11, 12, 1):
        return {"accident": 0.35, "fire": 0.15, "flood": 0.30, "medical": 0.15, "other": 0.05}
    return {"accident": 0.40, "fire": 0.25, "flood": 0.15, "medical": 0.15, "other": 0.05}


def _traffic_weights(hour: int) -> float:
    if 7 <= hour <= 9 or 17 <= hour <= 19:
        return 0.50
    if 10 <= hour <= 16:
        return 0.30
    return 0.15


def _random_reading() -> dict:
    hour = random.choices(range(24), weights=[_traffic_weights(h) for h in range(24)], k=1)[0]
    day = random.randint(0, 6)
    month = random.randint(1, 12)
    zone = random.choice(ZONES)

    stype_weights = [0.25, _traffic_weights(hour), 0.10, 0.10, 0.05]
    stype = random.choices(SENSOR_TYPES, weights=stype_weights, k=1)[0]
    severity = "critical" if random.random() < 0.35 else "normal"

    reading: dict = {
        "type": stype,
        "severity_class": severity,
        "hour": hour,
        "day": day,
        "month": month,
        "zone": zone,
        "value": None,
        "trend": 0.0,
        "recent_incidents": random.randint(0, 10),
    }

    if stype == "emergency_call":
        weights = _seasonal_weights(month)
        reading["incident_type"] = random.choices(list(weights.keys()), weights=list(weights.values()), k=1)[0]
    elif stype in ("traffic", "water", "air", "power"):
        reading["value"] = round(random.uniform(0.0, 1.0), 3)
        reading["trend"] = round(random.uniform(-0.3, 0.3), 3)

    return reading


def _label_synthetic(reading: dict) -> str:
    if reading["type"] == "emergency_call":
        it = reading.get("incident_type", "other")
        if it == "accident":
            return "dispatch_ambulance_police"
        if it == "fire":
            return "dispatch_fire_ambulance"
        if it == "flood":
            return "open_valve"
        return "no_action"

    if reading["type"] == "traffic" and reading["value"] is not None:
        if reading["value"] > 0.8:
            return "retime_traffic_signal"

    if reading["type"] == "water" and reading["value"] is not None:
        if reading["value"] > 0.7 or reading["trend"] > 0.2:
            return "open_valve"

    return "no_action"


def _apply_label_noise(y: list[str], noise: float = 0.03) -> list[str]:
    out = []
    for label in y:
        if random.random() < noise:
            out.append(random.choice(ACTIONS))
        else:
            out.append(label)
    return out


def _zone_index(zone: str) -> float:
    """Stable across runs (was hash(), randomized per process). Must match decision/model.py."""
    import zlib

    return (zlib.crc32(zone.encode("utf-8")) % 1000) / 1000.0


def _features(reading: dict) -> list[float]:
    stype = reading["type"]
    sev = reading["severity_class"]
    it = reading.get("incident_type", "")
    val = reading.get("value")

    return [
        1.0 if stype == "emergency_call" else 0.0,
        1.0 if stype == "traffic" else 0.0,
        1.0 if stype == "water" else 0.0,
        1.0 if stype == "air" else 0.0,
        1.0 if stype == "power" else 0.0,
        1.0 if sev == "critical" else 0.0,
        float(val) if val is not None else 0.0,
        1.0 if it == "accident" else 0.0,
        1.0 if it == "fire" else 0.0,
        1.0 if it == "flood" else 0.0,
        1.0 if it in ("medical", "other", "") else 0.0,
        reading["hour"] / 23.0,
        reading["day"] / 6.0,
        _zone_index(reading["zone"]),
        reading.get("trend", 0.0),
        min(reading.get("recent_incidents", 0), 10) / 10.0,
    ]


# ---------------------------------------------------------------------------
# Real data loader
# ---------------------------------------------------------------------------

def _load_real_samples(path: Path, per_class_cap: int = 500) -> tuple[list[dict], list[str]]:
    """Load real_samples.csv, subsample per class to balance.

    per_class_cap: maximum number of examples per label to keep.
    """
    if not path.exists():
        print(f"[warn] {path} not found — skipping real data")
        return [], []

    buckets: dict[str, list[dict]] = defaultdict(list)
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            label = r["label"]
            if label not in ACTIONS:
                continue
            buckets[label].append({
                "type": r["type"],
                "severity_class": r["severity_class"],
                "hour": int(r["hour"]),
                "day": int(r["day"]),
                "zone": r["zone"] or "unknown",
                "value": float(r["value"]) if r["value"] else None,
                "trend": float(r["trend"]) if r["trend"] else 0.0,
                "incident_type": r["incident_type"],
                "recent_incidents": 0,
            })

    sampled: list[dict] = []
    labels: list[str] = []
    for label, items in buckets.items():
        random.shuffle(items)
        keep = items[:per_class_cap]
        sampled.extend(keep)
        labels.extend([label] * len(keep))
        print(f"  real {label:32s} {len(keep):5d} (from {len(items)})")
    return sampled, labels


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    random.seed(42)
    np.random.seed(42)

    # 1. Synthetic
    N_SYNTH = 20_000
    print(f"Generating {N_SYNTH} synthetic readings...")
    synth = [_random_reading() for _ in range(N_SYNTH)]
    synth_labels = _apply_label_noise([_label_synthetic(r) for r in synth], noise=0.03)

    # 2. Real (balanced subsample)
    print("\nLoading real NYC data (balanced subsample, cap=500/class)...")
    real, real_labels = _load_real_samples(Path("data/real_samples.csv"), per_class_cap=500)

    # 3. Combine
    all_readings = synth + real
    all_labels = synth_labels + real_labels
    print(f"\nTotal training pool: {len(all_readings)} examples")
    print(f"  synthetic: {len(synth)}")
    print(f"  real:      {len(real)}")

    print("\nFinal class distribution:")
    from collections import Counter
    for action, count in Counter(all_labels).most_common():
        print(f"  {action:32s} {count:5d}")

    # 4. Features
    X = np.array([_features(r) for r in all_readings])
    y = np.array(all_labels)

    # 5. Split
    print("\nSplitting train/test 80/20...")
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y,
    )

    # 6. Train
    print("Training RandomForest (100 trees, max_depth=10, balanced)...")
    clf = RandomForestClassifier(
        n_estimators=100,
        max_depth=10,
        random_state=42,
        n_jobs=-1,
        class_weight="balanced",
    )
    clf.fit(X_train, y_train)

    train_acc = accuracy_score(y_train, clf.predict(X_train))
    test_acc = accuracy_score(y_test, clf.predict(X_test))
    print(f"\nTrain accuracy: {train_acc:.4f}")
    print(f"Test  accuracy: {test_acc:.4f}")

    print("\nClassification report (test set):")
    print(classification_report(y_test, clf.predict(X_test), zero_division=0))

    print("Feature importances:")
    for name, imp in sorted(zip(FEATURES, clf.feature_importances_), key=lambda x: -x[1]):
        print(f"  {name:24s} {imp:.4f}")

    out = Path("models")
    out.mkdir(exist_ok=True)
    out_file = out / "city_brain_v1.pkl"
    joblib.dump({"model": clf, "features": FEATURES, "actions": ACTIONS}, out_file)
    print(f"\nSaved model to {out_file}")


if __name__ == "__main__":
    main()