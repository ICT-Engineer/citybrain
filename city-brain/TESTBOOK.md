# Test Book · City Brain layer

Every scenario has one trigger command, and each run writes `logs/<id>-<UTC time>.log` (R2). Each log holds the timestamped timeline (R3), the decisions, the offline vault state and a PASS/FAIL line for every expected output below.

**Status:** these are the City Brain scenarios. They run against the Brain's built-in Twin and Guardian simulators (R1). The layers are **not merged yet**. The Twin team's own scenarios T5 and SCN-02 … SCN-06 are reproduced here with the same names and the same expected Brain behaviour, so the team can rerun them against the real layers on merge day.

| ID | Name | Last result |
|---|---|---|
| CALM-01 | Calm day: the Brain must not overreact | PASS (`logs/`) |
| T5 | Fake flood at the port | PASS (`logs/`) |
| SCN-02 | Real flash flood: start the linked pump | PASS (`logs/`) |
| SCN-03 | Dead and frozen sensors: use the backup | PASS (`logs/`) |
| SCN-04 | Quarantined pump: use the backup pump | PASS (`logs/`) |
| SCN-05 | Real accident and spoofed air alarm | PASS (`logs/`) |
| SCN-06 | Guardian outage: hold, never fabricate | PASS (`logs/`) |
| AIR-01 | Industrial smog: air model curtails the plant | PASS (`logs/`) |
| AIR-02 | Downtown haze: advisory only | PASS (`logs/`) |
| NET-01 | Link lost after deciding: offline vault | PASS (`logs/`) |
| SEC-01 | Command shield: tampering, replay, bad commands | PASS (`logs/`) |
| INP-01 | Malformed partner input | PASS (`logs/`) |
| EMG-01 | Emergency call: route responders | PASS (`logs/`) |

Run everything with `python scenarios/run.py --all` (on Windows, `RUN_ALL_SCENARIOS.cmd`).

## CALM-01 · Calm day: the Brain must not overreact

| Field | Value |
|---|---|
| ID | CALM-01 |
| Name | Calm day: the Brain must not overreact |
| Layers involved | Twin, Guardian, Brain |
| Description | Normal city for 60 ticks: water, traffic, temperature, flow, pressure and PM2.5 all within limits. |
| Trigger | `python scenarios/run.py --scenario calm-01` |
| Expected: Twin | 16 sensors and 5 air monitors report values inside their normal_limits. |
| Expected: Guardian | Every reading TRUSTED. |
| Expected: Brain | No incident, no actuator command. |
| How to verify | Log: no `decision` or `action` events; checks PASS. |

## T5 · Fake flood at the port

| Field | Value |
|---|---|
| ID | T5 |
| Name | Fake flood at the port |
| Layers involved | Twin, Guardian, Brain |
| Description | PRT-WLV-01 jumps from 50 cm to 180 cm while its redundant partner PRT-WLV-02 stays normal (forged reading). |
| Trigger | `python scenarios/run.py --scenario t5` |
| Expected: Twin | PRT-WLV-01 reports 180 cm; PRT-WLV-02 stays around 50 cm. |
| Expected: Guardian | UNTRUSTED (BACKUP_DISAGREEMENT, trust score falls to 0), then PRT-WLV-01 quarantined. |
| Expected: Brain | Flood response refused (DISCARDED), no pump or valve command; switches to backup PRT-WLV-02. |
| How to verify | Timeline chain READING → VERDICT UNTRUSTED → DECISION refused, no ACTION; `/city` shows the sensor red. |

## SCN-02 · Real flash flood: start the linked pump

| Field | Value |
|---|---|
| ID | SCN-02 |
| Name | Real flash flood: start the linked pump |
| Layers involved | Twin, Guardian, Brain |
| Description | Water rises on both port sensors (PRT-WLV-01/02) above 80 cm, then drains. |
| Trigger | `python scenarios/run.py --scenario scn-02` |
| Expected: Twin | Both sensors over 80 cm; PRT-PMP-01 running after the command; water back to normal. |
| Expected: Guardian | TRUSTED (pair agrees). |
| Expected: Brain | START PRT-PMP-01 once (no double response), STOP it when the water is normal again. |
| How to verify | Log: ACTION `PRT-PMP-01 start APPLIED` then `stop APPLIED`; pump blinks green on `/city`. |

