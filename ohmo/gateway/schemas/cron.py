"""Cron scheduled-job request/response schemas."""

from __future__ import annotations

from pydantic import BaseModel


class CronJobResponse(BaseModel):
    name: str
    schedule: str
    command: str
    cwd: str | None = None
    enabled: bool = True
    created_at: str | None = None
    next_run: str | None = None
    last_run: str | None = None
    last_status: str | None = None


class CreateCronJobRequest(BaseModel):
    name: str
    schedule: str
    command: str
    cwd: str | None = None
    enabled: bool = True


class SetCronJobEnabledRequest(BaseModel):
    enabled: bool


class CronHistoryEntryResponse(BaseModel):
    name: str
    command: str = ""
    started_at: str | None = None
    ended_at: str | None = None
    returncode: int | None = None
    status: str = ""
    stdout: str = ""
    stderr: str = ""


class CronSchedulerStatusResponse(BaseModel):
    running: bool
    pid: int | None = None
    total_jobs: int = 0
    enabled_jobs: int = 0
    log_file: str = ""
    history_file: str = ""
