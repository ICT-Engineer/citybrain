r"""City Brain pipeline for Twin-layer readings (merge-ready path).

    Twin reading --> replay/run checks --> usable? --(no)--> use backup sensor (SCN-03)
                         |
                         v
                  catalog detection --(no incident)--> nothing to do
                         |
          critical? -- yes --> decide || Guardian verdict (parallel, §3.1)
                         \-- no ---> Guardian verdict, then decide (§3.2)
                         |
          verdict TRUSTED and score >= threshold?  --(no)--> discard, record reasons (T5)
                         |                          \-- DEVICE_QUARANTINED / SENSOR_FAILED --> backup sensor
                         v
          for each command: Guardian says actuator quarantined? --> backup actuator (SCN-04)
                            send to Twin with idempotent request_id + verdict context
                            GUARDIAN_RESTRICTED --> backup actuator; TWIN_UNREACHABLE --> hold, never queue (SCN-06)

Invariant kept from the original Brain: NOTHING is dispatched without a TRUSTED
Guardian verdict attached; no answer from Guardian counts as UNTRUSTED.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from city_brain import catalog
from city_brain.adapters.twin_reading import from_twin, validate_twin_row
from city_brain.audit.log import AuditLogger
from city_brain.clients.guardian_contract import GuardianContractClient
from city_brain.clients.twin_contract import TwinContractClient, request_id_for
from city_brain.decision.twin_actions import PlannedCommand, ResponderBook, TwinPlan, plan_for, recovery_plan
from city_brain.detection.rules import DetectedIncident, IncidentKind
from city_brain.detection.twin_rules import TwinDetector
from city_brain.dispatch.trust_gate import TrustGate
from city_brain.observability.live_events import log_live_event
from city_brain.observability.metrics import METRICS
from city_brain.observability.timeline import TIMELINE
from city_brain.schemas.guardian import GuardianVerdict
from city_brain.schemas.twin import SensorReading, SeverityClass

log = logging.getLogger(__name__)

BACKUP_REASONS = {"DEVICE_QUARANTINED", "SENSOR_FAILED"}
MESSAGE_REASONS = {"REPLAY_OR_OUT_OF_ORDER", "MALFORMED_PAYLOAD", "MESSAGE_FLOOD", "FUTURE_TIMESTAMP"}


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass
class Decision:
    """One Brain decision, as shown on the dashboard and written to the audit log."""
    ts: str
    correlation_id: str
    sensor_id: str
    zone: str
    incident: str
    path: str
    outcome: str                        # DISPATCHED / PARTIAL / DISCARDED / HELD / ADVISORY / RECOVERED
    verdict: str | None = None
    trust_score: float | None = None
    reason_codes: list[str] = field(default_factory=list)
    chosen_by: str = "rules"
    confidence: float = 1.0
    commands: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    evidence_sensor: str | None = None


class BrainState:
    """In-memory view used by the dashboard and the API."""

    def __init__(self) -> None:
        self.run_id: str | None = None
        self.readings: dict[str, dict] = {}         # latest raw Twin reading per sensor
        self.trust: dict[str, dict] = {}            # latest Guardian device trust per device
        self.verdicts: dict[str, dict] = {}         # last verdict the Brain used per sensor
        self.decisions: deque[dict] = deque(maxlen=200)
        self.commands: deque[dict] = deque(maxlen=200)
        self.blocked_roads: set[str] = set()
        self.alerts: list[str] = []
        self.twin_status: dict = {"connected": False}
        self.guardian_status: dict = {"connected": False}
        self.actuator_state: dict[str, dict] = {}   # last command the Brain got applied per device
        self.restricted: set[str] = set()           # actuators the Twin refused with GUARDIAN_RESTRICTED
        self.input_rejections: dict[str, int] = {}  # partner input refused by validation, per reason

    def reset_run(self, run_id: str | None) -> None:
        self.run_id = run_id
        self.readings.clear()
        self.verdicts.clear()
        self.decisions.clear()                      # a new Twin run is a new simulation: start a clean log
        self.commands.clear()
        self.actuator_state.clear()
        self.restricted.clear()


class TwinPipeline:
    def __init__(self, twin: TwinContractClient | None = None, guardian: GuardianContractClient | None = None,
                 gate: TrustGate | None = None, audit: AuditLogger | None = None) -> None:
        self.twin = twin or TwinContractClient()
        self.guardian = guardian or GuardianContractClient()
        self.gate = gate or TrustGate()
        self.audit = audit or AuditLogger()
        self.detector = TwinDetector()
        self.book = ResponderBook()
        self.state = BrainState()
        import os
        from pathlib import Path

        from city_brain.offline import OfflineVault
        from city_brain.security.command_shield import get_shield
        self.vault = OfflineVault(Path(os.environ.get("BRAIN_DATA_DIR", "data")), get_shield().key)
        self._cid = 0
        self._last_seq: dict[str, int] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._backup_noted: dict[str, tuple] = {}

    # ------------------------------------------------------------------ run handling
    def on_run(self, run_id: str | None) -> bool:
        """Twin restarted its simulation (new run_id): numbering starts over."""
        if run_id == self.state.run_id:
            return False
        if self.state.run_id is not None:
            log_live_event("Twin Run Reset", f"run {self.state.run_id} -> {run_id}: replay counters and incidents reset", "reading")
        self.state.reset_run(run_id)
        self._last_seq.clear()
        self._backup_noted.clear()
        self.detector.reset()
        self.book.reset()
        return True

    def _fresh(self, reading: SensorReading) -> bool:
        prev = self._last_seq.get(reading.sensor_id)
        if prev is not None and reading.seq <= prev:
            return False
        self._last_seq[reading.sensor_id] = reading.seq
        return True

    # ------------------------------------------------------------------ main entry
    async def process_twin_row(self, row: dict, run_id: str | None = None) -> Decision | None:
        bad = validate_twin_row(row)
        if bad is not None:
            self.state.input_rejections[bad.split(":")[0]] = self.state.input_rejections.get(bad.split(":")[0], 0) + 1
            sid = row.get("sensor_id") if isinstance(row, dict) else None
            TIMELINE.add("input_rejected", "twin", None, f"reading from {sid!r} refused: {bad}", reason=bad)
            log.warning("twin reading refused (%s): %.200r", bad, row)
            return None
        reading = from_twin(row, run_id)
        if reading is None:
            return None
        sid = reading.sensor_id
        self.state.readings[sid] = row
        if not self._fresh(reading):
            return None                                       # same sample as the last poll (or older)
        METRICS.record_ingest()
        lock = self._locks.setdefault(sid, asyncio.Lock())
        async with lock:
            return await self._process(reading)

    async def _process(self, reading: SensorReading) -> Decision | None:
        sid = reading.sensor_id
        unusable = (reading.available is False or (reading.status or "").upper() in ("OFFLINE", "FAILED", "DEAD")
                    or reading.value is None)
        if unusable:
            return await self._use_backup(reading, why=f"{sid} unusable (status={reading.status}, available={reading.available})")
        self._backup_noted.pop(sid, None)                     # stream is healthy again

        det = self.detector.observe(reading)
        if det.cleared is not None:
            if det.cleared.evidence.get("corroborates"):
                return None                                   # the primary sensor's incident owns the recovery
            return await self._recover(reading, det.cleared)
        if det.incident is None:
            return None
        # Redundant pair (PRT-WLV-01/02, IND-TMP-01/02): if the backup already holds the same
        # incident, this reading corroborates it instead of dispatching a second response.
        active = self.detector.active()
        for b in reading.backup_ids:
            other = active.get(b)
            if other is not None and other.kind == det.incident.kind:
                det.incident.evidence["corroborates"] = b
                self.detector.mark_active(sid, det.incident)
                log_live_event("Incident Corroborated", f"{sid} confirms the {other.kind.value} already handled via {b}", "reading")
                return None
        self.detector.mark_active(sid, det.incident)          # claim now (before any await) so its pair corroborates
        if det.incident.kind == IncidentKind.AIR_POLLUTION:
            m = self.detector.mem.get(sid)
            last_f_ind = self.state.actuator_state.get("F-IND", {}).get("command")
            det.incident.evidence.update(history=(m.values[:-1] if m else []), plant_active=last_f_ind != "open",
                                         hour=reading.ts.hour + reading.ts.minute / 60)
        METRICS.record_incident()
        TIMELINE.add("reading", "twin", det.incident.correlation_id,
                     f"{sid} = {reading.value} {reading.unit} (normal {det.incident.evidence['normal_limits']})",
                     sensor_id=sid, observation_id=reading.observation_id, value=reading.value, incident=det.incident.kind.value)
        log_live_event("Incident Detected",
                       f"{det.incident.kind.value} on {sid} = {reading.value} {reading.unit} (normal {det.incident.evidence['normal_limits']})",
                       "reading")
        return await self._handle(reading, det.incident)

    # ------------------------------------------------------------------ paths
    async def _handle(self, reading: SensorReading, incident: DetectedIncident, evidence_sensor: str | None = None) -> Decision:
        critical = reading.severity_class == SeverityClass.CRITICAL
        path = "critical" if critical else "normal"
        trend = self.detector.trend(reading.sensor_id)
        if critical:

            async def _decide():
                s = time.perf_counter()
                p = plan_for(incident, reading, self.book, self.state.blocked_roads, trend)
                return p, (time.perf_counter() - s) * 1000

            async def _verify():
                s = time.perf_counter()
                v = await self.guardian.verify(reading)
                return v, (time.perf_counter() - s) * 1000

            (plan, d_ms), (verdict, v_ms) = await asyncio.gather(_decide(), _verify())
            METRICS.record_critical_path(max(0.0, min(d_ms, v_ms)))
        else:
            METRICS.record_normal_path()
            verdict = await self.guardian.verify(reading)
            plan = None
        self._remember_verdict(reading, verdict)
        TIMELINE.add("verdict", "guardian", incident.correlation_id,
                     (f"{verdict.verdict.value.upper()} score {verdict.trust_score:.2f}" + (f" {','.join(verdict.reason_codes)}" if verdict.reason_codes else ""))
                     if verdict else "no verdict (treated as UNTRUSTED)",
                     sensor_id=reading.sensor_id, observation_id=reading.observation_id,
                     verdict=verdict.verdict.value.upper() if verdict else None)
        log_live_event("Guardian Verdict",
                       f"{reading.sensor_id} obs={reading.observation_id}: "
                       f"{verdict.verdict.value.upper() if verdict else 'NO ANSWER (treated as UNTRUSTED)'}"
                       f"{' | score ' + format(verdict.trust_score, '.2f') if verdict else ''}"
                       f"{' | ' + ','.join(verdict.reason_codes) if verdict and verdict.reason_codes else ''}",
                       "verdict")

        gate = self.gate.check(verdict)
        if not gate.allowed:
            self._release(incident.sensor_id)                 # not handled: re-evaluate on the next sample
            codes = set(verdict.reason_codes) if verdict else set()
            if verdict and codes & BACKUP_REASONS and evidence_sensor is None:
                return await self._use_backup(reading, why=f"Guardian: {','.join(sorted(codes & BACKUP_REASONS))}")
            if critical:
                METRICS.record_critical_near_miss()
            METRICS.record_discard(gate.reason)
            d = Decision(ts=_now(), correlation_id=reading.correlation_id, sensor_id=incident.sensor_id, zone=incident.zone,
                         incident=incident.kind.value, path=path,
                         outcome="HELD" if verdict is None else "DISCARDED",
                         verdict=verdict.verdict.value.upper() if verdict else None,
                         trust_score=verdict.trust_score if verdict else None,
                         reason_codes=sorted(codes) or ([("no_verdict_for_observation"
                                                           if "404" in (self.guardian.last_error or "") else gate.reason)]),
                         notes=[(("Guardian has no verdict for this sample yet" if "404" in (self.guardian.last_error or "")
                                  else "Guardian unreachable") + ": no decision fabricated, nothing queued" if verdict is None else
                                 f"TRUSTED but trust score {verdict.trust_score:.2f} < threshold {self.gate.threshold}: "
                                 "no actuator command sent" if gate.reason == "below_threshold" else
                                 "UNTRUSTED evidence: response refused, no actuator command sent")
                                + (" (message problem, device trust untouched)" if codes & MESSAGE_REASONS else "")],
                         evidence_sensor=evidence_sensor)
            log_live_event("Incident Discarded" if verdict else "Decision Held",
                           f"{incident.kind.value} on {incident.sensor_id}: {d.notes[0]}", "discard")
            return await self._finish(d)

        if plan is None:
            plan = plan_for(incident, reading, self.book, self.state.blocked_roads, trend)
        return await self._execute(plan, reading, incident, verdict, path, evidence_sensor)

    async def _execute(self, plan: TwinPlan, reading: SensorReading, incident: DetectedIncident,
                       verdict: GuardianVerdict, path: str, evidence_sensor: str | None) -> Decision:
        TIMELINE.add("decision", "brain", plan.correlation_id,
                     f"{'recover' if path == 'recovery' else 'act on'} {incident.kind.value} at {incident.sensor_id}: "
                     + (", ".join(f"{c.device_id} {c.command}" for c in plan.commands) or "advisory, no actuator")
                     + (f" ({plan.chosen_by} {plan.confidence:.2f})" if plan.chosen_by == "model" else " (rules)"),
                     sensor_id=incident.sensor_id, incident=incident.kind.value, chosen_by=plan.chosen_by,
                     planned=[f"{c.device_id} {c.command}" for c in plan.commands])
        results = []
        for cmd in plan.commands:
            results.append(await self._send(cmd, plan.correlation_id, reading, verdict))
        applied = [r for r in results if r["outcome"] == "APPLIED"]
        unsent = [r for r in results if r.get("error") == "TWIN_UNREACHABLE"]
        if unsent:
            # The decision is taken but the link is down: never lose it. Seal it in the offline vault;
            # it is re-sent with the same request_id when the link returns (or expires, see offline/).
            saved = self.vault.save({"correlation_id": plan.correlation_id, "incident": incident.kind.value},
                                    [{k: r[k] for k in ("device_id", "command", "params", "request_id")} |
                                     {"context": {"correlation_id": plan.correlation_id, "requested_by": "city-brain",
                                                  "observation_id": reading.observation_id,
                                                  "guardian_verdict_id": verdict.verdict_id, "trust_score": verdict.trust_score},
                                      "source_sensor": incident.sensor_id} for r in unsent], incident.sensor_id)
            plan.notes.append(f"link to the Twin is down: decision sealed offline as {saved['vault_id']} "
                              f"({self.vault.pending_log.path.as_posix()}), will be re-sent automatically")
            TIMELINE.add("vault", "brain", plan.correlation_id, f"{len(unsent)} command(s) sealed offline as {saved['vault_id']}",
                         vault_id=saved["vault_id"])
            log_live_event("Saved Offline", f"{incident.kind.value} on {incident.sensor_id}: {len(unsent)} command(s) -> {saved['vault_id']}", "held")
        if not plan.commands:
            outcome = "ADVISORY"
        elif len(applied) == len(plan.commands):
            outcome = "DISPATCHED"
        elif applied:
            outcome = "PARTIAL"
        else:
            outcome = "FAILED"
        if path == "recovery":
            if outcome in ("DISPATCHED", "ADVISORY"):
                outcome = "RECOVERED"                     # detector already cleared the incident
            else:
                self.detector.mark_active(incident.sensor_id, incident)   # recovery not applied: try again next sample
        elif unsent:
            outcome = "SAVED_OFFLINE" if not applied else "PARTIAL_OFFLINE"
            self.detector.mark_active(incident.sensor_id, incident)   # decided: wait for delivery, do not re-decide
        elif outcome in ("DISPATCHED", "PARTIAL", "ADVISORY"):
            self.detector.mark_active(incident.sensor_id, incident)   # do not re-fire until it clears
        else:
            self._release(incident.sensor_id)                          # nothing applied: retry on a fresh sample
        d = Decision(ts=_now(), correlation_id=plan.correlation_id, sensor_id=incident.sensor_id, zone=incident.zone,
                     incident=incident.kind.value, path=path, outcome=outcome,
                     verdict=verdict.verdict.value.upper(), trust_score=verdict.trust_score,
                     reason_codes=verdict.reason_codes, chosen_by=plan.chosen_by, confidence=round(plan.confidence, 3),
                     commands=results, notes=[plan.reason, *plan.notes], evidence_sensor=evidence_sensor)
        return await self._finish(d, timeline_done=True)

    async def _send(self, cmd: PlannedCommand, correlation_id: str, reading: SensorReading,
                    verdict: GuardianVerdict | None) -> dict:
        tried: list[dict] = []
        candidates = [cmd.device_id, *cmd.alternatives]
        for device in candidates:
            this = cmd if device == cmd.device_id else self._retarget(cmd, device)
            if this is None:
                continue
            if device in self.state.restricted:
                tried.append({"device_id": device, "outcome": "SKIPPED", "error": "TWIN_RESTRICTED_EARLIER"})
                continue
            if await self.guardian.is_quarantined(device):
                tried.append({"device_id": device, "outcome": "SKIPPED", "error": "GUARDIAN_QUARANTINED"})
                log_live_event("Actuator Restricted", f"{device} is quarantined by Guardian: trying backup", "discard")
                continue
            rid = request_id_for(correlation_id, device, this.command, this.params)
            context = {"correlation_id": correlation_id, "requested_by": "city-brain",
                       "observation_id": reading.observation_id,
                       "guardian_verdict_id": verdict.verdict_id if verdict else None,
                       "trust_score": verdict.trust_score if verdict else None}
            env = await self.twin.send_actuator_command(device, this.command, this.params, rid,
                                                        {k: v for k, v in context.items() if v is not None})
            err = (env.get("error") or {}).get("code") if not env.get("ok") else None
            rec = {"device_id": device, "command": this.command, "params": this.params, "request_id": rid,
                   "outcome": "APPLIED" if env.get("ok") else "REJECTED", "error": err,
                   "twin_status": (env.get("data") or {}).get("status") if env.get("ok") else None,
                   "why": this.why}
            tried.append(rec)
            self._cid += 1
            self.state.commands.appendleft({"cid": self._cid, "ts": _now(), "correlation_id": correlation_id,
                                            "source_sensor": reading.sensor_id, **rec})
            if env.get("ok"):
                METRICS.record_dispatch()
                self.book.confirm(this)
                self.state.actuator_state[device] = {"command": this.command, "params": this.params, "ts": _now()}
                TIMELINE.add("action", "twin", correlation_id, f"{device} {this.command} {this.params or ''} APPLIED",
                             device_id=device, request_id=rid, outcome="APPLIED")
                log_live_event("Action Dispatched", f"{device} {this.command} {this.params or ''} -> APPLIED", "dispatch")
                return {**rec, "attempts": tried}
            TIMELINE.add("action", "twin", correlation_id, f"{device} {this.command} REJECTED {err}",
                         device_id=device, request_id=rid, outcome="REJECTED", error=err)
            log_live_event("Command Rejected", f"{device} {this.command}: {err}", "discard")
            if err == "GUARDIAN_RESTRICTED":
                self.state.restricted.add(device)
                continue                                         # SCN-04: try the backup actuator
            if err == "TWIN_UNREACHABLE":
                break                                            # SCN-06: hold, never queue
        last = tried[-1] if tried else {"device_id": cmd.device_id, "outcome": "SKIPPED", "error": "NO_CANDIDATE"}
        return {**last, "outcome": last.get("outcome", "REJECTED"), "attempts": tried}

    def _retarget(self, cmd: PlannedCommand, device: str) -> PlannedCommand | None:
        if cmd.command == "dispatch":                        # responders need their own route and mission
            zone = next((z for z, j in catalog.ZONE_TO_JUNCTION.items() if j == cmd.params["destination_node_id"]), "DTN")
            return self.book.plan_dispatch(device, zone, self.state.blocked_roads)
        return PlannedCommand(device, cmd.command, dict(cmd.params), why=cmd.why + f" (backup for {cmd.device_id})")

    async def _use_backup(self, reading: SensorReading, why: str) -> Decision | None:
        """SCN-03: ignore the bad stream and decide on a compatible backup sensor."""
        spec = catalog.sensors().get(reading.sensor_id)
        backups = list(reading.backup_ids or (spec.backup_ids if spec else ()))
        # The primary's open incident (if any) is handed over to the backup's own stream.
        self.detector.mem.pop(reading.sensor_id, None)
        chosen, note = None, ""
        for b in backups:
            row = self.state.readings.get(b)
            br = from_twin(row, self.state.run_id) if row else None
            if br is None or br.value is None or br.available is False or (br.status or "").upper() in ("FAILED", "OFFLINE"):
                continue
            chosen = b
            note = (f"{why}: stream ignored; decisions now rely on backup {b} "
                    f"(currently {br.value} {br.unit}), whose own readings are judged by Guardian")
            break
        if chosen is None:
            note = f"{why}: stream ignored; " + (f"no usable backup among {backups}" if backups else "no backup sensor in the catalog")
        key = (chosen, (reading.status or "").upper(), reading.available)
        if self._backup_noted.get(reading.sensor_id) == key:
            return None                                       # already reported for this outage
        self._backup_noted[reading.sensor_id] = key
        log_live_event("Backup Sensor" if chosen else "Stream Ignored", note, "reading")
        return await self._finish(Decision(ts=_now(), correlation_id=reading.correlation_id, sensor_id=reading.sensor_id,
                                           zone=reading.zone, incident="sensor_unusable", path="backup",
                                           outcome="BACKUP_USED" if chosen else "IGNORED",
                                           notes=[note], evidence_sensor=chosen))

    async def _recover(self, reading: SensorReading, incident: DetectedIncident) -> Decision:
        plan = recovery_plan(incident)
        if not plan.commands:
            return await self._finish(Decision(ts=_now(), correlation_id=plan.correlation_id, sensor_id=incident.sensor_id,
                                               zone=incident.zone, incident=incident.kind.value, path="recovery",
                                               outcome="RECOVERED", notes=[plan.reason]))
        verdict = await self.guardian.verify(reading)
        self._remember_verdict(reading, verdict)
        gate = self.gate.check(verdict)
        if not gate.allowed:
            self.detector.mark_active(incident.sensor_id, incident)   # stay active, re-check next sample
            return await self._finish(Decision(ts=_now(), correlation_id=plan.correlation_id, sensor_id=incident.sensor_id,
                                               zone=incident.zone, incident=incident.kind.value, path="recovery",
                                               outcome="HELD", notes=[plan.reason, f"recovery waits for a TRUSTED reading ({gate.reason})"]))
        return await self._execute(plan, reading, incident, verdict, "recovery", None)

    # ------------------------------------------------------------------ bookkeeping
    def _release(self, sensor_id: str) -> None:
        m = self.detector.mem.get(sensor_id)
        if m is not None:
            m.active = None
        for other in self.detector.mem.values():               # readings that only corroborated it re-open too
            if other.active is not None and other.active.evidence.get("corroborates") == sensor_id:
                other.active = None

    def _remember_verdict(self, reading: SensorReading, verdict: GuardianVerdict | None) -> None:
        self.state.verdicts[reading.sensor_id] = {
            "observation_id": reading.observation_id,
            "verdict": verdict.verdict.value.upper() if verdict else "NO_ANSWER",
            "trust_score": verdict.trust_score if verdict else None,
            "reason_codes": verdict.reason_codes if verdict else [],
            "ts": _now(),
        }

    async def _finish(self, d: Decision, quiet: bool = False, timeline_done: bool = False) -> Decision:
        rec = asdict(d)
        if not timeline_done:
            TIMELINE.add("decision", "brain", d.correlation_id,
                         f"{d.outcome} {d.incident} on {d.sensor_id}: " + (d.notes[0] if d.notes else ""),
                         sensor_id=d.sensor_id, outcome=d.outcome, incident=d.incident,
                         planned=[f"{c['device_id']} {c.get('command')}" for c in d.commands])
        self.state.decisions.appendleft(rec)
        try:
            self.vault.journal.append(rec)                    # every decision is kept on disk
        except OSError as e:
            log.warning("decision journal write failed: %s", e)
        await self.audit.record("brain_decision", d.correlation_id, rec)
        if not quiet:
            await self.guardian.post_timeline(
                "brain_decision", observation_id=d.correlation_id, device_id=d.sensor_id, incident=d.incident, outcome=d.outcome, verdict=d.verdict,
                actions=[f"{c['device_id']}:{c.get('command')}:{c['outcome']}" for c in d.commands])
        return d

    # ------------------------------------------------------------------ emergency calls
    async def emergency_call(self, kind: str, zone: str) -> Decision:
        """Emergency calls are reported events (B/C own them in the Twin). The Brain still
        needs a Guardian verdict; in lookup mode the call must be known to Guardian."""
        kinds = {"accident": IncidentKind.ACCIDENT, "fire": IncidentKind.FIRE, "medical": IncidentKind.MEDICAL}
        k = kinds.get(kind, IncidentKind.ACCIDENT)
        corr = f"{self.state.run_id or 'run'}:CALL-{zone}:{int(time.time() * 1000)}"
        inc = DetectedIncident(kind=k, correlation_id=corr, sensor_id=f"CALL-{zone}", zone=zone,
                               severity_class="critical", evidence={"incident_type": kind})
        plan = plan_for(inc, None, self.book, self.state.blocked_roads)
        reading = SensorReading(sensor_id=f"CALL-{zone}", type="emergency_call", value=None, ts=datetime.now(UTC),
                                seq=0, sig=corr, severity_class="critical", correlation_id=corr, zone=zone,
                                observation_id=corr, emergency={"incident_type": kind})
        verdict = GuardianVerdict(correlation_id=corr, sensor_id=reading.sensor_id, ts=datetime.now(UTC), seq=0,
                                  trust_score=1.0, verdict="trusted", sig="operator-confirmed-call",
                                  reason_codes=["OPERATOR_CONFIRMED_CALL"])
        TIMELINE.add("reading", "operator", corr, f"emergency call: {kind} in {catalog.ZONE_NAMES.get(zone, zone)}", sensor_id=f"CALL-{zone}")
        TIMELINE.add("verdict", "operator", corr, "OPERATOR_CONFIRMED_CALL (calls are not Twin sensor data)", verdict="TRUSTED")
        log_live_event("Emergency Call", f"{kind} reported in {catalog.ZONE_NAMES.get(zone, zone)} (operator-confirmed)", "reading")
        return await self._execute(plan, reading, inc, verdict, "critical", None)

    async def retry_offline(self) -> list[dict]:
        """Re-send sealed decisions once the link is back (same request_id = applied at most once)."""
        from city_brain.offline import PENDING_TTL_S
        out = []
        for item in self.vault.pending():
            if time.time() - item.get("last_try", 0) < 3.0:
                continue                                       # back-off: one attempt every 3 s per decision
            item["last_try"] = time.time()
            age = time.time() - item["saved_at"]
            is_recovery = str(item["correlation_id"]).endswith(":recovery")
            active = self.detector.active().get(item["sensor_id"])
            stale = not is_recovery and (active is None or active.correlation_id != item["correlation_id"])
            if age > PENDING_TTL_S or stale:
                # Never act on an old picture of the city: a decision older than the TTL, or whose
                # incident has cleared meanwhile, is kept for the audit but not executed.
                self.vault.update(item["vault_id"], state="EXPIRED",
                                  note="older than the TTL" if age > PENDING_TTL_S else "incident no longer active")
                if not is_recovery:
                    self._release(item["sensor_id"])
                out.append({"vault_id": item["vault_id"], "state": "EXPIRED"})
                continue
            results, left = [], []
            for c in item["commands"]:
                if c.get("delivered"):
                    continue
                env = await self.twin.send_actuator_command(c["device_id"], c["command"], c["params"], c["request_id"], c["context"])
                err = (env.get("error") or {}).get("code") if not env.get("ok") else None
                self._cid += 1
                self.state.commands.appendleft({"cid": self._cid, "ts": _now(), "correlation_id": item["correlation_id"],
                                                "source_sensor": c.get("source_sensor"), "device_id": c["device_id"],
                                                "command": c["command"], "params": c["params"], "request_id": c["request_id"],
                                                "outcome": "APPLIED" if env.get("ok") else "REJECTED", "error": err,
                                                "why": f"delivered from offline vault {item['vault_id']}"})
                if env.get("ok"):
                    c["delivered"] = True
                    METRICS.record_dispatch()
                    self.state.actuator_state[c["device_id"]] = {"command": c["command"], "params": c["params"], "ts": _now()}
                    TIMELINE.add("action", "twin", item["correlation_id"], f"{c['device_id']} {c['command']} APPLIED (from vault {item['vault_id']})",
                                 device_id=c["device_id"], request_id=c["request_id"], outcome="APPLIED", vault_id=item["vault_id"])
                    log_live_event("Delivered From Vault", f"{c['device_id']} {c['command']} ({item['vault_id']})", "dispatch")
                elif err == "TWIN_UNREACHABLE":
                    left.append(c)
                results.append(err or "APPLIED")
            if any(r == "TWIN_UNREACHABLE" for r in results):
                self.vault.update(item["vault_id"], attempts=item.get("attempts", 0) + 1, commands=item["commands"])
            elif all(r == "APPLIED" for r in results):
                self.vault.update(item["vault_id"], state="SENT", attempts=item.get("attempts", 0) + 1,
                                  commands=item["commands"], sent_at=time.time())
                out.append({"vault_id": item["vault_id"], "state": "SENT"})
            else:
                self.vault.update(item["vault_id"], state="FAILED", errors=results)
                self._release(item["sensor_id"])
                out.append({"vault_id": item["vault_id"], "state": "FAILED"})
        return out

    async def release_all(self) -> list[dict]:
        out = []
        for unit, mission in list(self.book.missions.items()):
            cmd = PlannedCommand(unit, "release", {"mission_ref": mission}, why="mission complete")
            rid = request_id_for(mission, unit, "release", cmd.params)
            env = await self.twin.send_actuator_command(unit, "release", cmd.params, rid, {"requested_by": "city-brain"})
            ok = bool(env.get("ok"))
            if ok:
                self.book.confirm(cmd)
            out.append({"device_id": unit, "mission_ref": mission, "outcome": "APPLIED" if ok else "REJECTED",
                        "error": (env.get("error") or {}).get("code")})
        return out
