"""Task management router with SSE log streaming."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Annotated, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from ohmo.gateway.dependencies import get_current_user, get_runtime, _RuntimeState
from ohmo.gateway.schemas.tasks import (
    AutopilotRepoTaskResponse,
    CreateTaskRequest,
    TaskResponse,
    TaskStatus,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tasks", tags=["tasks"])


def _task_to_response(record) -> TaskResponse:
    return TaskResponse(
        id=record.id,
        type=record.type,
        status=record.status,
        description=record.description,
        cwd=record.cwd,
        command=record.command,
        prompt=record.prompt,
        created_at=record.created_at,
        started_at=record.started_at,
        ended_at=record.ended_at,
        return_code=record.return_code,
        metadata=record.metadata or {},
    )


@router.get("", response_model=list[TaskResponse])
async def list_tasks(
    _user: Annotated[dict, Depends(get_current_user)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
    task_status: TaskStatus | None = Query(None, alias="status"),
):
    """List all background tasks, optionally filtered by status."""
    if runtime.task_manager is None:
        return []
    tasks = list(runtime.task_manager._tasks.values())
    if task_status is not None:
        tasks = [t for t in tasks if t.status == task_status]
    tasks.sort(key=lambda t: t.created_at, reverse=True)
    return [_task_to_response(t) for t in tasks]


@router.post("", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
async def create_task(
    body: CreateTaskRequest,
    _user: Annotated[dict, Depends(get_current_user)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
):
    """Create and start a new background task."""
    if runtime.task_manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Task manager not initialized",
        )
    cwd = body.cwd or str(Path.cwd())
    if body.type == "local_bash":
        if not body.command:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="command is required for local_bash tasks",
            )
        record = await runtime.task_manager.create_shell_task(
            command=body.command,
            description=body.description,
            cwd=cwd,
        )
    else:
        if not body.prompt:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="prompt is required for agent tasks",
            )
        record = await runtime.task_manager.create_agent_task(
            prompt=body.prompt,
            description=body.description,
            cwd=cwd,
        )
    return _task_to_response(record)


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: str,
    _user: Annotated[dict, Depends(get_current_user)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
):
    """Get details of a specific task."""
    if runtime.task_manager is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Task manager not initialized")
    record = runtime.task_manager._tasks.get(task_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Task '{task_id}' not found")
    return _task_to_response(record)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_task(
    task_id: str,
    _user: Annotated[dict, Depends(get_current_user)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
):
    """Cancel or delete a task."""
    if runtime.task_manager is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Task manager not initialized")
    record = runtime.task_manager._tasks.get(task_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Task '{task_id}' not found")
    try:
        from openharness.tasks import stop_task

        await stop_task(task_id)
    except Exception as exc:
        logger.warning("Failed to stop task %s: %s", task_id, exc)
    runtime.task_manager._tasks.pop(task_id, None)


@router.get("/{task_id}/logs")
async def stream_task_logs(
    task_id: str,
    _user: Annotated[dict, Depends(get_current_user)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
):
    """Stream task output as SSE events."""
    if runtime.task_manager is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Task manager not initialized")
    record = runtime.task_manager._tasks.get(task_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Task '{task_id}' not found")

    output_file = record.output_file

    async def log_stream() -> AsyncIterator[str]:
        position = 0
        while True:
            current_record = runtime.task_manager._tasks.get(task_id)
            if current_record is None:
                break
            try:
                text = output_file.read_text(encoding="utf-8", errors="replace")
                if len(text) > position:
                    chunk = text[position:]
                    position = len(text)
                    yield f"data: {json.dumps({'text': chunk})}\n\n"
            except OSError:
                pass
            if current_record.status in ("completed", "failed", "killed"):
                yield f"data: {json.dumps({'event': 'done', 'status': current_record.status})}\n\n"
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(
        log_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Autopilot endpoints
# ---------------------------------------------------------------------------

@router.get("/autopilot/repos", response_model=list[AutopilotRepoTaskResponse])
async def list_autopilot_repos(
    _user: Annotated[dict, Depends(get_current_user)],
):
    """List autopilot repo task cards."""
    try:
        from openharness.autopilot.service import RepoAutopilotStore

        store = RepoAutopilotStore(cwd=Path.cwd())
        cards = store.list_cards()
    except Exception:
        cards = []
    return [
        AutopilotRepoTaskResponse(
            id=c.id,
            title=c.title,
            body=c.body,
            status=str(c.status),
            score=getattr(c, "score", None),
            source_kind=getattr(c, "source_kind", ""),
        )
        for c in cards
    ]


@router.post("/autopilot/start", status_code=status.HTTP_204_NO_CONTENT)
async def start_autopilot(
    _user: Annotated[dict, Depends(get_current_user)],
):
    """Start Autopilot mode (stub — integrate with your RepoAutopilotStore run loop)."""
    logger.info("Autopilot start requested via API")


@router.post("/autopilot/stop", status_code=status.HTTP_204_NO_CONTENT)
async def stop_autopilot(
    _user: Annotated[dict, Depends(get_current_user)],
):
    """Stop Autopilot mode."""
    logger.info("Autopilot stop requested via API")
