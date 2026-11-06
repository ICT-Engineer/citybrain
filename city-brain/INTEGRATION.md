# Merge-day guide (NOT done yet)

This Brain is **not connected** to the Twin or the Guardian. It runs alone with its built-in
simulators (`SIMULATOR=true`). When the team decides to merge, set `SIMULATOR=false` and the
settings below. No code changes.

## 1. Point the Brain at the real Twin
| Twin start command | Brain settings |
|---|---|
| `python tools/run_twin.py --dev` (Dashboard + MCP, one shared owner) | `TWIN_TRANSPORT=mcp`, `TWIN_MCP_URL=http://<twin-host>:8000/sse`, `TWIN_URL=http://<twin-host>:8000` |
| unified ASGI app (`src/twin_app.py`, REST + MCP on one port) | `TWIN_TRANSPORT=rest`, `TWIN_URL=http://<twin-host>:<port>` |
| always | `TWIN_TOKEN_BRAIN=<their brain token>` (the default only works with `--dev`), `MOCK_MODE=false` |

Twin prerequisite: their live simulation must be READY. On commit `a80f0a9` it is not
(13 of 16 sensors raise `INVALID_BINDING`); the 4-line `sensor-physics-fix.patch` fixes it.

## 2. Point the Brain at the real Guardian
`GUARDIAN_URL=http://<guardian-host>:8002`, `GUARDIAN_MODE=lookup`,
`GUARDIAN_TOKEN_BRAIN=<their GUARDIAN_TOKEN_BRAIN>`, and, to appear on their shared
timeline, `GUARDIAN_TIMELINE_TOKEN=<their admin token>`.
The Guardian must run with `GUARDIAN_PROFILE=twin` against the **same** Twin, so both layers
see the same run and observation ids (`{run_id[-12:]}-{sensor_id}-{seq}`).

Port note: the Guardian defaults to 8002 and the Twin's MCP to 8000. Brain stays on 8300.

## 3. Check before declaring the merge done
1. `GET /api/contract` on the Brain shows the partner URLs and `mock_mode: false`.
2. The dashboard header shows Twin and Guardian connected, and the run id is the Twin's.
3. Run the Twin's scenarios on the Twin side (`python scenarios/run.py --scenario t5`, `scn-02` ... `scn-06`)
   and compare the Brain's decisions with each scenario's "Expected: Brain".
4. Wrong token drill: a wrong `TWIN_TOKEN_BRAIN` must show `NOT_AUTHORIZED` in the Twin chip.

## What was verified without merging
- All 16 sensor map positions are identical to the live Twin dashboard (read from its DOM).
- Brain commands were sent to a real Twin MCP server once, as a compatibility check:
  priority corridor and ambulance dispatch `APPLIED`, idempotent replay, wrong token refused.
- Every command the Brain can plan passes the Twin's own `build_command_effect`.
- Verdicts produced by the real Guardian engine map correctly (TRUSTED / data problem / message problem).
