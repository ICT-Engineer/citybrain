"""Mock Guardian — fake trust verifier for local testing.

Port 8001. Chaos mode via env var GUARDIAN_CHAOS:
  off        -> trusted, 0.95  (default)
  suspicious -> suspicious, 0.5
  rejected   -> rejected, 0.1
  slow       -> sleeps 30s then trusted  (tests timeouts)
  down       -> returns HTTP 500          (tests fail-safe default)
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from city_brain.schemas.guardian import GuardianVerdict, Verdict
from city_brain.schemas.twin import SensorReading

logging.basicConfig(level="INFO")
log = logging.getLogger("guardian_mock")

app = FastAPI(title="Mock Guardian", version="0.1.0")


class VerifyRequest(BaseModel):
    reading: SensorReading


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "role": "guardian-mock", "chaos": os.getenv("GUARDIAN_CHAOS", "off")}


@app.post("/verify", response_model=GuardianVerdict)
async def verify(req: VerifyRequest) -> GuardianVerdict:
    chaos = os.getenv("GUARDIAN_CHAOS", "off").lower()
    log.info("verify sensor=%s chaos=%s", req.reading.sensor_id, chaos)

    if chaos == "down":
        raise HTTPException(status_code=500, detail="chaos: guardian down")

    if chaos == "slow":
        await asyncio.sleep(30)

    if chaos == "rejected":
        verdict, score = Verdict.REJECTED, 0.1
    elif chaos == "suspicious":
        verdict, score = Verdict.SUSPICIOUS, 0.5
    else:
        verdict, score = Verdict.TRUSTED, 0.95

    return GuardianVerdict(
        correlation_id=req.reading.correlation_id,
        sensor_id=req.reading.sensor_id,
        ts=req.reading.ts,
        seq=req.reading.seq,
        trust_score=score,
        verdict=verdict,
        sig=f"guardian-mock-{req.reading.correlation_id}",
        reason_codes=[f"chaos={chaos}"],
    )