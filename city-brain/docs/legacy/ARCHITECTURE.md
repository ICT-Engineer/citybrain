# City Brain Layer — Architecture

**Challenge:** TSYP14 — City Brain Convergence
**Layer owned by this team:** City Brain (operational decision-maker)
**Status:** Implemented (Phase 1). See "Implementation status" below for the checklist.

---

## 1. Role in the system

City Brain sits between the Twin (which simulates the city's sensors and actuators) and Guardian (which verifies whether a sensor reading can be trusted). Its one job: **consume real-time sensor data, decide the correct operational response to an incident, and dispatch that response through the Twin's actuator tools — but only once Guardian has confirmed the triggering reading is trustworthy.**

City Brain does not try to detect fraud or anomalies itself. That is Guardian's job, and duplicating it risks the two layers disagreeing. City Brain's intelligence is operational correctness: given a clean, trusted reading, pick the right response and track it to completion.

---

## 2. System architecture
                         ┌────────────────────┐
                         │   Twin (sensors)    │
                         │ Emits live readings │
                         └─────────┬───────────┘
                     readings      │      readings
                 ┌──────────────────┴──────────────────┐
                 ▼                                      ▼
      ┌────────────────────┐    verdict      ┌────────────────────┐
      │     City Brain      │◀───────────────│      Guardian       │
      │ Decides, dispatches │                 │  Verifies trust     │
      └─────────┬───────────┘                 └─────────┬───────────┘
          dispatch, if trusted             contain, if rejected
                 │                                      │
                 └──────────────────┬───────────────────┘
                                     ▼
                         ┌────────────────────┐
                         │  Twin (actuators)   │
                         │  Executes actions   │
                         └────────────────────┘

- Twin broadcasts every sensor reading to **both** City Brain and Guardian at the same time — neither layer waits on the other to start receiving data.
- Guardian independently judges each reading and returns a trust verdict to City Brain.
- City Brain never calls an actuator without a trusted verdict attached.
- Guardian can also act directly on Twin's actuators for containment (isolate a sensor, quarantine a device, roll back a falsified reading) — this is the one path that bypasses City Brain, because it is a safety action, not an operational decision.

---

## 3. Two processing paths

Life-critical events cannot afford to wait for a full verify-then-decide sequence, so the system splits event handling into two paths based on a severity tag set by the Twin.

### 3.1 Critical path (parallel)

Used for events such as fire or a major accident, where seconds matter.

1. Twin tags the reading `severity_class = critical` and sends it to City Brain and Guardian **at the same time**.
2. City Brain starts computing the response immediately — it does not wait for Guardian.
3. City Brain **holds** the computed decision instead of sending it.
4. Guardian verifies the reading in parallel and returns a verdict.
5. If the verdict is `trusted`, City Brain sends the held decision to the Twin. If `suspicious`/`rejected`, City Brain **discards** the decision — nothing was ever sent, so there is nothing to roll back — and logs it as a near-miss.



### 3.2 Normal path (sequential)

Used for routine events such as ordinary traffic readings.

1. Twin tags the reading `severity_class = normal` and sends it to Guardian first.
2. Guardian verifies the reading.
3. Only once Guardian clears it does the reading reach City Brain.
4. City Brain decides and dispatches.




### 3.3 The rule that holds on both paths

**Nothing is ever sent to the Twin's actuator tools without a trusted Guardian verdict attached.** The only difference between the two paths is *when* City Brain starts computing — immediately on the critical path, only after clearance on the normal path. This is the property an evaluator testing adversarial scenarios will check first.

### 3.4 Who sets severity_class

The Twin sets `severity_class` at the moment a reading is emitted, since it is the single source of truth both other layers key off. If City Brain and Guardian inferred severity independently, they could end up routing the same event down two different paths and desynchronizing. This is an open item to confirm with the Twin and Guardian teams at the 31 October matching.

---

## 4. Data contract

### 4.1 Fields received from the Twin layer

| Field | Type | Required | Why City Brain needs it |
|---|---|---|---|
| `sensor_id` | string | Yes | Identifies the source; keys the whole pipeline (routing, dedup, audit log) |
| `type` | enum | Yes | `traffic` / `water` / `power` / `air` / `emergency_call` — selects which incident-detection rule applies |
| `value`, `unit` | number, string | Yes | The actual reading the decision is based on |
| `ts` | ISO-8601 | Yes | Ordering and staleness checks |
| `seq` | integer | Yes | Monotonic per sensor; detects replayed/out-of-order readings |
| `sig` | string | Yes | Twin's signature on the reading; verified independently rather than only trusting Guardian |
| `severity_class` | enum: `critical` \| `normal` | Yes (new field) | Decides which of the two paths this event takes; must be set consistently by the Twin |
| `correlation_id` | string (uuid) | Yes | Shared id so City Brain's decision and Guardian's independent verdict on the same event can be matched back together, especially on the critical path |
| zone / location | string or lat-lon | Yes | Picks which actuator instance to act on (which intersection, valve, grid segment) |
| emergency-call payload | object | Only for `type=emergency_call` | Reported incident type, location, caller info — classifies fire/accident/flood directly |
| `action_ack` | object, sent after dispatch | Yes | Twin confirming the actuator call executed; closes the incident state machine |

### 4.2 Fields received from the Guardian layer

Same fields on both paths — only the timing differs.

| Field | Type | Required | Why City Brain needs it |
|---|---|---|---|
| `correlation_id` | string (uuid) | Yes | Matches the verdict to the decision in flight, critical on the parallel path |
| `sensor_id`, `ts`, `seq` | — | Yes | Ties the verdict back to the exact reading it judges |
| `trust_score` | float 0–1 | Yes | Gate threshold; nothing is dispatched below it |
| `verdict` | enum: `trusted` \| `suspicious` \| `rejected` | Yes | Drives the state-machine transition |
| `sig` | string | Yes | Guardian's signature on its own verdict, so a forged "trusted" result can't be injected |
| `reason_codes` | array of strings | Optional | Explains why a reading was flagged; useful for audit log and report, not required to function |

### 4.3 Fields sent back to the Twin (dispatch)

| Field | Required | Why |
|---|---|---|
| `actuator_id`, `action`, `params` | Yes | What we're asking the Twin to do, and on which device |
| `correlation_id` | Yes | Lets Twin (and Guardian) trace the action back to the originating reading |
| `trust_score` | Yes | Twin should also refuse to execute below threshold — defense in depth |
| `requested_by` | Yes | Identifies City Brain as the source, for Twin's own access-scope checks |

---

## 5. Incident detection and response, by type

The five pipeline fields above (`correlation_id`, `ts`, `seq`, `sig`, `severity_class`) are required on every reading regardless of incident type. What differs per type is which sensor triggers detection, what corroborating signal strengthens the decision, and whether a single reading or a rolling window is needed to confirm it.

### 5.1 Accidents
- **Trigger:** `emergency_call` feed, `incident_type=accident`
- **Corroboration:** traffic camera at the same zone (sudden vehicle-count drop/stop)
- **Needs:** `sensor_id`, zone/location, `incident_subtype` (if exposed), `severity_class` (usually `critical`)
- **Response:** dispatch ambulance + police to zone; optionally retime the nearest signal to clear a corridor

### 5.2 Fires
- **Trigger:** `emergency_call` feed, `incident_type=fire`
- **Corroboration:** air-quality monitor (PM2.5/smoke spike) in the same zone
- **Needs:** `sensor_id`, zone, air-quality value, `severity_class` (`critical`)
- **Response:** dispatch fire brigade + ambulance; consider power-grid isolation for that segment and traffic rerouting

### 5.3 Traffic jams
- **Trigger:** traffic camera count/density sustained above threshold for N consecutive ticks (a single spike is not enough)
- **Needs:** `sensor_id`, `value`, zone/intersection id, a rolling window of the last N readings, `severity_class` (usually `normal`, unless it blocks an emergency corridor)
- **Response:** retime the traffic signal (extend green phase, adjust cycle)

### 5.4 Flooding
- **Trigger:** water-level sensor exceeding threshold, combined with rate of rise (a slow seasonal rise should not trigger the same response as a rapid spike)
- **Needs:** `sensor_id`, `value`/`unit`, rate of change (derived from consecutive `seq`'d readings), zone (reservoir/river segment), `severity_class` (`critical` if rate of rise is high or a hard threshold is crossed, otherwise `normal`)
- **Response:** open/close the relevant valve; if severe, also flag for emergency dispatch and consider isolating any substation in the flood zone

---

## 6. Incident state machine

A per-incident state machine prevents the most common integration bug: the Twin resending a reading, or a partial/duplicate update, re-triggering the pipeline and causing a double dispatch (e.g. two ambulances for one accident).

---

## 7. Technology choices

| Concern | Choice | Why |
|---|---|---|
| Language / framework | Python 3.12 + FastAPI | Async I/O for a live stream plus concurrent tool calls; Pydantic gives free schema validation on every cross-team payload |
| Exposing / consuming tools | MCP-style tool registry | The brief requires MCP-style tools; schemas enforced in code on both caller and callee, not just documented |
| Consuming Twin's stream | Subscribe to a Redis Stream with a persisted consumer group | A restart resumes from where it left off instead of losing events |
| Storage | PostgreSQL | Durable audit trail: reading → verdict → decision → dispatch, needed for the evidence the scoring rubric asks for |
| Incident state machine | Small explicit state machine (enum + guard functions) | Cheap to build, directly demonstrable to evaluators |
| Testing | pytest + a custom `scenario_runner.py` | Scripts adversarial sequences (spoofed reading, frozen sensor, Guardian unreachable, duplicate events) against a self-written mock Twin/Guardian, reused later against the real triplet |
| CI | GitHub Actions: lint + pytest on push | Catches a broken integration before the demo, not during it |
| Containerization | Docker, one container per layer (api, consumer, postgres, redis) | Makes "Established Connection" trivial to set up with partner teams |

---

## 8. Security measures

- **Fail-safe default:** if Guardian is unreachable or does not respond, City Brain takes **no action** — never "assume trusted." This is the single most scored behavior under the adversarial-scenario criterion.
- **Independent signature check:** City Brain carries the Twin's `sig` on a reading end-to-end. Verification is Guardian's responsibility by design (§1) — City Brain does not duplicate it.
- **Replay protection:** the monotonic `seq` field lets City Brain (and Guardian) reject a reading that isn't strictly increasing, catching replayed-reading attacks.
- **Trust gate enforced twice:** City Brain checks the verdict before calling `twin.actuator.dispatch`, and the Twin itself should refuse the call server-side if `trust_score` is missing or below threshold — defense in depth in case City Brain's own check has a bug.
- **Audit log:** every verdict and every dispatch decision is logged with the evidence that justified it (`trust_score`, `seq`, which rule fired). This is the concrete proof shown to evaluators for the "Security & Trust Mechanisms" and "Response to Adversarial/Simulated Scenarios" scoring criteria.
- **Least-privilege scopes:** City Brain's access token only allows calling operational actuator tools, not Guardian's containment tools — keeping the separation of concerns the brief asks for.

---

## 9. Open questions for the triplet (Twin and Guardian teams)

- Who sets `severity_class`? Proposed: the Twin, at the point of emission.
- What is the exact `trust_score` threshold for "trusted"? Needs to be a shared number so Guardian's and Twin's enforcement agree.
- What format for `correlation_id`? Proposed: a uuid4 generated once by the Twin at emission, carried unchanged through the pipeline.
- Should the Twin also refuse actuator calls below the `trust_score` threshold on its own side? Recommended, as a second line of defense.

---

## 10. Implementation status

Mapping of every architectural commitment to its concrete implementation
and verifiable evidence. All references are to files in this repository.

### 10.1 Data contract and schemas (§4)

| Item | Where | Status |
|---|---|---|
| `SensorReading`, `DispatchCommand`, `ActionAck` | `src/city_brain/schemas/twin.py` | ✅ |
| `GuardianVerdict`, `Verdict` | `src/city_brain/schemas/guardian.py` | ✅ |
| Pydantic validation on every cross-team payload | enforced by FastAPI on ingest | ✅ |

### 10.2 Two processing paths (§3)

| Item | Where | Status |
|---|---|---|
| Critical path (parallel) | `Pipeline._process_critical` | ✅ |
| Normal path (sequential) | `Pipeline._process_normal` | ✅ |
| Invariant: no dispatch without trusted verdict | `dispatch/trust_gate.py`, `orchestrator.py` | ✅ |
| Latency measured and exposed | `saved_ms` field + `/metrics` | ✅ |

### 10.3 Detection (§5)

| Item | Where | Status |
|---|---|---|
| §5.1 Accidents | `detection/rules.py` | ✅ |
| §5.2 Fires | `detection/rules.py` | ✅ |
| §5.3 Traffic jams (streak ≥ 3) | `detection/rules.py` | ✅ |
| §5.4 Flooding (hard threshold) | `detection/rules.py` | ✅ |

### 10.4 Decision engine (ML model)

| Item | Where | Status |
|---|---|---|
| Synthetic dataset + training | `scripts/train_model.py` | ✅ |
| Trained RandomForest serialized | `models/city_brain_v1.pkl` | ✅ |
| Model inference wrapper | `decision/model.py` | ✅ |
| Rule-based fallback if confidence < threshold | `decision/engine.py` | ✅ |

### 10.5 State machine (§6)

| Item | Where | Status |
|---|---|---|
| Transitions for both paths | `decision/state_machine.py` | ✅ |
| Prevents double-dispatch | tested in `test_pipeline.py` | ✅ |

### 10.6 Technology (§7)

| Item | Where | Status |
|---|---|---|
| FastAPI + async I/O | `main.py`, `api/routes.py` | ✅ |
| MCP-style tool discovery | `mcp/registry.py`, `GET /mcp/tools` | ✅ |
| Redis Streams ingestion with consumer group | `ingestion/stream.py` | ✅ |
| Standalone consumer service | `scripts/run_consumer.py` | ✅ |
| PostgreSQL audit trail | `audit/db.py`, `audit/log.py` | ✅ |
| Incident state machine | `decision/state_machine.py` | ✅ |
| Test suite | `tests/` (28+ tests) | ✅ |
| Scenario runner | `scripts/scenario_runner.py` (5/5 pass) | ✅ |
| GitHub Actions CI | `.github/workflows/ci.yml` | ✅ |
| Docker Compose (api, consumer, postgres, redis) | `docker-compose.yml` | ✅ |

### 10.7 Security (§8)

| Item | Where | Status |
|---|---|---|
| Fail-safe default (no action if Guardian down) | `dispatch/trust_gate.py` | ✅ |
| Replay protection (monotonic `seq`) | `security/replay_guard.py` | ✅ |
| Trust gate enforced at dispatch | `orchestrator.py` before every `twin.dispatch()` | ✅ |
| Full audit log, queryable by `correlation_id` | `GET /audit/{correlation_id}` | ✅ |
| Independent signature field carried end-to-end | `sig` on every payload | ✅ (verification is Guardian's responsibility) |

### 10.8 Evidence

Concrete, reproducible proof of every claim above lives in `EVIDENCE.md`.

### 10.9 Known limitations

We document here the current boundaries of the Phase 1 delivery. Each
limitation is either a deliberate architectural choice or a known
dependency on partner layers, with a clear path forward for Phase 2.

**1. Replay guard state is per-process (in-memory).**
- *Reason:* For Phase 1, the system runs on a single API worker. 
- *Impact:* If deployed with multiple workers (e.g., Kubernetes pods), a replay attack could bypass the guard by routing to a different worker.
- *Phase 2 plan:* Migrate the `ReplayGuard` state to Redis (using a sorted set per `sensor_id`). This will make the guard stateless and horizontally scalable.

**2. Cryptographic signatures (`sig`) are delegated to Guardian.**
- *Reason:* By design (§1), City Brain is an operational layer, not a security layer. Duplicating signature verification could lead to "split-brain" scenarios where Guardian trusts a reading but City Brain rejects it (or vice versa).
- *Impact:* If Guardian is compromised and lies about a signature, City Brain will trust the verdict.
- *Defense:* We still carry the `sig` end-to-end in the audit log. If a breach is detected post-incident, we can trace exactly which reading was forged.
- *Phase 2 plan:* Implement a lightweight HMAC verification in City Brain as a second line of defense, sharing a symmetric key with the Twin.

**3. Decision model relies on synthetic data for rare events.**
- *Reason:* For Phase 1, we could not access the Twin team's real simulation data. We trained on 21,313 examples (20,000 synthetic + 1,313 real NYC incidents).
- *Impact:* The model performs exceptionally well on structured data (96.7% test accuracy) but may need fine-tuning for the specific zone names and sensor distributions used by the real Twin.
- *Phase 2 plan:* At the 31 October matching, we will collect real sensor distributions from the Twin team and retrain the model (the training script is already parameterized and reproducible).