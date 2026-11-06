"""Load and adapt real NYC datasets to City Brain training format.

Sources:
  - data/nyc_collisions.csv  (NYC Motor Vehicle Collisions)
  - data/nyc_fires.csv       (NYC Fire Incident Dispatch Data)

Produces: data/real_samples.csv with columns:
    type, severity_class, hour, day, zone, value, trend, incident_type, label
"""

from __future__ import annotations

import csv
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

# --- NYC boroughs -> zone names (stable order) ---
BOROUGHS = ["MANHATTAN", "BROOKLYN", "QUEENS", "BRONX", "STATEN ISLAND"]


def _borough_to_zone(b: str) -> str:
    b = (b or "").strip().upper()
    return b.lower().replace(" ", "_") if b in BOROUGHS else "unknown"


def _parse_datetime(date_str: str, time_str: str = "") -> datetime | None:
    """Parse 'YYYY-MM-DDTHH:MM:SS.000' + 'H:MM' or similar."""
    try:
        if "T" in date_str:
            return datetime.fromisoformat(date_str.split(".")[0])
        if time_str:
            return datetime.fromisoformat(f"{date_str} {time_str}")
        return datetime.fromisoformat(date_str)
    except Exception:
        return None


def _safe_int(v: str, default: int = 0) -> int:
    try:
        return int(float(v))
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Collisions -> SensorReading-like rows
# ---------------------------------------------------------------------------

def load_collisions(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            dt = _parse_datetime(r.get("crash_date", ""), r.get("crash_time", ""))
            if dt is None:
                continue

            injured = _safe_int(r.get("number_of_persons_injured", "0"))
            killed = _safe_int(r.get("number_of_persons_killed", "0"))
            factor = r.get("contributing_factor_vehicle_1", "").strip()

            # Severity: any injury/kill -> critical
            severity = "critical" if (injured > 0 or killed > 0) else "normal"

            # Value: proxy for severity 0..1
            value = min(1.0, (injured * 0.2) + (killed * 0.5))

            # Label: always dispatch_ambulance_police for accidents
            label = "dispatch_ambulance_police"

            rows.append({
                "type": "emergency_call",
                "severity_class": severity,
                "hour": dt.hour,
                "day": dt.weekday(),
                "zone": _borough_to_zone(r.get("borough", "")),
                "value": round(value, 3),
                "trend": 0.0,
                "incident_type": "accident",
                "label": label,
                "source": "nyc_collisions",
                "factor": factor[:40],
            })
    return rows


# ---------------------------------------------------------------------------
# Fires -> SensorReading-like rows
# ---------------------------------------------------------------------------

def _classify_fire(classification: str) -> tuple[str, str] | None:
    """Return (incident_type, label) or None if not useful."""
    c = classification.lower()

    # Fires
    if "fire" in c:
        # Distinguish severity by context
        if any(k in c for k in ["dwelling", "multiple dwelling", "high-rise", "commercial"]):
            return ("fire", "dispatch_fire_ambulance")
        if "rubbish" in c or "debris" in c or "food on the stove" in c:
            return ("fire", "dispatch_fire_ambulance")
        return ("fire", "dispatch_fire_ambulance")

    # Water / flooding
    if "water" in c and "utility" in c:
        return ("flood", "open_valve")

    # Medical
    if "medical" in c:
        return ("medical", "dispatch_ambulance_police")

    # Vehicle accident
    if "vehicle accident" in c:
        return ("accident", "dispatch_ambulance_police")

    # Gas / electric utilities (dangerous but not fire)
    if "gas" in c and "utility" in c:
        return ("other", "dispatch_fire_ambulance")  # treat as hazard

    if "electric" in c and "utility" in c:
        return ("other", "no_action")  # power handled separately

    return None


def load_fires(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            dt = _parse_datetime(r.get("incident_datetime", ""))
            if dt is None:
                continue

            classification = r.get("incident_classification", "").strip()
            mapped = _classify_fire(classification)
            if mapped is None:
                continue
            incident_type, label = mapped

            severity = "critical" if incident_type in ("fire", "flood") else "normal"
            value = 0.9 if severity == "critical" else 0.3

            rows.append({
                "type": "emergency_call",
                "severity_class": severity,
                "hour": dt.hour,
                "day": dt.weekday(),
                "zone": _borough_to_zone(r.get("incident_borough", "")),
                "value": value,
                "trend": 0.0,
                "incident_type": incident_type,
                "label": label,
                "source": "nyc_fires",
                "factor": classification[:40],
            })
    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    root = Path("data")
    collisions_path = root / "nyc_collisions.csv"
    fires_path = root / "nyc_fires.csv"
    out_path = root / "real_samples.csv"

    all_rows: list[dict] = []

    if collisions_path.exists():
        print(f"Loading {collisions_path}...")
        c_rows = load_collisions(collisions_path)
        print(f"  -> {len(c_rows)} collision rows")
        all_rows.extend(c_rows)
    else:
        print(f"[skip] {collisions_path} not found")

    if fires_path.exists():
        print(f"Loading {fires_path}...")
        f_rows = load_fires(fires_path)
        print(f"  -> {len(f_rows)} fire rows")
        all_rows.extend(f_rows)
    else:
        print(f"[skip] {fires_path} not found")

    if not all_rows:
        print("No rows loaded. Aborting.")
        return

    print(f"\nTotal real rows: {len(all_rows)}")
    print("Label distribution:")
    for label, count in Counter(r["label"] for r in all_rows).most_common():
        print(f"  {count:6d}  {label}")

    print("\nSource distribution:")
    for src, count in Counter(r["source"] for r in all_rows).most_common():
        print(f"  {count:6d}  {src}")

    out_path.parent.mkdir(exist_ok=True)
    fieldnames = [
        "type", "severity_class", "hour", "day", "zone", "value", "trend",
        "incident_type", "label", "source", "factor",
    ]
    with out_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(all_rows)

    print(f"\nSaved {len(all_rows)} rows to {out_path}")


if __name__ == "__main__":
    main()