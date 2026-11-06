"""Registry of MCP-style tools exposed by City Brain.

Each tool maps 1:1 to an HTTP endpoint, but is described with a
name + schema so other layers (Twin, Guardian, jury) can discover it
without reading our source code.

ARCHITECTURE.md §4 defines the payloads; this file names them.
"""

from __future__ import annotations

from city_brain.mcp.schemas import MCPTool

# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------
# Hand-written JSON Schemas, mirroring src/city_brain/schemas/*.
# Kept inline (not auto-generated) so the contract is explicit and auditable.
# ---------------------------------------------------------------------------

_SENSOR_READING_SCHEMA = {
    "type": "object",
    "required": [
        "sensor_id", "type", "ts", "seq", "sig",
        "severity_class", "correlation_id", "zone",
    ],
    "properties": {
        "sensor_id": {"type": "string"},
        "type": {
            "type": "string",
            "enum": ["traffic", "water", "power", "air", "emergency_call"],
        },
        "value": {"type": ["number", "string", "null"]},
        "unit": {"type": ["string", "null"]},
        "ts": {"type": "string", "format": "date-time"},
        "seq": {"type": "integer", "minimum": 0},
        "sig": {"type": "string"},
        "severity_class": {"type": "string", "enum": ["critical", "normal"]},
        "correlation_id": {"type": "string"},
        "zone": {"type": "string"},
        "emergency": {
            "type": ["object", "null"],
            "properties": {
                "incident_type": {"type": "string"},
                "reported_location": {"type": ["string", "null"]},
                "caller_info": {"type": ["string", "null"]},
            },
        },
    },
}

_GUARDIAN_VERDICT_SCHEMA = {
    "type": "object",
    "required": [
        "correlation_id", "sensor_id", "ts", "seq",
        "trust_score", "verdict", "sig",
    ],
    "properties": {
        "correlation_id": {"type": "string"},
        "sensor_id": {"type": "string"},
        "ts": {"type": "string", "format": "date-time"},
        "seq": {"type": "integer"},
        "trust_score": {"type": "number", "minimum": 0, "maximum": 1},
        "verdict": {
            "type": "string",
            "enum": ["trusted", "suspicious", "rejected"],
        },
        "sig": {"type": "string"},
        "reason_codes": {"type": "array", "items": {"type": "string"}},
    },
}

_DISPATCH_COMMAND_SCHEMA = {
    "type": "object",
    "required": [
        "actuator_id", "action", "correlation_id",
        "trust_score", "requested_by",
    ],
    "properties": {
        "actuator_id": {"type": "string"},
        "action": {"type": "string"},
        "params": {"type": "object"},
        "correlation_id": {"type": "string"},
        "trust_score": {"type": "number", "minimum": 0, "maximum": 1},
        "requested_by": {"type": "string"},
    },
}

_PIPELINE_RESULT_SCHEMA = {
    "type": "object",
    "required": ["correlation_id", "state"],
    "properties": {
        "correlation_id": {"type": "string"},
        "state": {
            "type": "string",
            "enum": [
                "detected", "decided_held", "verified",
                "decided", "dispatched", "discarded",
            ],
        },
        "incident_kind": {
            "type": ["string", "null"],
            "enum": ["accident", "fire", "traffic_jam", "flood", None],
        },
        "verdict": {"type": ["object", "null"]},
        "dispatched": {"type": "array", "items": {"type": "object"}},
        "discarded_reason": {"type": ["string", "null"]},
    },
}


TOOLS: list[MCPTool] = [
    MCPTool(
        name="city_brain.ingest",
        description=(
            "Feed a sensor reading into the City Brain pipeline. "
            "Runs detection, decision (ML model), Guardian verification, "
            "trust gate, and dispatch. Returns the pipeline result."
        ),
        input_schema=_SENSOR_READING_SCHEMA,
        output_schema=_PIPELINE_RESULT_SCHEMA,
    ),
    MCPTool(
        name="city_brain.dispatch",
        description=(
            "Send an actuator dispatch command to the Twin. "
            "Called internally by the pipeline after Guardian approval."
        ),
        input_schema=_DISPATCH_COMMAND_SCHEMA,
        output_schema={
            "type": "object",
            "properties": {
                "correlation_id": {"type": "string"},
                "actuator_id": {"type": "string"},
                "status": {"type": "string"},
            },
        },
    ),
    MCPTool(
        name="city_brain.receive_verdict",
        description=(
            "Receive a Guardian verdict on a previously-seen reading. "
            "Used on the critical path (§3.1) to release a held decision."
        ),
        input_schema=_GUARDIAN_VERDICT_SCHEMA,
        output_schema={
            "type": "object",
            "properties": {"received": {"type": "string"}},
        },
    ),
    MCPTool(
        name="city_brain.health",
        description="Liveness probe. Returns the City Brain service status.",
        input_schema={"type": "object", "properties": {}},
        output_schema={
            "type": "object",
            "properties": {"status": {"type": "string"}},
        },
    ),
]


def get_tools() -> list[MCPTool]:
    """Return all registered MCP-style tools."""
    return TOOLS