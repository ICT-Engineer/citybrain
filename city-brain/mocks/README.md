# Mocks

Fake Twin (port 8002) and Guardian (port 8001) for local development.

## Run both

    python scripts/run_mocks.py

## Run one manually

    uvicorn mocks.twin_mock:app --port 8002 --reload
    uvicorn mocks.guardian_mock:app --port 8001 --reload

## Chaos mode (Guardian)

Set `GUARDIAN_CHAOS` env var before starting:

    $env:GUARDIAN_CHAOS="rejected"   # PowerShell
    export GUARDIAN_CHAOS=rejected   # bash

Values: `off` (default), `suspicious`, `rejected`, `slow`, `down`.

Use to test the fail-safe default (ARCHITECTURE §8):
Guardian `down` or `slow` -> City Brain must NOT dispatch.