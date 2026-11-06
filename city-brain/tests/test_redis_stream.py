"""Integration tests for the Redis Streams consumer.

These tests need a running Redis. They skip automatically if Redis
is unavailable, so `pytest` stays green in environments without it.

    docker compose up -d redis
    pytest tests/test_redis_stream.py -v
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest

from city_brain.config import settings
from city_brain.ingestion.stream import RedisStreamConsumer, publish_reading
from city_brain.schemas.twin import (
    EmergencyCallPayload,
    SensorReading,
    SensorType,
    SeverityClass,
)


def _redis_available() -> bool:
    try:
        import redis
        r = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=1)
        r.ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _redis_available(),
    reason="Redis not available (run: docker compose up -d redis)",
)


def _reading(corr: str = "corr-test-redis-1", seq: int = 1) -> SensorReading:
    return SensorReading(
        sensor_id="redis-test-1",
        type=SensorType.EMERGENCY_CALL,
        value=None,
        unit=None,
        ts=datetime.now(UTC),
        seq=seq,
        sig="sig-test",
        severity_class=SeverityClass.CRITICAL,
        correlation_id=corr,
        zone="downtown",
        emergency=EmergencyCallPayload(incident_type="accident"),
    )


@pytest.mark.asyncio
async def test_publish_and_consume():
    """Publish a reading, then read it back through a fresh consumer group."""
    stream = f"test.stream.{uuid.uuid4().hex[:8]}"
    corr = f"corr-{uuid.uuid4().hex[:8]}"

    msg_id = await publish_reading(_reading(corr=corr), stream=stream)
    assert msg_id is not None

    c = RedisStreamConsumer(
        stream=stream,
        group=f"test-group-{uuid.uuid4().hex[:6]}",
        consumer="test-consumer",
        block_ms=500,
        batch=10,
    )
    await c.start()

    received: list[SensorReading] = []

    async def _handler(r: SensorReading) -> None:
        received.append(r)
        # Schedule stop outside the current coroutine
        asyncio.get_running_loop().call_soon(lambda: setattr(c, "_running", False))

    # Hard timeout so the test never hangs
    async def _run():
        await c.run_forever(_handler)

    try:
        await asyncio.wait_for(_run(), timeout=10.0)
    except TimeoutError:
        pytest.fail("run_forever did not stop within 10s")

    await c.stop()

    assert len(received) == 1
    assert received[0].correlation_id == corr
    assert received[0].sensor_id == "redis-test-1"


@pytest.mark.asyncio
async def test_consumer_start_fails_without_redis():
    """Consumer.start() must raise if Redis is unreachable."""
    c = RedisStreamConsumer(url="redis://localhost:9999/0")
    with pytest.raises(Exception):  # noqa: B017  (any connection error is fine)
        await c.start()