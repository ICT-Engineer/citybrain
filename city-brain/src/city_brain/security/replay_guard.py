"""Replay protection for incoming readings. ARCHITECTURE.md §8.

Detects a reading that pretends to be new but carries a `seq` that is
not strictly greater than the last seq seen for that sensor. Catches
naive replay attacks where an attacker resends an old signed reading.

State is in-memory per process. For multi-worker deployments this
should move to Redis (TODO for v2).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from city_brain.schemas.twin import SensorReading

log = logging.getLogger(__name__)


@dataclass
class ReplayCheckResult:
    allowed: bool
    reason: str = "ok"
    last_seq: int | None = None
    incoming_seq: int | None = None


class ReplayGuard:
    """Tracks the highest `seq` seen per sensor_id."""

    def __init__(self) -> None:
        self._last_seq: dict[str, int] = {}

    def check(self, reading: SensorReading) -> ReplayCheckResult:
        """Return allowed=False if the reading's seq is stale or repeated."""
        sid = reading.sensor_id
        incoming = reading.seq
        previous = self._last_seq.get(sid)

        if previous is not None and incoming <= previous:
            log.warning(
                "replay detected sensor=%s incoming_seq=%s last_seq=%s corr=%s",
                sid, incoming, previous, reading.correlation_id,
            )
            return ReplayCheckResult(
                allowed=False,
                reason=f"replay: seq {incoming} <= last {previous}",
                last_seq=previous,
                incoming_seq=incoming,
            )

        self._last_seq[sid] = incoming
        return ReplayCheckResult(
            allowed=True,
            reason="ok",
            last_seq=previous,
            incoming_seq=incoming,
        )

    def reset(self) -> None:
        """Test helper."""
        self._last_seq.clear()