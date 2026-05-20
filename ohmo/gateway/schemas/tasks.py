"""Task management request/response schemas."""

from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field


TaskStatus = Literal["pending", "running", "completed", "failed", "killed"]
TaskType = Literal["local_bash", "local_agent", "remote_agent"]


class TaskResponse(BaseModel):
    id: str
    type: TaskType
    status: TaskStatus
    description: str
    cwd: str
    command: str | None = None
    prompt: str | None = None
    created_at: float
    started_at: float | None = None
    ended_at: float | None = None
    return_code: int | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class CreateTaskRequest(BaseModel):
    description: str
    prompt: str | None = None
    command: str | None = None
    type: TaskType = "local_agent"
    agent_name: str | None = None
    cwd: str | None = None


class AutopilotRepoTaskResponse(BaseModel):
    id: str
    title: str
    body: str
    status: str
    score: float | None = None
    source_kind: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
