"""Runtime configuration, loaded from environment / .env.

Every partner address and credential is configurable, so the Brain can be
pointed at the real Twin and Guardian on merge day without code changes.
Defaults run the Brain standalone against its contract-faithful mocks.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Storage (optional: the Brain runs memory-only without them)
    database_url: str = "postgresql://citybrain:citybrain@localhost:5433/citybrain"
    redis_url: str = "redis://localhost:6380/0"

    # ---- Guardian layer (aya883/guardian_layer contract) -------------------
    guardian_url: str = "http://127.0.0.1:8320"
    # Guardian's read-only Brain token (their GUARDIAN_TOKEN_BRAIN). Empty = no header.
    guardian_token_brain: str = ""
    # Token allowed to write the shared timeline (their admin GUARDIAN_TOKEN). Empty = no header.
    guardian_timeline_token: str = ""
    guardian_timeline_enabled: bool = True
    # "lookup": Guardian polls the Twin itself; Brain asks GET /verdicts/{observation_id}.
    # "push":   Brain sends POST /observations and uses the verdict it returns.
    guardian_mode: str = "lookup"
    guardian_verdict_wait_s: float = 4.0
    trust_threshold: float = 0.7

    # ---- Twin layer (dhouhaichaouii/CS-TSYP-TWIN-LAYER, twin-interface-0.1) --
    twin_url: str = "http://127.0.0.1:8310"
    # "rest": /api/v1/* routes.  "mcp": FastMCP tools over SSE.
    twin_transport: str = "rest"
    twin_mcp_url: str = "http://127.0.0.1:8000/sse"
    # The Twin's brain-role token (their TWIN_TOKEN_BRAIN). The default is the
    # Twin's documented LOCAL-DEV token; it only works when the Twin runs with --dev.
    twin_token_brain: str = "token-brain-secret-2026"
    twin_poll_interval_s: float = 1.0
    twin_poll_enabled: bool = True
    # True only while running against the bundled mocks (enables scenario buttons).
    mock_mode: bool = True
    # Built-in simulator: the Brain serves its own Twin and Guardian simulators at /sim/twin and
    # /sim/guardian (one program, one port) and talks to them. Set false to use external services.
    simulator: bool = True
    port: int = 8300

    # Redis Streams consumer (original ingestion path, still supported)
    consumer_group: str = "city-brain"
    consumer_name: str = "city-brain-1"
    sensor_stream: str = "sensors.readings"

    # Decision model
    model_path: str = "models/city_brain_v1.pkl"
    model_confidence_threshold: float = 0.6

    log_level: str = "INFO"


settings = Settings()
