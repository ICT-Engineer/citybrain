"""HTTP client for Guardian's /verify endpoint.

Fail-safe: any error (timeout, 5xx, network) returns None.
The TrustGate treats None as "unreachable" -> deny. §8.
"""

from __future__ import annotations

import logging

import httpx

from city_brain.config import settings
from city_brain.schemas.guardian import GuardianVerdict
from city_brain.schemas.twin import SensorReading

log = logging.getLogger(__name__)


class GuardianClient:
    def __init__(self, base_url: str | None = None, timeout: float = 5.0) -> None:
        self.base_url = (base_url or settings.guardian_url).rstrip("/")
        self.timeout = timeout

    async def verify(self, reading: SensorReading) -> GuardianVerdict | None:
        url = f"{self.base_url}/verify"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(url, json={"reading": reading.model_dump(mode="json")})
                r.raise_for_status()
                return GuardianVerdict.model_validate(r.json())
        except httpx.HTTPError as e:
            log.warning("guardian.verify failed: %s", e)
            return None
        except Exception as e:
            log.warning("guardian.verify unexpected error: %s", e)
            return None