## SCN-03 · Dead and frozen sensors: use the backup

| Field | Value |
|---|---|
| ID | SCN-03 |
| Name | Dead and frozen sensors: use the backup |
| Layers involved | Twin, Guardian, Brain |
| Description | IND-TMP-01 dies (null value) while the industrial temperature rises; DTN-TRF-01 freezes on one value. |
| Trigger | `python scenarios/run.py --scenario scn-03` |
| Expected: Twin | IND-TMP-01 FAILED, IND-TMP-02 reports the overheat, DTN-TRF-01 repeats one value. |
| Expected: Guardian | IND-TMP-01 and DTN-TRF-01 UNTRUSTED/quarantined; IND-TMP-02 TRUSTED. |
| Expected: Brain | Ignore both bad streams, use IND-TMP-02, open coolant valve IND-VLV-01. |
| How to verify | Log: BACKUP_USED for IND-TMP-01, ACTION `IND-VLV-01 set_opening APPLIED`. |

## SCN-04 · Quarantined pump: use the backup pump

| Field | Value |
|---|---|
| ID | SCN-04 |
| Name | Quarantined pump: use the backup pump |
| Layers involved | Twin, Guardian, Brain |
| Description | Real flood while pump PRT-PMP-01 is quarantined by the Guardian. |
| Trigger | `python scenarios/run.py --scenario scn-04` |
| Expected: Twin | Refuses PRT-PMP-01 with GUARDIAN_RESTRICTED; accepts PRT-PMP-02. |
| Expected: Guardian | Flood readings TRUSTED; PRT-PMP-01 device trust quarantined. |
| Expected: Brain | Record the rejection, START PRT-PMP-02; PRT-PMP-01 never started. |
| How to verify | Log: ACTION REJECTED GUARDIAN_RESTRICTED then `PRT-PMP-02 start APPLIED`. |

## SCN-05 · Real accident and spoofed air alarm

| Field | Value |
|---|---|
| ID | SCN-05 |
| Name | Real accident and spoofed air alarm |
| Layers involved | Twin, Guardian, Brain |
| Description | Traffic surge on DTN-TRF-01/02 (real accident) while a fake sensor RES-AIR-TEST-01 claims dangerous air. |
| Trigger | `python scenarios/run.py --scenario scn-05` |
| Expected: Twin | Downtown traffic over its limit; RES-AIR-TEST-01 reports an impossible air value. |
| Expected: Guardian | Traffic TRUSTED; RES-AIR-TEST-01 UNTRUSTED (OUT_OF_RANGE). |
| Expected: Brain | Priority corridor on DTN-TLT-01; spoofed alarm refused, no command for it. |
| How to verify | Log: ACTION `DTN-TLT-01 set_priority_corridor APPLIED`; DISCARDED decision for RES-AIR-TEST-01. |

## SCN-06 · Guardian outage: hold, never fabricate

| Field | Value |
|---|---|
| ID | SCN-06 |
| Name | Guardian outage: hold, never fabricate |
| Layers involved | Twin, Guardian, Brain |
| Description | Guardian stops answering for 30 s while the port water is high. |
| Trigger | `python scenarios/run.py --scenario scn-06` |
| Expected: Twin | Water at 110 cm on both sensors. |
| Expected: Guardian | No verdict during the outage, then TRUSTED. |
| Expected: Brain | HELD during the outage (no command, nothing queued); START PRT-PMP-01 once the Guardian is back. |
| How to verify | Log: decision HELD with no commands, later ACTION `PRT-PMP-01 start APPLIED`. |

## AIR-01 · Industrial smog: air model curtails the plant

| Field | Value |
|---|---|
| ID | AIR-01 |
| Name | Industrial smog: air model curtails the plant |
| Layers involved | Twin, Guardian, Brain |
| Description | A strong PM2.5 source over the industrial zone; PLANT-01 adds to it. |
| Trigger | `python scenarios/run.py --scenario air-01` |
| Expected: Twin | AIR-IND-01 rises above 35 µg/m³, then falls once F-IND is open. |
| Expected: Guardian | TRUSTED. |
| Expected: Brain | Air model chooses curtail_industry: open feeder F-IND (cuts PLANT-01), never touch hospital feeder F-ESS; close F-IND when the air is clean. |
| How to verify | Log: decision `chosen_by: model` with curtail_industry, ACTION `F-IND open APPLIED`. |

