"""Client for the Guardian layer's contract (aya883/guardian_layer, interface v0.3/v0.4).

Brain-facing endpoints (bearer = their GUARDIAN_TOKEN_BRAIN, read-only):
  GET  /verdicts/{observation_id}  -> Verdict  (404 OBSERVATION_UNKNOWN until judged)
  POST /trust/verdict {observation_id} (same answer, body form)
  GET  /devices/{device_id}/trust  -> {trust_score, quarantined, stale, ...} (404 DEVICE_UNKNOWN)
  POST /observations               -> Verdict  (push mode; needs an ingest-capable token)
Shared screen (admin token): POST /timeline/events {"timestamp","layer":"brain","event",...}

Fail-safe (Guardian INTERFACE: "503 ... the caller must treat it as UNTRUSTED"):
any timeout, 5xx, 401 or network error returns None and the TrustGate denies.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime

import httpx

from city_brain.config import settings
from city_brain.schemas.guardian import GuardianVerdict
from city_brain.schemas.twin import SensorReading

log = logging.getLogger(__name__)


class GuardianContractClient:
    def __init__(self, base_url: str | None = None, token: str | None = None,
                 timeline_token: str | None = None, mode: str | None = None,
                 wait_s: float | None = None, timeout: float = 3.0) -> None:
        self.base_url = (base_url or settings.guardian_url).rstrip("/")
        self.token = settings.guardian_token_brain if token is None else token
        self.timeline_token = settings.guardian_timeline_token if timeline_token is None else timeline_token
        self.mode = (mode or settings.guardian_mode).lower()
        self.wait_s = settings.guardian_verdict_wait_s if wait_s is None else wait_s
        self.timeout = timeout
        self.last_error: str | None = None

    def _h(self, token: str | None) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"} if token else {}

    # ------------------------------------------------------------------ verdicts
    async def verify(self, reading: SensorReading) -> GuardianVerdict | None:
        """Return the Guardian verdict for this reading, or None (treated as UNTRUSTED)."""
        if self.mode == "push":
            return await self._push(reading)
        return await self._lookup(reading)

    async def _lookup(self, reading: SensorReading) -> GuardianVerdict | None:
        obs = reading.observation_id
        if not obs:
            self.last_error = "reading has no observation_id"
            return None
        deadline = time.monotonic() + self.wait_s
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                while True:
                    r = await c.get(f"{self.base_url}/verdicts/{obs}", headers=self._h(self.token))
                    if r.status_code == 200:
                        self.last_error = None
                        return GuardianVerdict.from_guardian_layer(r.json(), reading.correlation_id, reading.seq)
                    if r.status_code != 404 or time.monotonic() >= deadline:
                        self.last_error = f"HTTP {r.status_code}"
                        return None
                    await asyncio.sleep(0.25)              # Guardian polls the Twin ~1/s: not judged yet
        except httpx.HTTPError as e:
            self.last_error = str(e) or e.__class__.__name__
            log.warning("guardian lookup failed obs=%s: %s", obs, self.last_error)
            return None

    async def _push(self, reading: SensorReading) -> GuardianVerdict | None:
        if not isinstance(reading.value, (int, float)):
            self.last_error = "push mode needs a numeric value"
            return None
        body = {
            "observation_id": reading.observation_id or reading.correlation_id,
            "device_id": reading.sensor_id,
            "sensor_type": reading.twin_type or reading.type.value,
            "zone": reading.zone,
            "value": float(reading.value),
            "unit": reading.unit or "",
            "timestamp": datetime.now(UTC).isoformat(),
            "seq": reading.seq,
            "tick": reading.sim_tick,
            "backup_ids": reading.backup_ids[:4],
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(f"{self.base_url}/observations", json=body, headers=self._h(self.token))
            if r.status_code == 200:
                self.last_error = None
                return GuardianVerdict.from_guardian_layer(r.json(), reading.correlation_id, reading.seq)
            self.last_error = f"HTTP {r.status_code}"
            return None
        except httpx.HTTPError as e:
            self.last_error = str(e) or e.__class__.__name__
            return None

    async def device_trust(self, device_id: str) -> dict | None:
        """Guardian's view of a device; None when unknown or unreachable."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.get(f"{self.base_url}/devices/{device_id}/trust", headers=self._h(self.token))
            return r.json() if r.status_code == 200 else None
        except httpx.HTTPError:
            return None

    async def is_quarantined(self, device_id: str) -> bool:
        t = await self.device_trust(device_id)
        return bool(t and t.get("quarantined"))

    # ------------------------------------------------------------------ shared timeline
    async def post_timeline(self, event: str, **fields) -> None:
        if not settings.guardian_timeline_enabled:
            return
        body = {"timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "layer": "brain", "event": event, **fields}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                await c.post(f"{self.base_url}/timeline/events", json=body, headers=self._h(self.timeline_token))
        except httpx.HTTPError:
            pass                                            # the timeline is best-effort, never blocks a decision

    async def health(self) -> dict:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.get(f"{self.base_url}/health")
            return {"reachable": r.status_code == 200, "http": r.status_code, **(r.json() if r.status_code == 200 else {})}
        except Exception as e:                              # noqa: BLE001
            return {"reachable": False, "error": str(e) or e.__class__.__name__}
