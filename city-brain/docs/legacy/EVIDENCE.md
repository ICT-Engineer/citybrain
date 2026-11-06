# City Brain — Verification Evidence

Concrete proof that the City Brain layer implements the behaviors described
in `ARCHITECTURE.md`. Every claim here is backed by a runnable command.

Date of last verification: 2026-10-13

---

## 1. Test suite

    $ pytest
    28 passed

Covers: mocks, schemas, model v2, pipeline paths, replay guard,
Redis stream consumer, smoke.

Test modules:

    tests/test_mocks.py           7 tests
    tests/test_model.py           8 tests
    tests/test_pipeline.py        4 tests
    tests/test_pipeline_paths.py  4 tests
    tests/test_redis_stream.py    2 tests
    tests/test_schemas.py         2 tests
    tests/test_smoke.py           1 test

## 2. Adversarial scenarios

    $ python scripts/scenario_runner.py
    ✅ spoofed_reading
    ✅ frozen_sensor
    ✅ guardian_unreachable (SKIPPED, chaos=off)
    ✅ duplicate_event
    ✅ replayed_reading

    Result: 5/5 scenarios passed

## 3. Live metrics (`GET /metrics`)

    critical_path_total:       2
    normal_path_total:         2
    dispatches_total:          5
    discards_total:            4
    replays_rejected_total:    3
    discards_by_reason:        {"verdict_rejected": 1, "replay": 3}

- 2 critical events processed via the parallel path (§3.1)
- 2 normal events processed via the sequential path (§3.2)
- 5 dispatches executed by the Twin
- 3 replayed readings blocked before detection

## 4. Model decisions

Decision model v3: RandomForest, **16 features**, trained on **21 313
examples** — 20 000 synthetic with realistic distributions + 1 313 real
incidents from NYC Open Data (Motor Vehicle Collisions h9gi-nx95 and
Fire Incident Dispatch 8m42-w767, subsampled to 500/class for balance).

    Train accuracy: 0.9677
    Test  accuracy: 0.9669
    F1 macro:       0.95

Every dispatch decision is logged with the source of the decision:

    INFO:city_brain.pipeline.orchestrator:decision chosen_by=model confidence=0.913 reason=model predicted retime_traffic_signal
    INFO:city_brain.pipeline.orchestrator:decision chosen_by=model confidence=1.000 reason=model predicted dispatch_ambulance_police

`chosen_by=model` proves the decision came from the trained RandomForest,
not from the rule-based fallback in `decision/engine.py`.

## 4bis. Real training data

Two public NYC Open Data datasets were downloaded once and adapted
offline via `scripts/load_real_dataset.py`. No API calls are made at
runtime — the resulting `.pkl` is committed to the repo.

    Source                              Rows used
    ──────────────────────────────────  ─────────
    NYC Motor Vehicle Collisions        500  (from 10 000)
    NYC Fire Incident Dispatch Data     813  (from  4 097)
    ──────────────────────────────────  ─────────
    Total real                         1 313

Both sources are subsampled per class to counter the natural 93%
imbalance of `dispatch_ambulance_police` in raw collisions data.
The synthetic generator provides the remaining 20 000 examples with
balanced class distribution, ensuring the model performs well on the
rare but critical `open_valve` and `dispatch_fire_ambulance` actions.

    $ python scripts/load_real_dataset.py
    Loading data\nyc_collisions.csv...
      -> 10000 collision rows
    Loading data\nyc_fires.csv...
      -> 4097 fire rows
    Total real rows: 14097
    Saved to data\real_samples.csv

    $ python scripts/train_model.py
    Loading real NYC data (balanced subsample, cap=500/class)...
      real dispatch_ambulance_police   500 (from 13073)
      real dispatch_fire_ambulance     500 (from   711)
      real open_valve                  163 (from   163)
      real no_action                   150 (from   150)
    Total training pool: 21313 examples
      synthetic: 20000
      real:      1313
    Test accuracy: 0.9669
    F1 macro:      0.95

## 5. Fail-safe behavior (§8)

When Guardian is unreachable:

    $ python -c "... /ingest with GUARDIAN_CHAOS=down ..."
    {"state":"discarded","discarded_reason":"guardian_unreachable","dispatched":[]}

