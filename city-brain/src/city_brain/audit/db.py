"""PostgreSQL connection pool + schema for the audit log.

Creates the `audit_log` table on first startup if it doesn't exist.
Uses asyncpg directly (no ORM) — the schema is small and stable.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg

from city_brain.config import settings

log = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id              BIGSERIAL PRIMARY KEY,
    correlation_id  TEXT        NOT NULL,
    event_type      TEXT        NOT NULL,
    payload         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS audit_log_correlation_id_idx ON audit_log (correlation_id);
CREATE INDEX IF NOT EXISTS audit_log_event_type_idx     ON audit_log (event_type);
"""


class AuditDB:
    """Thin wrapper around an asyncpg connection pool."""

    def __init__(self, dsn: str | None = None) -> None:
        self.dsn = dsn or settings.database_url
        self._pool: asyncpg.Pool | None = None

    @property
    def ready(self) -> bool:
        return self._pool is not None

    async def connect(self) -> None:
        """Create the pool and ensure the schema exists."""
        if self._pool is not None:
            return
        self._pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=5)
        async with self._pool.acquire() as conn:
            await conn.execute(_SCHEMA)
        log.info("audit db connected dsn=%s", self._redacted())

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def insert(
        self, correlation_id: str, event_type: str, payload: dict[str, Any]
    ) -> None:
        if self._pool is None:
            log.warning("audit db not connected; dropping event %s", event_type)
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO audit_log (correlation_id, event_type, payload) "
                "VALUES ($1, $2, $3::jsonb)",
                correlation_id,
                event_type,
                json.dumps(payload, default=str),
            )

    async def fetch_trace(self, correlation_id: str) -> list[dict[str, Any]]:
        if self._pool is None:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, correlation_id, event_type, payload, ts "
                "FROM audit_log WHERE correlation_id = $1 ORDER BY id ASC",
                correlation_id,
            )
            return [
                {
                    "id": r["id"],
                    "correlation_id": r["correlation_id"],
                    "event_type": r["event_type"],
                    "payload": json.loads(r["payload"]) if isinstance(r["payload"], str) else dict(r["payload"]),
                    "ts": r["ts"].isoformat(),
                }
                for r in rows
            ]

    async def count(self) -> int:
        if self._pool is None:
            return 0
        async with self._pool.acquire() as conn:
            return await conn.fetchval("SELECT COUNT(*) FROM audit_log")

    def _redacted(self) -> str:
        """Hide password from log."""
        dsn = self.dsn
        if "@" in dsn and ":" in dsn.split("@")[0]:
            head, tail = dsn.split("@", 1)
            scheme_user = head.rsplit(":", 1)[0]
            return f"{scheme_user}:***@{tail}"
        return dsn


# Module-level singleton
audit_db = AuditDB()