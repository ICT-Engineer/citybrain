# Test Book — Brain Layer

Scenarios are designed to prove the Brain layer works correctly, handles
adversarial conditions, and remains safe when a partner misbehaves.

Every scenario is runnable against the Brain's own mocks (R1) via:
```bash
python scripts/scenario_runner.py

Logs for each scenario are saved to logs/<scenario_id>.log.

T1 — Spoofed Reading (Brain -> Guardian)
Field	Value
ID	T1
Name	Spoofed reading bypass attempt
Layers involved	Twin (mock), Guardian, City Brain
Description	A reading with a forged sig is injected. Guardian rejects it. Brain must not dispatch.
Trigger	python scripts/scenario_runner.py (scenario 1)
Expected: Twin	Emits a reading with sig: "FORGED_SIGNATURE_0000"
Expected: Guardian	Returns verdict: "rejected", trust_score: 0.1 (mock chaos mode)
Expected: Brain	Returns state: "discarded", dispatched: []. Logs critical_near_miss if critical path.
How to verify	Check logs/T1.log for verdict_rejected. Check PostgreSQL audit_log table.
T2 — Frozen Sensor (Twin -> Brain)
Field	Value
ID	T2
Name	Frozen sensor replay
Layers involved	Twin (mock), City Brain
Description	The same reading (same sensor_id, same seq) is sent twice. The second must be rejected before detection.
Trigger	python scripts/scenario_runner.py (scenario 2)
Expected: Twin	Sends the same seq twice
Expected: Guardian	Not called for the second reading
Expected: Brain	First: no_incident. Second: discarded_reason: "replay_rejected"
How to verify	Check logs/T2.log for replay detected. Check /metrics for replays_rejected_total.
T3 — Guardian Unreachable (Brain -> Guardian -> Twin)
Field	Value
ID	T3
Name	Guardian failure fail-safe
Layers involved	Twin (mock), Guardian (down), City Brain
Description	Guardian is unreachable. Brain must fail-safe: no actuator dispatched.
Trigger	Restart mocks with GUARDIAN_CHAOS=down, then send a critical incident
Expected: Twin	Streams a critical accident
Expected: Guardian	Returns HTTP 500 (chaos mode)
Expected: Brain	state: "discarded", discarded_reason: "guardian_unreachable", dispatched: []
How to verify	logs/T3.log shows guardian.verify failed. /metrics shows discards_by_reason: {"guardian_unreachable": 1}
T4 — Duplicate Critical Event (Twin -> Brain -> Twin)
Field	Value
ID	T4
Name	Duplicate dispatch prevention
Layers involved	Twin (mock), Guardian, City Brain
Description	The same critical incident is sent twice with different correlation_ids but same seq. Only the first must dispatch.
Trigger	python scripts/scenario_runner.py (scenario 4)
Expected: Twin	Receives exactly 2 dispatch commands (ambulance + police), not 4
Expected: Guardian	Verifies both readings as trusted
Expected: Brain	First: dispatched: [ambulance, police]. Second: discarded_reason: "replay_rejected"
How to verify	GET /actuator/dispatched returns exactly 2 entries. logs/T4.log shows 1 dispatch, 1 replay reject
T5 — Replayed Reading (Twin -> Brain)
Field	Value
ID	T5
Name	Older reading replay
Layers involved	Twin (mock), City Brain
Description	An older reading (lower seq) is sent after a newer one. The Brain must reject it as a replay.
Trigger	python scripts/scenario_runner.py (scenario 5)
Expected: Twin	Sends seq=100, then seq=50 for the same sensor
Expected: Guardian	Not called for the second
Expected: Brain	Second reading: discarded_reason: "replay_rejected"
How to verify	logs/T5.log shows replay detected sensor=... incoming_seq=50 last_seq=100