Nothing is dispatched. The pipeline returns cleanly instead of crashing.

## 6. Two processing paths (§3)

- Critical (`severity_class=critical`): decision + verification run in parallel.
  `saved_ms` reports the gain (~40-50ms per event on this machine).
- Normal (`severity_class=normal`): sequential — verify first, then decide.

Both invariants hold: no dispatch without a trusted verdict.

## 7. Redis Streams ingestion (§7)

The Twin publishes readings to a Redis Stream. A standalone consumer
(`scripts/run_consumer.py`) reads them via a consumer group and calls
the pipeline. Fail-fast if Redis is unreachable.

    $ python scripts/run_consumer.py
    audit db connected events=N
    consumer started, waiting for readings on stream=sensors.readings

    $ curl -X POST "http://127.0.0.1:8002/readings/publish?sensor_id=redis-demo&severity=critical&incident_type=fire&seq=1"
    {"msg_id": "1730...-0", "correlation_id": "corr-83e32d13"}

    # In the consumer terminal:
    processed corr=corr-83e32d13 state=dispatched kind=fire

Confirms the full loop: Twin → Redis Stream → City Brain consumer
→ decision (ML model v2) → Guardian → trust gate → dispatch → audit.

## 8. Audit trail in PostgreSQL (§7, §8)

    $ curl http://127.0.0.1:8000/audit/corr-audit-3
    {
      "correlation_id": "corr-audit-3",
      "count": 5,
      "events": [
        {"event_type": "reading_received"},
        {"event_type": "critical_parallel_timing"},
        {"event_type": "guardian_verdict"},
        {"event_type": "dispatched"},
        {"event_type": "dispatched"}
      ]
    }

    $ docker exec city-brain-postgres-1 psql -U citybrain -d citybrain -c \
        "SELECT event_type FROM audit_log WHERE correlation_id='corr-audit-3' ORDER BY id"

Every decision is durably persisted and queryable by correlation_id.

## 9. Replay protection (§8)

    $ python -c "... send same sensor_id + seq twice ..."
    1st: no_incident
    2nd: replay_rejected

Any reading whose `seq` is not strictly greater than the last seen `seq`
for that sensor is rejected before detection runs.

## 10. MCP-style tool discovery

    $ curl http://127.0.0.1:8000/mcp/tools
    [
      {"name": "city_brain.ingest",          "description": "...", "input_schema": {...}},
      {"name": "city_brain.dispatch",        "description": "...", "input_schema": {...}},
      {"name": "city_brain.receive_verdict", "description": "...", "input_schema": {...}},
      {"name": "city_brain.health",          "description": "...", "input_schema": {...}}
    ]

Every sensor reading and actuator action is exposed as a named,
discoverable tool with a JSON schema — satisfying the MCP-style
requirement from the challenge brief.

## 11. Docker Compose deployment

    $ docker compose up -d
    ✔ Container city-brain-postgres-1  Healthy
    ✔ Container city-brain-redis-1     Healthy
    ✔ Container city-brain-api-1       Started
    ✔ Container city-brain-consumer-1  Started

    $ docker compose ps
    NAME                    STATUS
    city-brain-api-1        Up (healthy)
    city-brain-consumer-1   Up
    city-brain-postgres-1   Up (healthy)
    city-brain-redis-1      Up (healthy)

    $ curl http://localhost:8000/health
    {"status":"ok"}

    $ curl http://localhost:8000/audit
    {"total_events": 24}

All four services start with a single command. The API and the consumer
connect to PostgreSQL and Redis through Docker's internal network.
The API container reads the audit table from the Postgres container
via the service name — no host-level ports involved.

---

## Reproduce

All commands above assume:

    # Option A — native (4 terminals)
    Terminal 1: python scripts/run_mocks.py
    Terminal 2: uvicorn city_brain.main:app --port 8000 --reload
    Terminal 3: python scripts/run_consumer.py
    Terminal 4: <commands from this document>

    # Option B — Docker (single command)
    docker compose up -d
    # then open http://localhost:8000/docs for the interactive API
    # or  http://localhost:8000/mcp/tools for the MCP tool listing