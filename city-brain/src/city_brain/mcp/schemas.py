"""MCP-style tool descriptor.

Mirrors the MCP tool schema: name, description, input_schema, output_schema.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class MCPTool(BaseModel):
    """A callable tool exposed by City Brain, MCP-style."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Fully-qualified tool name, e.g. city_brain.ingest")
    description: str = Field(..., description="What the tool does")
    input_schema: dict[str, Any] = Field(
        ..., description="JSON Schema of the tool's input"
    )
    output_schema: dict[str, Any] = Field(
        ..., description="JSON Schema of the tool's output"
    )