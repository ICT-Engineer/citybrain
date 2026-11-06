# City Brain · Interface Card

*Version `brain-interface-1.0`. Partner contracts: Twin `twin-interface-0.1`, Guardian v0.4. All times are UTC ISO-8601.*

## Receives
| From | How | Message (example) |
|---|---|---|
| **Twin**: sensor readings | REST `GET {TWIN_URL}/api/v1/sensors`, polled every `TWIN_POLL_INTERVAL_S`. MCP option: tool `get_sensor_readings` over SSE | `{"ok":true,"run_id":"twin-run-8f3a","data":[{"sensor_id":"PRT-WLV-01","type":"water_level","reported_value":82.75,"unit":"cm","zone_id":"PRT","seq":62,"timestamp":"2026-11-06T21:08:11Z"}],"error":null}` |
| **Twin**: city state and events | `GET /api/v1/state`, `GET /api/v1/events?seq=` | device status, blocked roads, quarantined devices |
| **Twin**: command result | response to our `POST /api/v1/commands` | `{"ok":false,"error":{"code":"GUARDIAN_RESTRICTED","message":"PRT-PMP-01 is quarantined"}}` |
| **Guardian**: verdict per reading | `GET {GUARDIAN_URL}/verdicts/{observation_id}`, where `observation_id = run_id[-12:]-sensor_id-seq` | `{"observation_id":"…-PRT-WLV-01-62","verdict":"TRUSTED","trust_score":1.0,"reason_codes":[],"recommended_action":"NONE"}`. A 404 `OBSERVATION_UNKNOWN` means the reading has not been judged yet. |
| **Guardian**: device trust | `GET /devices/{id}/trust` | `{"device_id":"PRT-PMP-01","quarantined":true,"trust_score":0.31}` |
| **Operator**: emergency call | `POST /api/emergency?kind=accident&zone=IND` | marked `OPERATOR_CONFIRMED_CALL`; it is not sensor data |

## Sends
| To | How | Message (example) |
|---|---|---|
| **Twin**: actuator command | `POST {TWIN_URL}/api/v1/commands` (MCP: `send_actuator_command`) | `{"device_id":"PRT-PMP-01","command":"start","params":{"speed_pct":100},"request_id":"brain-39242ecc…","context":{"correlation_id":"…:PRT-WLV-01:62","observation_id":"…","guardian_verdict_id":"…","trust_score":1.0,"requested_by":"city-brain"}}` |
| **Guardian**: shared timeline | `POST /timeline/events`, layer `brain` | `{"layer":"brain","event":"brain_decision","observation_id":"…","outcome":"DISPATCHED"}` |
| **Anyone**: live timeline (R3) | `GET /api/timeline?since=<id>` (screen: `/timeline`) | `{"id":4,"timestamp":"2026-11-06T21:08:13.137Z","stage":"action","layer":"twin","correlation_id":"…","summary":"PRT-PMP-01 start APPLIED"}` |
| **Anyone**: state | `GET /api/overview`, `GET /api/vault`, `GET /health` | decisions, commands, metrics, offline vault status |

The Brain only sends these Twin commands: pump `start {speed_pct}` / `stop` / `set_speed`, valve `set_opening {open_pct}`, signal `set_priority_corridor` / `set_mode` / `set_phase`, feeder `open` / `close`, and responder `dispatch {destination_node_id, route_road_ids ≤ 5, mission_ref}` / `release`.

## Pointing at a partner (config only, no code change)
`SIMULATOR=true` (the default) uses the built-in simulators at `/sim/twin` and `/sim/guardian`. For real partners, set `SIMULATOR=false` and `MOCK_MODE=false` and fill in:
`TWIN_URL`, `TWIN_TRANSPORT=rest|mcp`, `TWIN_MCP_URL`, `TWIN_TOKEN_BRAIN`, `GUARDIAN_URL`, `GUARDIAN_MODE=lookup|push`, `GUARDIAN_TOKEN_BRAIN`, `GUARDIAN_TIMELINE_TOKEN`, `TRUST_THRESHOLD` (0.7), `GUARDIAN_VERDICT_WAIT_S` (4) and `PORT` (8300). See `.env.example`.

## Authentication and security
- **Calling the Twin:** `Authorization: Bearer <TWIN_TOKEN_BRAIN>`. The Twin enforces the brain role.
- **Signing every command:** HMAC-SHA256 over `METHOD\npath\ntimestamp\nnonce\nsha256(body)`, sent as `X-CityBrain-Key-Id`, `-Timestamp`, `-Nonce`, `-Body-SHA256` and `-Signature`. A receiver rejects requests with: `SIGNATURE_MISSING`, `SIGNATURE_INVALID`, `BODY_TAMPERED`, `SIGNATURE_EXPIRED` (more than 30 s old) or `NONCE_REPLAYED`. The key comes from `BRAIN_SIGNING_KEY` or the file `data/secrets/brain_signing.key`, and is never logged.
- **Calling the Guardian:** `Authorization: Bearer <GUARDIAN_TOKEN_BRAIN>`, read-only.
- **Before sending:** each command is checked against the catalog (device, command, parameter types and ranges, route on the road graph) and rate-limited (6 per device per minute). HTTPS is required for any host that is not localhost.
- **Input validation:** a malformed partner reading is refused with a clear code: `NOT_AN_OBJECT`, `MISSING_FIELD:x`, `BAD_SENSOR_ID`, `UNKNOWN_TYPE:x`, `BAD_VALUE`, `BAD_SEQ` or `BAD_TIMESTAMP`. Refusals are counted in `/api/overview.input_rejections` and appear on the timeline.

## Errors the Brain handles
| Error | What the Brain does |
|---|---|
| `GUARDIAN_RESTRICTED` | uses the backup actuator (PRT-PMP-01 → PRT-PMP-02) |
| `INVALID_STATE` | records the error; no retry |
| `IDEMPOTENCY_CONFLICT` | treats it as already applied |
| `NOT_AUTHORIZED` | shown on the Twin chip; no retry |
| Twin unreachable | commands are sealed in the offline vault and re-sent with the same `request_id` (expire after 5 min) |
| Guardian unreachable, or no verdict | outcome `HELD`: no action and nothing queued |
