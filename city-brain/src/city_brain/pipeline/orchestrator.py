"""City Brain pipeline.

ARCHITECTURE.md §3 defines two processing paths:

  Critical (§3.1, parallel):
      Twin sends a `critical` reading to City Brain AND Guardian at the
      same time. We compute the decision immediately (ML model) and ask
      Guardian for a verdict concurrently. Once both finish, we gate:
      if trusted -> dispatch, else -> discard as a "near miss".

  Normal (§3.2, sequential):
      Guardian verifies first. Only if the verdict clears do we compute
      the decision and dispatch.

  Invariant (both paths):
      NOTHING is dispatched without a trusted Guardian verdict attached.

§8 requires a fail-safe default: if Guardian is unreachable or errors,
we take no action (never "assume trusted").
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from city_brain.audit.log import AuditLogger
from city_brain.clients.guardian_client import GuardianClient
from city_brain.clients.twin_client import TwinClient
from city_brain.decision.engine import Decision, decide
from city_brain.decision.state_machine import IncidentState, IncidentStateMachine
from city_brain.detection.rules import DetectedIncident, detect
from city_brain.dispatch.trust_gate import TrustGate

# Live dashboard hook
from city_brain.observability.live_events import log_live_event
from city_brain.observability.metrics import METRICS
from city_brain.schemas.guardian import GuardianVerdict
from city_brain.schemas.twin import ActionAck, SensorReading, SeverityClass
from city_brain.security.replay_guard import ReplayGuard

log = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    correlation_id: str
    state: IncidentState
    path: str = "normal"                        # "critical" or "normal"
    incident_kind: str | None = None
    verdict: GuardianVerdict | None = None
    dispatched: list[ActionAck] = field(default_factory=list)
    discarded_reason: str | None = None
    saved_ms: float = 0.0                       # parallel gain vs sequential (critical only)


class Pipeline:
    def __init__(
        self,
        guardian: GuardianClient | None = None,
        twin: TwinClient | None = None,
        gate: TrustGate | None = None,
        audit: AuditLogger | None = None,
    ) -> None:
        self.guardian = guardian or GuardianClient()
        self.twin = twin or TwinClient()
        self.gate = gate or TrustGate()
        self.audit = audit or AuditLogger()
        self.replay_guard = ReplayGuard()

    # ------------------------------------------------------------------
    # Public entry point — routes to the correct path
    # ------------------------------------------------------------------
    async def process(self, reading: SensorReading) -> PipelineResult:
        METRICS.record_ingest()
        log_live_event("Reading Arrived", f"Sensor: {reading.sensor_id} | Type: {reading.type.value} | Severity: {reading.severity_class.value}", "reading")

        # §8: reject replayed / stale readings before anything else.
        replay = self.replay_guard.check(reading)
        if not replay.allowed:
            METRICS.record_replay_rejected()
            METRICS.record_discard("replay")
            await self.audit.record(
                "replay_rejected",
                reading.correlation_id,
                {
                    "sensor_id": reading.sensor_id,
                    "incoming_seq": replay.incoming_seq,
                    "last_seq": replay.last_seq,
                },
            )
            log.warning(
                "replay rejected corr=%s reason=%s",
                reading.correlation_id,
                replay.reason,
            )
            log_live_event("Replay Rejected", f"Sensor: {reading.sensor_id} | Seq: {replay.incoming_seq} <= {replay.last_seq}", "discard")
            return PipelineResult(
                reading.correlation_id,
                IncidentState.DETECTED,
                discarded_reason="replay_rejected",
            )

        log.info(
            "ingest sensor=%s type=%s severity=%s corr=%s",
            reading.sensor_id, reading.type, reading.severity_class, reading.correlation_id,
        )
        await self.audit.record(
            "reading_received",
            reading.correlation_id,
            {"sensor_id": reading.sensor_id, "type": reading.type.value,
             "severity_class": reading.severity_class.value},
        )

        incident = detect(reading)
        if incident is None:
            log.info("no incident detected corr=%s", reading.correlation_id)
            return PipelineResult(
                reading.correlation_id,
                IncidentState.DETECTED,
                discarded_reason="no_incident",
            )
        METRICS.record_incident()

        if reading.severity_class == SeverityClass.CRITICAL:
            return await self._process_critical(reading, incident)
        return await self._process_normal(reading, incident)

    # ------------------------------------------------------------------
    # §3.1 — Critical path (parallel)
    # ------------------------------------------------------------------
    async def _process_critical(
        self, reading: SensorReading, incident: DetectedIncident
    ) -> PipelineResult:
        sm = IncidentStateMachine(reading.correlation_id)
        log.info("critical path corr=%s", reading.correlation_id)

        # Run decision and verification concurrently.
        t0 = time.perf_counter()

        async def _timed_decide() -> tuple[Decision, float]:
            s = time.perf_counter()
            d = decide(incident, trust_score=0.0, reading=reading)
            return d, (time.perf_counter() - s) * 1000.0

        async def _timed_verify() -> tuple[GuardianVerdict | None, float]:
            s = time.perf_counter()
            v = await self.guardian.verify(reading)
            return v, (time.perf_counter() - s) * 1000.0

        (decision, decide_ms), (verdict, verify_ms) = await asyncio.gather(
            _timed_decide(), _timed_verify()
        )
        parallel_ms = (time.perf_counter() - t0) * 1000.0

        # Reporting: saved_ms = potential gain from parallelism = min(decide, verify).
        saved_ms = max(0.0, min(decide_ms, verify_ms))
        METRICS.record_critical_path(saved_ms)

        log_live_event(
            "Guardian Verdict", 
            f"Verdict: {verdict.verdict.value if verdict else 'unreachable'} | Score: {verdict.trust_score if verdict else 0.0}",
            "verdict"
        )

        log.info(
            "critical parallel: decide=%.1fms verify=%.1fms parallel=%.1fms saved=%.1fms "
            "chosen_by=%s confidence=%.3f",
            decide_ms, verify_ms, parallel_ms, saved_ms,
            decision.chosen_by, decision.confidence,
        )
        await self.audit.record(
            "critical_parallel_timing",
            reading.correlation_id,
            {
                "decide_ms": round(decide_ms, 2),
                "verify_ms": round(verify_ms, 2),
                "parallel_ms": round(parallel_ms, 2),
                "saved_ms": round(saved_ms, 2),
                "chosen_by": decision.chosen_by,
                "confidence": decision.confidence,
            },
        )

        await self.audit.record(
            "guardian_verdict",
            reading.correlation_id,
            {
                "verdict": verdict.verdict.value if verdict else "unreachable",
                "trust_score": verdict.trust_score if verdict else None,
            },
        )

        sm.transition(IncidentState.DECIDED_HELD)

        # Gate
        gate_result = self.gate.check(verdict)
        if not gate_result.allowed:
            sm.transition(IncidentState.DISCARDED)
            METRICS.record_discard(gate_result.reason)
            METRICS.record_critical_near_miss()
            log.warning(
                "critical near-miss corr=%s reason=%s (decision computed but never sent)",
                reading.correlation_id, gate_result.reason,
            )
            await self.audit.record(
                "critical_near_miss",
                reading.correlation_id,
                {"reason": gate_result.reason,
                 "commands_held": len(decision.commands)},
            )
            log_live_event("Incident Discarded", f"Reason: {gate_result.reason}", "discard")
            return PipelineResult(
                reading.correlation_id, sm.state, path="critical",
                incident_kind=incident.kind.value, verdict=verdict,
                discarded_reason=gate_result.reason, saved_ms=saved_ms,
            )

        # Dispatch
        assert verdict is not None
        acks = await self._dispatch(decision, verdict)
        sm.transition(IncidentState.DISPATCHED)
        return PipelineResult(
            reading.correlation_id, sm.state, path="critical",
            incident_kind=incident.kind.value, verdict=verdict,
            dispatched=acks, saved_ms=saved_ms,
        )

    # ------------------------------------------------------------------
    # §3.2 — Normal path (sequential)
    # ------------------------------------------------------------------
    async def _process_normal(
        self, reading: SensorReading, incident: DetectedIncident
    ) -> PipelineResult:
        sm = IncidentStateMachine(reading.correlation_id)
        log.info("normal path corr=%s", reading.correlation_id)
        METRICS.record_normal_path()

        # 1. Verify FIRST
        verdict = await self.guardian.verify(reading)
        
        log_live_event(
            "Guardian Verdict", 
            f"Verdict: {verdict.verdict.value if verdict else 'unreachable'} | Score: {verdict.trust_score if verdict else 0.0}",
            "verdict"
        )

        await self.audit.record(
            "guardian_verdict",
            reading.correlation_id,
            {"verdict": verdict.verdict.value if verdict else "unreachable",
             "trust_score": verdict.trust_score if verdict else None},
        )

        gate_result = self.gate.check(verdict)
        if not gate_result.allowed:
            sm.transition(IncidentState.DISCARDED)
            METRICS.record_discard(gate_result.reason)
            log.warning(
                "discarded corr=%s reason=%s", reading.correlation_id, gate_result.reason
            )
            await self.audit.record(
                "discarded", reading.correlation_id, {"reason": gate_result.reason}
            )
            log_live_event("Incident Discarded", f"Reason: {gate_result.reason}", "discard")
            return PipelineResult(
                reading.correlation_id, sm.state, path="normal",
                incident_kind=incident.kind.value, verdict=verdict,
                discarded_reason=gate_result.reason,
            )

        sm.transition(IncidentState.VERIFIED)

        # 2. Only now decide
        decision = decide(incident, trust_score=0.0, reading=reading)
        log.info(
            "decision chosen_by=%s confidence=%.3f reason=%s",
            decision.chosen_by, decision.confidence, decision.reason,
        )
        sm.transition(IncidentState.DECIDED)

        # 3. Dispatch
        assert verdict is not None
        acks = await self._dispatch(decision, verdict)
        sm.transition(IncidentState.DISPATCHED)
        return PipelineResult(
            reading.correlation_id, sm.state, path="normal",
            incident_kind=incident.kind.value, verdict=verdict,
            dispatched=acks,
        )

    # ------------------------------------------------------------------
    # Shared dispatch helper
    # ------------------------------------------------------------------
    async def _dispatch(
        self, decision: Decision, verdict: GuardianVerdict
    ) -> list[ActionAck]:
        for cmd in decision.commands:
            cmd.trust_score = verdict.trust_score

        acks: list[ActionAck] = []
        for cmd in decision.commands:
            ack = await self.twin.dispatch(cmd)
            if ack is not None:
                acks.append(ack)
                METRICS.record_dispatch()
            await self.audit.record(
                "dispatched",
                decision.correlation_id,
                {"actuator_id": cmd.actuator_id, "action": cmd.action,
                 "ack": ack.status if ack else "no_ack"},
            )
            log_live_event(
                "Action Dispatched", 
                f"Actuator: {cmd.actuator_id} | Action: {cmd.action} | Corr: {decision.correlation_id}", 
                "dispatch"
            )
        return acks