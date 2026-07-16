"""Chat-related request/response schemas."""

from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field


class CreateSessionRequest(BaseModel):
    agent_name: str | None = None
    title: str | None = None
    persist_mode: Literal["session", "log", "none"] | None = None


class SessionInfo(BaseModel):
    id: str
    title: str
    agent_name: str | None = None
    conversation_id: str | None = None
    session_key: str | None = None
    channel: str = "web"
    platform: str = "web"
    bot_name: str | None = None
    chat_id: str | None = None
    sender_id: str | None = None
    sender_name: str | None = None
    created_at: float
    updated_at: float
    message_count: int = 0


class KbPreference(BaseModel):
    """Per-message knowledge-base retrieval preference set by the client UI."""

    enabled: bool = True
    namespaces: list[str] = Field(default_factory=list)


class MessageRequest(BaseModel):
    content: str
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    response_format: Literal["text", "json"] = "text"
    persist_mode: Literal["session", "log", "none"] | None = None
    kb: KbPreference | None = None


class PermissionRequestInfo(BaseModel):
    tool_name: str
    reason: str
    request_id: str


class MessageSyncResponse(BaseModel):
    session_id: str
    status: Literal["completed", "error"]
    text: str
    invocation_id: str | None = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    status_messages: list[str] = Field(default_factory=list)
    permission_requests: list[PermissionRequestInfo] = Field(default_factory=list)
    usage: dict[str, Any] | None = None
    error: str | None = None
    recoverable: bool | None = None


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


class ArtifactInfo(BaseModel):
    id: str
    name: str
    path: str
    relative_path: str = ""
    extension: str = ""
    mime_type: str = "application/octet-stream"
    size: int = 0
    updated_at: float = 0.0
    tool_name: str | None = None
    tool_use_id: str | None = None
    preview_kind: Literal["code", "text", "markdown", "html", "pdf", "word", "image", "binary"] = "binary"


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
    metadata: dict[str, Any] | None = None


class SSEArtifact(BaseModel):
    event: Literal["artifact_created"] = "artifact_created"
    artifact: ArtifactInfo


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


class SSECompact(BaseModel):
    """Context-compaction progress so the client can show a persistent indicator."""

    event: Literal["compact"] = "compact"
    phase: str
    trigger: str = "auto"
    message: str | None = None
    attempt: int | None = None


class SSEPermissionRequest(BaseModel):
    event: Literal["permission_request"] = "permission_request"
    tool_name: str
    reason: str
    request_id: str


class ApproveRequest(BaseModel):
    request_id: str
    approved: bool
    scope: Literal["once", "session"] = "once"
