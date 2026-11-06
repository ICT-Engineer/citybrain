"""Redis Streams consumer for the Twin's sensor stream.

ARCHITECTURE.md §7: subscribe to a Redis Stream with a persisted
consumer group so a restart resumes from where it left off.

The consumer calls `on_reading(reading)` for every message. Failures
of individual readings are logged and the message is acked — we do not
want a poison message to block the stream forever.
"""

from __future__ import annotations

import abc
import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

import redis.asyncio as aioredis

from city_brain.config import settings
from city_brain.schemas.twin import SensorReading

log = logging.getLogger(__name__)

OnReading = Callable[[SensorReading], Awaitable[None]]


class SensorStreamConsumer(abc.ABC):
    """Abstract source of SensorReading objects."""

    @abc.abstractmethod
    async def start(self) -> None: ...

    @abc.abstractmethod
    async def stop(self) -> None: ...

    @abc.abstractmethod
    async def run_forever(self, on_reading: OnReading) -> None: ...


class RedisStreamConsumer(SensorStreamConsumer):
    """Consumer group reader over a Redis Stream.

    Usage:
        c = RedisStreamConsumer()
        await c.start()             # connects + creates group
        await c.run_forever(handler)
    """

    def __init__(
        self,
        url: str | None = None,
        stream: str | None = None,
        group: str | None = None,
        consumer: str | None = None,
        block_ms: int = 5000,
        batch: int = 10,
    ) -> None:
        self.url = url or settings.redis_url
        self.stream = stream or settings.sensor_stream
        self.group = group or settings.consumer_group
        self.consumer = consumer or settings.consumer_name
        self.block_ms = block_ms
        self.batch = batch
        self._redis: aioredis.Redis | None = None
        self._running = False

    async def start(self) -> None:
        """Connect and ensure the consumer group exists.

        Raises ConnectionError if Redis is unreachable — the caller
        (run_consumer.py) treats that as fatal and exits.
        """
        self._redis = aioredis.from_url(self.url, decode_responses=True)
        # Fail fast if we cannot reach Redis.
        await self._redis.ping()

        # Create the stream + group if needed (MKSTREAM).
        # id="0" means: start from the earliest unread message. This is
        # the right behavior for a persisted consumer group — a restart
        # resumes from where it left off, it does not skip messages.
        try:
            await self._redis.xgroup_create(
                name=self.stream,
                groupname=self.group,
                id="0",
                mkstream=True,
            )
            log.info("consumer group created stream=%s group=%s", self.stream, self.group)
        except aioredis.ResponseError as e:
            if "BUSYGROUP" in str(e):
                log.info("consumer group already exists group=%s", self.group)
            else:
                raise

        self._running = True
        log.info(
            "RedisStreamConsumer ready stream=%s group=%s consumer=%s",
            self.stream, self.group, self.consumer,
        )

    async def stop(self) -> None:
        self._running = False
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    async def run_forever(self, on_reading: OnReading) -> None:
        """Blocking loop: read, parse, dispatch to on_reading, ack."""
        if self._redis is None:
            raise RuntimeError("call start() before run_forever()")

        while self._running:
            try:
                results = await self._redis.xreadgroup(
                    groupname=self.group,
                    consumername=self.consumer,
                    streams={self.stream: ">"},
                    count=self.batch,
                    block=self.block_ms,
                )
            except asyncio.CancelledError:
                # Shutting down
                raise
            except Exception as e:
                log.error("xreadgroup failed: %s", e)
                if not self._running:
                    break
                await asyncio.sleep(1.0)
                continue

            if not results:
                continue

            for _stream_name, messages in results:
                for msg_id, fields in messages:
                    await self._handle(msg_id, fields, on_reading)
                    if not self._running:
                        break
                if not self._running:
                    break

    async def _handle(self, msg_id: str, fields: dict, on_reading: OnReading) -> None:
        """Parse one message and call on_reading. Always ack at the end."""
        assert self._redis is not None
        try:
            payload_raw = fields.get("payload")
            if payload_raw is None:
                log.warning("message without payload field id=%s", msg_id)
                await self._redis.xack(self.stream, self.group, msg_id)
                return

            data = json.loads(payload_raw)
            reading = SensorReading.model_validate(data)
            await on_reading(reading)
        except Exception as e:
            # Do not ack on failure to preserve for inspection? We choose
            # to ack and log — poison messages must not stall the stream.
            log.error("failed to handle message id=%s err=%s", msg_id, e)
        finally:
            await self._redis.xack(self.stream, self.group, msg_id)


async def publish_reading(
    reading: SensorReading,
    url: str | None = None,
    stream: str | None = None,
) -> str:
    """Publish a reading to the Redis stream. Used by the mock Twin."""
    r = aioredis.from_url(url or settings.redis_url, decode_responses=True)
    try:
        msg_id = await r.xadd(
            name=stream or settings.sensor_stream,
            fields={"payload": reading.model_dump_json()},
        )
        return msg_id
    finally:
        await r.aclose()