"""In-memory metrics for City Brain.

Exposes counters and timings via /metrics so evaluators can see:
  - how many critical-path events ran in parallel
  - how much time the parallel path saved vs sequential
  - how many near-misses (critical events rejected by Guardian)
  - dispatch/discard counts
  - replays blocked by the replay guard

Not Prometheus-compatible on purpose — plain JSON keeps it readable
for a live demo without extra dependencies.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class Metrics:
    # --- Critical path (§3.1) ---
    critical_path_total: int = 0
    critical_path_saved_ms_total: float = 0.0
    critical_near_misses: int = 0

    # --- Normal path (§3.2) ---
    normal_path_total: int = 0

    # --- Outcomes ---
    dispatches_total: int = 0
    discards_total: int = 0
    replays_rejected_total: int = 0
    discards_by_reason: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    # --- Throughput ---
    readings_ingested_total: int = 0
    incidents_detected_total: int = 0

    # Thread safety (FastAPI can call from multiple workers)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ------------------------------------------------------------------
    # Recording helpers
    # ------------------------------------------------------------------
    def record_ingest(self) -> None:
        with self._lock:
            self.readings_ingested_total += 1

    def record_incident(self) -> None:
        with self._lock:
            self.incidents_detected_total += 1

    def record_critical_path(self, saved_ms: float) -> None:
        with self._lock:
            self.critical_path_total += 1
            self.critical_path_saved_ms_total += max(0.0, saved_ms)

    def record_normal_path(self) -> None:
        with self._lock:
            self.normal_path_total += 1

    def record_critical_near_miss(self) -> None:
        with self._lock:
            self.critical_near_misses += 1

    def record_dispatch(self) -> None:
        with self._lock:
            self.dispatches_total += 1

    def record_discard(self, reason: str) -> None:
        with self._lock:
            self.discards_total += 1
            self.discards_by_reason[reason] += 1

    def record_replay_rejected(self) -> None:
        with self._lock:
            self.replays_rejected_total += 1

    # ------------------------------------------------------------------
    # Snapshot (for /metrics endpoint)
    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            avg_saved = (
                self.critical_path_saved_ms_total / self.critical_path_total
                if self.critical_path_total
                else 0.0
            )
            return {
                "critical_path_total": self.critical_path_total,
                "critical_path_avg_saved_ms": round(avg_saved, 2),
                "critical_near_misses": self.critical_near_misses,
                "normal_path_total": self.normal_path_total,
                "readings_ingested_total": self.readings_ingested_total,
                "incidents_detected_total": self.incidents_detected_total,
                "dispatches_total": self.dispatches_total,
                "discards_total": self.discards_total,
                "replays_rejected_total": self.replays_rejected_total,
                "discards_by_reason": dict(self.discards_by_reason),
            }


# Module-level singleton
METRICS = Metrics()