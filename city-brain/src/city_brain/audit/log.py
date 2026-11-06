"""Audit logger: console + PostgreSQL.

ARCHITECTURE.md §7 and §8 require a durable audit trail. This logger
writes every event to:
  1. the standard Python log (still useful for tail -f)
  2. the `audit_log` table in PostgreSQL (source of truth for evaluation)
"""

from __future__ import annotations

import logging
from typing import Any

from city_brain.audit.db import audit_db

log = logging.getLogger(__name__)


class AuditLogger:
    """Writes audit events to console AND Postgres."""

    async def record(
        self,
        event_type: str,
        correlation_id: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        payload = payload or {}

        # 1. Console
        log.info("AUDIT %s correlation_id=%s payload=%s", event_type, correlation_id, payload)

        # 2. Postgres
        try:
            await audit_db.insert(correlation_id, event_type, payload)
        except Exception as e:
            # Never let audit failure break the pipeline — log and continue.
            log.error("audit db insert failed: %s", e)