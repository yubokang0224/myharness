"""Agent management request/response schemas."""

from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field


class AgentResponse(BaseModel):
    name: str
    description: str
    system_prompt: str
    model: str | None = None
    provider_profile: str | None = None
    effort: str | None = None
    permission_mode: str | None = None
    tools: list[str] | None = None
    disallowed_tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list)
    color: str | None = None
    avatar: str | None = None
    max_turns: int | None = None
    source: Literal["builtin", "user"] = "user"
    kind: Literal["expert", "team"] = "expert"
    member_ids: list[str] = Field(default_factory=list)
    welcome_message: str | None = None


class CreateAgentRequest(BaseModel):
    name: str
    description: str = ""
    system_prompt: str = ""
    model: str | None = None
    provider_profile: str | None = None
    effort: str | None = None
    permission_mode: str | None = None
    tools: list[str] | None = None
    disallowed_tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list)
    color: str | None = None
    avatar: str | None = None
    max_turns: int | None = None
    kind: Literal["expert", "team"] = "expert"
    member_ids: list[str] = Field(default_factory=list)
    welcome_message: str | None = None


class UpdateAgentRequest(BaseModel):
    description: str | None = None
    system_prompt: str | None = None
    model: str | None = None
    provider_profile: str | None = None
    effort: str | None = None
    permission_mode: str | None = None
    tools: list[str] | None = None
    disallowed_tools: list[str] | None = None
    skills: list[str] | None = None
    mcp_servers: list[str] | None = None
    color: str | None = None
    avatar: str | None = None
    max_turns: int | None = None
    kind: Literal["expert", "team"] | None = None
    member_ids: list[str] | None = None
    welcome_message: str | None = None


class RunningAgentInfo(BaseModel):
    team_id: str
    agent_name: str
    status: Literal["active", "idle", "error"] = "active"
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentToolInfo(BaseModel):
    name: str
    description: str = ""