## AIR-02 · Downtown haze: advisory only

| Field | Value |
|---|---|
| ID | AIR-02 |
| Name | Downtown haze: advisory only |
| Layers involved | Twin, Guardian, Brain |
| Description | Traffic haze downtown, no plant in that zone. |
| Trigger | `python scenarios/run.py --scenario air-02` |
| Expected: Twin | AIR-DTN-01 above 35 µg/m³. |
| Expected: Guardian | TRUSTED. |
| Expected: Brain | Health advisory; no curtailment (no plant to cut). |
| How to verify | Log: decision ADVISORY for AIR-DTN-01, no F-IND command. |

## NET-01 · Link lost after deciding: offline vault

| Field | Value |
|---|---|
| ID | NET-01 |
| Name | Link lost after deciding: offline vault |
| Layers involved | Twin, Guardian, Brain |
| Description | Uplink to the Twin is cut, then a real flood happens; the link comes back. |
| Trigger | `python scenarios/run.py --scenario net-01` |
| Expected: Twin | Unreachable during the cut; applies the command once the link is back. |
| Expected: Guardian | TRUSTED. |
| Expected: Brain | Decision SAVED_OFFLINE in data/offline/pending_decisions.jsonl (fsync, hash chain, HMAC seal); re-sent with the same request_id; vault marks it SENT. |
| How to verify | Log: `vault` events SAVED then SENT; vault integrity OK; pump blinks amber then green. |

## SEC-01 · Command shield: tampering, replay, bad commands

| Field | Value |
|---|---|
| ID | SEC-01 |
| Name | Command shield: tampering, replay, bad commands |
| Layers involved | Twin, Brain |
| Description | Six attacks on the Brain → Twin channel: out-of-range parameter, unknown device, tampered body, valid send, replay, unsigned command. |
| Trigger | `python scenarios/run.py --scenario sec-01` |
| Expected: Twin | Accepts only the valid signed command; refuses BODY_TAMPERED, NONCE_REPLAYED, SIGNATURE_MISSING (401). |
| Expected: Guardian | Not involved. |
| Expected: Brain | Blocks PARAMETER_OUT_OF_RANGE and UNKNOWN_DEVICE before anything leaves. |
| How to verify | Log: all six checks PASS; `POST /api/security/selftest` returns the codes. |

## INP-01 · Malformed partner input

| Field | Value |
|---|---|
| ID | INP-01 |
| Name | Malformed partner input |
| Layers involved | Twin, Brain |
| Description | The Twin feed carries five broken readings: text value, unknown type, negative seq, bad id, bad timestamp. |
| Trigger | `python scenarios/run.py --scenario inp-01` |
| Expected: Twin | Sends the five malformed rows alongside the 16 good ones. |
| Expected: Guardian | Skips the malformed rows. |
| Expected: Brain | Refuses each with a clear code (BAD_VALUE, UNKNOWN_TYPE, BAD_SEQ, BAD_SENSOR_ID, BAD_TIMESTAMP), keeps running, no command. |
| How to verify | Log: `input_rejected` events; `/api/overview.input_rejections` counts. |

## EMG-01 · Emergency call: route responders

| Field | Value |
|---|---|
| ID | EMG-01 |
| Name | Emergency call: route responders |
| Layers involved | Twin, Brain |
| Description | Operator reports an accident in the industrial zone. |
| Trigger | `python scenarios/run.py --scenario emg-01` |
| Expected: Twin | AMB-01 and POL-01 dispatched along R-LOW → R-IND, then released. |
| Expected: Guardian | Not involved (operator-confirmed call). |
| Expected: Brain | Dispatch ambulance and police on the shortest route of the Twin road graph (≤ 5 roads); release at mission end. |
| How to verify | Log: ACTION dispatch APPLIED for AMB-01 and POL-01 with route [R-LOW, R-IND]. |
