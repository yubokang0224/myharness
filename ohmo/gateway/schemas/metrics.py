"""Schemas for the daily-metrics endpoint."""

from __future__ import annotations

from pydantic import BaseModel, Field


class InvocationDayMetrics(BaseModel):
    date: str
    invocations: int = 0
    completed: int = 0
    errors: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    avg_duration_ms: int | None = None


class SessionDayMetrics(BaseModel):
    date: str
    sessions: int = 0
    messages: int = 0


class DailyMetrics(BaseModel):
    days: int = 0
    invocation_daily: list[InvocationDayMetrics] = Field(default_factory=list)
    session_daily: list[SessionDayMetrics] = Field(default_factory=list)
