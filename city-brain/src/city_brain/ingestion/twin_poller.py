"""Pull loop: the Twin only answers requests (twin-interface-0.1), so the Brain polls it.

Every `TWIN_POLL_INTERVAL_S` it reads GET /api/v1/sensors (latest reading per sensor),
skips samples it has already seen (same seq), and hands new ones to the TwinPipeline.
Every few seconds it also refreshes city state (blocked roads / alerts) and the
Guardian's trust view of every catalog device for the dashboard.
"""

from __future__ import annotations

import asyncio
import logging
import time

from city_brain import catalog
from city_brain.config import settings
from city_brain.pipeline.twin_pipeline import TwinPipeline

log = logging.getLogger(__name__)


class TwinPoller:
    def __init__(self, pipeline: TwinPipeline, interval: float | None = None) -> None:
        self.p = pipeline
        self.interval = interval or settings.twin_poll_interval_s
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._tick = 0
        self._inflight: set[asyncio.Task] = set()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="twin-poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def poll_once(self) -> int:
        env = await self.p.twin.get_sensor_readings()
        st = self.p.state.twin_status
        if not env.get("ok"):
            err = env.get("error") or {}
            st.update(connected=False, error=f"{err.get('code')}: {err.get('message')}", last_error_at=time.time(),
                      errors_in_a_row=st.get("errors_in_a_row", 0) + 1)
            return 0
        data = env.get("data") or {}
        st.update(connected=True, error=None, last_ok_at=time.time(), errors_in_a_row=0,
                  run_id=env.get("run_id"), sim_tick=data.get("sim_tick"), sensors=data.get("count"))
        self.p.on_run(env.get("run_id"))
        rows = [r for r in data.get("readings", []) if isinstance(r, dict)]
        # Never wait for verdicts here: the Twin only serves the LATEST reading per sensor, so a
        # slow Guardian lookup on one sensor must not make the Brain miss another sensor's sample.
        # Per-sensor locks inside the pipeline keep each sensor's samples in order.
        for r in rows:
            t = asyncio.create_task(self.p.process_twin_row(r, env.get("run_id")))
            self._inflight.add(t)
            t.add_done_callback(self._inflight.discard)
        return len(rows)

    async def drain(self) -> None:
        """Wait for in-flight readings (tests)."""
        while self._inflight:
            await asyncio.gather(*list(self._inflight), return_exceptions=True)

    async def refresh_context(self) -> None:
        env = await self.p.twin.get_city_state()
        if env.get("ok"):
            data = env.get("data") or {}
            alerts = [a for a in data.get("active_incidents", []) if isinstance(a, str)]
            self.p.state.alerts = alerts
            self.p.state.blocked_roads = {a.removeprefix("ROAD_BLOCKED_") for a in alerts if a.startswith("ROAD_BLOCKED_")}
        g = await self.p.guardian.health()
        self.p.state.guardian_status = {"connected": bool(g.get("reachable")), **g}
        if g.get("reachable"):
            ids = list(catalog.sensors()) + [a for a in catalog.actuators() if catalog.actuators()[a].family == "pump"]
            results = await asyncio.gather(*(self.p.guardian.device_trust(d) for d in ids), return_exceptions=True)
            for d, t in zip(ids, results, strict=True):
                if isinstance(t, dict):
                    self.p.state.trust[d] = t

    async def _run(self) -> None:
        await asyncio.sleep(1.0)                 # let the server finish starting (built-in simulator shares its port)
        while not self._stop.is_set():
            try:
                await self.poll_once()
                if self.p.vault.pending() and self.p.state.twin_status.get("connected"):
                    await self.p.retry_offline()              # link is back: deliver sealed decisions
                if self._tick % 3 == 0:
                    await self.refresh_context()
            except Exception as e:                                 # noqa: BLE001 - the loop must survive
                log.warning("twin poll loop error: %s", e)
            self._tick += 1
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except TimeoutError:
                pass
