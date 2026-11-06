from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Verdict(StrEnum):
    TRUSTED = "trusted"
    SUSPICIOUS = "suspicious"
    REJECTED = "rejected"


class GuardianVerdict(BaseModel):
    """Brain-internal verdict. Built from either the original mock shape or the
    real Guardian layer's Verdict (see `from_guardian_layer`)."""

    model_config = ConfigDict(extra="forbid")
    correlation_id: str
    sensor_id: str
    ts: datetime
    seq: int
    trust_score: float = Field(..., ge=0.0, le=1.0)
    verdict: Verdict
    sig: str
    reason_codes: list[str] = Field(default_factory=list)
    # Extra fields carried from the Guardian layer (optional)
    verdict_id: str | None = None
    observation_id: str | None = None
    risk_level: str | None = None
    recommended_action: str | None = None

    @classmethod
    def from_guardian_layer(cls, body: dict, correlation_id: str, seq: int = 0) -> GuardianVerdict:
        """Map aya883/guardian_layer `Verdict` JSON to the Brain model.

        TRUSTED -> trusted. UNTRUSTED only because of a *message* problem (replay, flood,
        forged timestamp, malformed payload: the Guardian leaves the device's trust untouched)
        -> suspicious. Any UNTRUSTED that says the *data* is false -> rejected. The real
        Guardian may recommend MONITOR on a first BACKUP_DISAGREEMENT, so the reason codes,
        not recommended_action, decide. Either way the TrustGate refuses anything not trusted.
        """
        raw = str(body.get("verdict", "")).upper()
        codes = {str(c) for c in body.get("reason_codes", [])}
        message_only = {"REPLAY_OR_OUT_OF_ORDER", "MESSAGE_FLOOD", "FUTURE_TIMESTAMP", "MALFORMED_PAYLOAD"}
        if raw == "TRUSTED":
            v = Verdict.TRUSTED
        elif codes and codes <= message_only | {c for c in codes if c.startswith("MALFORMED")}:
            v = Verdict.SUSPICIOUS
        else:
            v = Verdict.REJECTED
        issued = body.get("issued_at")
        try:
            ts = datetime.fromisoformat(str(issued).replace("Z", "+00:00")) if issued else datetime.now(UTC)
        except ValueError:
            ts = datetime.now(UTC)
        score = float(body.get("trust_score", 0.0))
        return cls(
            correlation_id=correlation_id,
            sensor_id=str(body.get("device_id", "")),
            ts=ts,
            seq=seq,
            trust_score=min(1.0, max(0.0, score)),
            verdict=v,
            sig=str(body.get("verdict_id", "")),
            reason_codes=[str(c) for c in body.get("reason_codes", [])],
            verdict_id=body.get("verdict_id"),
            observation_id=body.get("observation_id"),
            risk_level=body.get("risk_level"),
            recommended_action=body.get("recommended_action"),
        )
