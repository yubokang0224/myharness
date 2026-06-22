"""Invocation log request/response schemas."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class InvocationSummary(BaseModel):
    invocation_id: str
    session_id: str | None = None
    agent_name: str | None = None
    channel: str = "api"
    platform: str = "api"
    model: str = ""
    status: str = "completed"
    request_content: str | None = None
    response_text: str | None = None
    error: str | None = None
    created_at: float
    message_count: int = 0
    tool_call_count: int = 0


class InvocationDetail(InvocationSummary):
    cwd: str = ""
    system_prompt: str = ""
    messages: list[dict[str, Any]] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    status_messages: list[str] = Field(default_factory=list)
    permission_requests: list[dict[str, Any]] = Field(default_factory=list)
    tool_metadata: dict[str, Any] = Field(default_factory=dict)
