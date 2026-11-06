"""FastAPI application entrypoint.

Preloads the decision model, connects to PostgreSQL if available, and starts polling the
Twin through its partner contract.

With SIMULATOR=true (the default) the Brain is fully standalone: it serves its own built-in
Twin and Guardian simulators at /sim/twin and /sim/guardian on the same port and talks to
them. Nothing else is started and nothing outside this program is contacted.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from city_brain.api import twin_routes
from city_brain.api.routes import router
from city_brain.audit.db import audit_db
from city_brain.config import settings
from city_brain.decision.model import get_model
from city_brain.ingestion.twin_poller import TwinPoller
from city_brain.pipeline.twin_pipeline import TwinPipeline

logging.basicConfig(level=settings.log_level)
logging.getLogger("httpx").setLevel(logging.WARNING)   # one line per partner call is too noisy
log = logging.getLogger(__name__)

if settings.simulator:
    base = f"http://127.0.0.1:{settings.port}"
    settings.twin_url = f"{base}/sim/twin"
    settings.guardian_url = f"{base}/sim/guardian"
    settings.twin_transport = "rest"
    settings.mock_mode = True
    os.environ["SIM_TWIN_URL"] = settings.twin_url        # the Guardian simulator polls the Twin simulator
    from city_brain.simulator import guardian as sim_guardian
    from city_brain.simulator import twin as sim_twin


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("City Brain starting...")
    sim_tasks: list[asyncio.Task] = []
    if settings.simulator:
        sim_twin.CITY.step()
        sim_tasks.append(asyncio.create_task(sim_twin._ticker(), name="sim-twin"))
        sim_tasks.append(asyncio.create_task(sim_guardian._loop(), name="sim-guardian"))
        log.info("built-in Twin and Guardian simulators running at %s and %s", settings.twin_url, settings.guardian_url)

    m = get_model()
    log.info("decision model preloaded ready=%s path=%s", m.ready, m.path)

    try:
        await audit_db.connect()
        log.info("audit db ready events=%d", await audit_db.count())
    except Exception as e:
        log.warning("audit db unavailable, running in memory-only mode: %s", e)

    pipeline = TwinPipeline()
    poller = TwinPoller(pipeline)
    twin_routes.bind(pipeline, poller)
    if settings.twin_poll_enabled:
        poller.start()
        log.info("polling Twin at %s (%s), Guardian at %s (%s mode)",
                 settings.twin_url, settings.twin_transport, settings.guardian_url, settings.guardian_mode)

    yield

    await poller.stop()
    for t in sim_tasks:
        t.cancel()
    await audit_db.close()
    log.info("City Brain stopping")


app = FastAPI(title="City Brain (Twin/Guardian-ready)", version="0.2.0", lifespan=lifespan)
app.include_router(twin_routes.router)
app.include_router(router)
if settings.simulator:
    app.mount("/sim/twin", sim_twin.app)
    app.mount("/sim/guardian", sim_guardian.app)
