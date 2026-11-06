"""HTTP client for Twin's actuator dispatch endpoint."""

from __future__ import annotations

import logging

import httpx

from city_brain.config import settings
from city_brain.schemas.twin import ActionAck, DispatchCommand

log = logging.getLogger(__name__)


class TwinClient:
    def __init__(self, base_url: str | None = None, timeout: float = 5.0) -> None:
        self.base_url = (base_url or settings.twin_url).rstrip("/")
        self.timeout = timeout

    async def dispatch(self, command: DispatchCommand) -> ActionAck | None:
        url = f"{self.base_url}/actuator/dispatch"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(url, json=command.model_dump(mode="json"))
                r.raise_for_status()
                return ActionAck.model_validate(r.json())
        except httpx.HTTPError as e:
            log.warning("twin.dispatch failed actuator=%s: %s", command.actuator_id, e)
            return None
        except Exception as e:
            log.warning("twin.dispatch unexpected error: %s", e)
            return None