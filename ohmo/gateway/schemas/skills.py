"""Skills and MCP request/response schemas."""

from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field


class SkillResponse(BaseModel):
    name: str
    description: str
    content: str
    source: str
    path: str | None = None
    enabled: bool = True


class CreateSkillRequest(BaseModel):
    name: str
    description: str
    content: str


class UpdateSkillRequest(BaseModel):
    description: str | None = None
    content: str | None = None


class McpServerResponse(BaseModel):
    name: str
    type: Literal["stdio", "http", "ws"]
    state: Literal["connected", "failed", "pending", "disabled"]
    detail: str = ""
    transport: str = "unknown"
    config: dict[str, Any] = Field(default_factory=dict)
    tools: list[dict[str, Any]] = Field(default_factory=list)


class AddMcpServerRequest(BaseModel):
    name: str
    config: dict[str, Any]
