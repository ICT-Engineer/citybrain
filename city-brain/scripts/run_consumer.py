"""Standalone consumer service. ARCHITECTURE.md §7.

Runs outside the FastAPI app. Fail-fast if Redis is unreachable:
exit with non-zero status so a process supervisor (docker-compose,
k8s, systemd) can restart it.

Usage:
    python scripts/run_consumer.py
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from city_brain.config import settings
from city_brain.ingestion.stream import RedisStreamConsumer
from city_brain.pipeline.orchestrator import Pipeline

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("consumer")


async def _main() -> int:
    from city_brain.audit.db import audit_db

    # Connect to PostgreSQL first (the consumer writes its own audit trail).
    try:
        await audit_db.connect()
        log.info("audit db connected events=%d", await audit_db.count())
    except Exception as e:
        log.error("audit db unavailable: %s", e)
        log.error("PostgreSQL is required for audit; exiting with code 1")
        return 1

    consumer = RedisStreamConsumer()
    pipeline = Pipeline()

    try:
        await consumer.start()
    except Exception as e:
        log.error("failed to start consumer: %s", e)
        log.error("Redis is required for ingestion; exiting with code 1")
        return 1

    # Graceful shutdown on Ctrl+C
    stop_event = asyncio.Event()

    def _signal_handler(*_):
        log.info("shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows: fall back to KeyboardInterrupt handling
            pass

    async def _handle(reading):
        try:
            result = await pipeline.process(reading)
            log.info(
                "processed corr=%s state=%s kind=%s",
                result.correlation_id, result.state.value, result.incident_kind,
            )
        except Exception as e:
            log.error("pipeline error corr=%s err=%s", reading.correlation_id, e)

    log.info("consumer started, waiting for readings on stream=%s", settings.sensor_stream)
    try:
        await consumer.run_forever(_handle)
    except KeyboardInterrupt:
        log.info("keyboard interrupt")
    finally:
        await consumer.stop()
        await audit_db.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_main()))
    except KeyboardInterrupt:
        sys.exit(0)