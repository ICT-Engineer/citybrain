"""R3 live timeline: reading -> Guardian verdict -> Brain decision -> actuator action, with timestamps.

Every step is stamped with a UTC timestamp and the incident's correlation_id, so one incident
can be followed across the three layers. Shown on GET /timeline (screen) and GET /api/timeline
(JSON, cursor `since`). The scenario runner (scenarios/run.py) writes the same events to logs/.
The event shape matches the Guardian layer's shared timeline
({"timestamp", "layer", "event", "observation_id", ...}) so the screens can be merged later.
"""

from __future__ import annotations

import itertools
import threading
from collections import deque
from datetime import UTC, datetime

STAGES = ("reading", "verdict", "decision", "action", "input_rejected", "network", "vault")


class Timeline:
    def __init__(self, maxlen: int = 3000) -> None:
        self._ev: deque[dict] = deque(maxlen=maxlen)
        self._id = itertools.count(1)
        self._lock = threading.Lock()

    def add(self, stage: str, layer: str, correlation_id: str | None, summary: str, **fields) -> dict:
        e = {"id": next(self._id), "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
             "stage": stage, "layer": layer, "correlation_id": correlation_id, "summary": summary, **fields}
        with self._lock:
            self._ev.append(e)
        return e

    def since(self, cursor: int = 0, limit: int = 500) -> list[dict]:
        with self._lock:
            return [e for e in self._ev if e["id"] > cursor][:limit]

    def clear(self) -> None:
        with self._lock:
            self._ev.clear()


TIMELINE = Timeline()
