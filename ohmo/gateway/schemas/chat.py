"""Chat-related request/response schemas."""

from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field


class CreateSessionRequest(BaseModel):
    agent_name: str | None = None
    title: str | None = None


class SessionInfo(BaseModel):
    id: str
    title: str
    agent_name: str | None = None
    created_at: float
    updated_at: float
    message_count: int = 0


class MessageRequest(BaseModel):
    content: str
    attachments: list[dict[str, Any]] = Field(default_factory=list)


class MessageInfo(BaseModel):
    id: str
    role: Literal["user", "assistant"]
    content: str
    created_at: float
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    attachments: list[dict[str, Any]] = Field(default_factory=list)


class MemoryInfo(BaseModel):
    entries: list[dict[str, Any]]
    files: list[str]


# SSE event payloads (serialized to JSON in the `data:` field)
class SSETextDelta(BaseModel):
    event: Literal["text_delta"] = "text_delta"
    text: str


class SSEToolCall(BaseModel):
    event: Literal["tool_call"] = "tool_call"
    tool_name: str
    tool_input: dict[str, Any]


class SSEToolResult(BaseModel):
    event: Literal["tool_result"] = "tool_result"
    tool_name: str
    output: str
    is_error: bool = False


class SSEDone(BaseModel):
    event: Literal["done"] = "done"
    usage: dict[str, Any] | None = None


class SSEError(BaseModel):
    event: Literal["error"] = "error"
    message: str
    recoverable: bool = True


class SSEStatus(BaseModel):
    event: Literal["status"] = "status"
    message: str


class SSEPermissionRequest(BaseModel):
    event: Literal["permission_request"] = "permission_request"
    tool_name: str
    reason: str
    request_id: str


class ApproveRequest(BaseModel):
    request_id: str
    approved: bool
