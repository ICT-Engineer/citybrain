"""Client for the Twin layer's partner contract (twin-interface-0.1).

Transports (same tools, same envelope, same security on the Twin side):
  - "rest": GET /api/v1/sensors, /api/v1/state, /api/v1/events?seq=N, POST /api/v1/commands
  - "mcp" : FastMCP tools get_sensor_readings / get_city_state / get_events_since /
            send_actuator_command over SSE (the Twin's `run_twin.py` shares this owner
            with its dashboard).

Every call sends `Authorization: Bearer <brain token>` (the Twin refuses tokens in
URLs for partner tools). Every response is the Twin envelope:
    {"ok": bool, "run_id": str, "data": {...}, "error": {"code","message"} | null, "timestamp": str}
Network failures never raise into the pipeline: they come back as an envelope with
error code TWIN_UNREACHABLE, so the Brain holds instead of guessing (SCN-06).
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

import httpx

from city_brain.config import settings

log = logging.getLogger(__name__)


# Simulated network fault for demos: "uplink" = commands Brain -> Twin cannot get through.
NETWORK = {"uplink_down": False}


def _failure(code: str, message: str) -> dict:
    return {"ok": False, "run_id": None, "data": None, "error": {"code": code, "message": message}, "timestamp": None}


def request_id_for(correlation_id: str, device_id: str, command: str, params: dict) -> str:
    """Deterministic idempotency key: a retry of the same decision never applies physics twice,
    while a different command or parameter set gets a new key (avoids DUPLICATE_CONFLICT)."""
    blob = json.dumps({"c": correlation_id, "d": device_id, "m": command, "p": params}, sort_keys=True)
    return "brain-" + hashlib.sha256(blob.encode()).hexdigest()[:24]


class TwinContractClient:
    def __init__(self, base_url: str | None = None, token: str | None = None,
                 transport: str | None = None, mcp_url: str | None = None, timeout: float = 5.0) -> None:
        self.base_url = (base_url or settings.twin_url).rstrip("/")
        self.token = settings.twin_token_brain if token is None else token
        self.transport = (transport or settings.twin_transport).lower()
        self.mcp_url = mcp_url or settings.twin_mcp_url
        self.timeout = timeout

    # ------------------------------------------------------------------ helpers
    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def _rest(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None,
                    extra_headers: dict | None = None) -> dict:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.request(method, f"{self.base_url}{path}", params=params, json=body,
                                    headers={**self._headers, **(extra_headers or {})})
            try:
                env = r.json()
            except ValueError:
                return _failure("BAD_RESPONSE", f"HTTP {r.status_code}, body is not JSON")
            if not isinstance(env, dict) or "ok" not in env:
                return _failure("BAD_RESPONSE", f"HTTP {r.status_code}, not a Twin envelope")
            return env
        except httpx.HTTPError as e:
            log.warning("twin %s %s failed: %s", method, path, e)
            return _failure("TWIN_UNREACHABLE", str(e) or e.__class__.__name__)

    async def _mcp(self, tool: str, arguments: dict) -> dict:
        try:
            from mcp import ClientSession
            from mcp.client.sse import sse_client
        except ImportError:                                           # optional dependency
            return _failure("MCP_NOT_INSTALLED", "pip install mcp to use TWIN_TRANSPORT=mcp")
        try:
            async with sse_client(self.mcp_url, headers=self._headers, timeout=self.timeout) as (rd, wr):
                async with ClientSession(rd, wr) as session:
                    await session.initialize()
                    res = await session.call_tool(tool, arguments)
            if getattr(res, "structuredContent", None):
                sc = res.structuredContent
                return sc.get("result", sc) if isinstance(sc, dict) else _failure("BAD_RESPONSE", "structured content")
            for item in res.content:
                text = getattr(item, "text", None)
                if text:
                    return json.loads(text)
            return _failure("BAD_RESPONSE", "empty MCP result")
        except Exception as e:                                        # noqa: BLE001 - never raise into the pipeline
            log.warning("twin MCP %s failed: %s", tool, e)
            return _failure("TWIN_UNREACHABLE", str(e) or e.__class__.__name__)

    # ------------------------------------------------------------------ tools
    async def get_sensor_readings(self, zone_id: str | None = None, sensor_id: str | None = None,
                                  sensor_type: str | None = None) -> dict:
        args = {k: v for k, v in {"zone_id": zone_id, "sensor_id": sensor_id, "sensor_type": sensor_type}.items() if v}
        if self.transport == "mcp":
            return await self._mcp("get_sensor_readings", args)
        return await self._rest("GET", "/api/v1/sensors", params=args or None)

    async def get_city_state(self, zone_id: str | None = None) -> dict:
        args = {"zone_id": zone_id} if zone_id else {}
        if self.transport == "mcp":
            return await self._mcp("get_city_state", args)
        return await self._rest("GET", "/api/v1/state", params=args or None)

    async def get_events_since(self, seq: int = 0) -> dict:
        if self.transport == "mcp":
            return await self._mcp("get_events_since", {"seq": seq})
        return await self._rest("GET", "/api/v1/events", params={"seq": seq})

    async def send_actuator_command(self, device_id: str, command: str, params: dict[str, Any],
                                    request_id: str, context: dict[str, Any] | None = None) -> dict:
        from city_brain.security.command_shield import get_shield
        shield = get_shield()
        blocked = shield.check(device_id, command, params, request_id)            # validation + rate limit, before anything leaves
        if blocked:
            return _failure(f"BLOCKED_BY_SHIELD:{blocked}", f"{device_id} {command} {params} refused locally ({blocked})")
        body = {"device_id": device_id, "command": command, "params": params, "request_id": request_id}
        if context:
            body["context"] = context
        if NETWORK["uplink_down"]:
            return _failure("TWIN_UNREACHABLE", "uplink to the Twin is down (simulated network cut)")
        if self.transport == "mcp":
            return await self._mcp("send_actuator_command", body)
        path = "/api/v1/commands"
        signed_path = (httpx.URL(self.base_url).path.rstrip("/") + path)
        return await self._rest("POST", path, body=body, extra_headers=shield.headers("POST", signed_path, body))

    async def health(self) -> dict:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.get(f"{self.base_url}/health/ready")
            body = r.json()
            data = body.get("data", body) if isinstance(body, dict) else {}
            return {"reachable": True, "http": r.status_code, "status": data.get("status")}
        except Exception as e:                                        # noqa: BLE001
            return {"reachable": False, "error": str(e) or e.__class__.__name__}
