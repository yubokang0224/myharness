"""Cron scheduled-jobs router: registry display, execution history, controls."""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ohmo.gateway.dependencies import get_current_user
from ohmo.gateway.schemas.cron import (
    CreateCronJobRequest,
    CronHistoryEntryResponse,
    CronJobResponse,
    CronSchedulerStatusResponse,
    SetCronJobEnabledRequest,
)
from openharness.services.cron import (
    delete_cron_job,
    get_cron_job,
    load_cron_jobs,
    set_job_enabled,
    upsert_cron_job,
    validate_cron_expression,
)
from openharness.services.cron_scheduler import (
    execute_job,
    load_history,
    scheduler_status,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/cron", tags=["cron"])

# Strong references so fire-and-forget "run now" tasks are not garbage-collected
_manual_runs: set[asyncio.Task] = set()


def _job_to_response(job: dict[str, Any]) -> CronJobResponse:
    return CronJobResponse(
        name=str(job.get("name", "")),
        schedule=str(job.get("schedule", "")),
        command=str(job.get("command", "")),
        cwd=job.get("cwd"),
        enabled=bool(job.get("enabled", True)),
        created_at=job.get("created_at"),
        next_run=job.get("next_run"),
        last_run=job.get("last_run"),
        last_status=job.get("last_status"),
    )


@router.get("/jobs", response_model=list[CronJobResponse])
async def list_jobs(
    _user: Annotated[dict, Depends(get_current_user)],
):
    """List all registered cron jobs."""
    return [_job_to_response(job) for job in load_cron_jobs()]


@router.post("/jobs", response_model=CronJobResponse, status_code=status.HTTP_201_CREATED)
async def create_job(
    body: CreateCronJobRequest,
    _user: Annotated[dict, Depends(get_current_user)],
):
    """Create or replace a cron job."""
    if not validate_cron_expression(body.schedule):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid cron expression: {body.schedule!r}",
        )
    upsert_cron_job(
        {
            "name": body.name,
            "schedule": body.schedule,
            "command": body.command,
            "cwd": body.cwd,
            "enabled": body.enabled,
        }
    )
    job = get_cron_job(body.name)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Job '{body.name}' was not persisted",
        )
    return _job_to_response(job)


@router.put("/jobs/{name}/enabled", response_model=CronJobResponse)
async def set_enabled(
    name: str,
    body: SetCronJobEnabledRequest,
    _user: Annotated[dict, Depends(get_current_user)],
):
    """Enable or disable a cron job."""
    if not set_job_enabled(name, body.enabled):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Cron job '{name}' not found",
        )
    job = get_cron_job(name)
    return _job_to_response(job or {"name": name, "enabled": body.enabled})


@router.post("/jobs/{name}/run", status_code=status.HTTP_202_ACCEPTED)
async def run_job_now(
    name: str,
    _user: Annotated[dict, Depends(get_current_user)],
):
    """Trigger a cron job immediately (runs in the background)."""
    job = get_cron_job(name)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Cron job '{name}' not found",
        )
    task = asyncio.create_task(execute_job(job))
    _manual_runs.add(task)
    task.add_done_callback(_manual_runs.discard)
    logger.info("Manual run triggered for cron job %r", name)
    return {"detail": f"Job '{name}' started"}


@router.delete("/jobs/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_job(
    name: str,
    _user: Annotated[dict, Depends(get_current_user)],
):
    """Delete a cron job."""
    if not delete_cron_job(name):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Cron job '{name}' not found",
        )


@router.get("/history", response_model=list[CronHistoryEntryResponse])
async def get_history(
    _user: Annotated[dict, Depends(get_current_user)],
    limit: int = Query(50, ge=1, le=500),
    job_name: str | None = Query(None),
):
    """Return recent execution records, newest first."""
    entries = load_history(limit=limit, job_name=job_name)
    entries.reverse()
    return [
        CronHistoryEntryResponse(
            name=str(entry.get("name", "")),
            command=str(entry.get("command", "")),
            started_at=entry.get("started_at"),
            ended_at=entry.get("ended_at"),
            returncode=entry.get("returncode"),
            status=str(entry.get("status", "")),
            stdout=str(entry.get("stdout", "")),
            stderr=str(entry.get("stderr", "")),
        )
        for entry in entries
    ]


@router.get("/status", response_model=CronSchedulerStatusResponse)
async def get_status(
    _user: Annotated[dict, Depends(get_current_user)],
):
    """Return scheduler daemon status."""
    return CronSchedulerStatusResponse(**scheduler_status())
