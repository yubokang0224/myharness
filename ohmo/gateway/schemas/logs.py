"""Schemas for the process-log query endpoint."""

from __future__ import annotations

from pydantic import BaseModel, Field


class LogEntry(BaseModel):
    ts: float = 0.0
    time: str = ""
    level: str = "INFO"
    logger: str = ""
    trace_id: str = "-"
    message: str = ""
    exc: str | None = None


class LogPage(BaseModel):
    items: list[LogEntry] = Field(default_factory=list)
    source: str = "gateway"
    file: str = ""
    total_returned: int = 0
