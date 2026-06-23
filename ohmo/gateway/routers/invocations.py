"""Invocation-log router."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ohmo.gateway.dependencies import _RuntimeState, get_current_user, get_runtime
from ohmo.gateway.schemas.invocations import InvocationDetail, InvocationPage, InvocationSummary
from ohmo.session_storage import count_invocation_records, list_invocation_records, load_invocation_record

router = APIRouter(prefix="/invocations", tags=["invocations"])


def _summary_from_record(record: dict) -> InvocationSummary:
    return InvocationSummary(
        invocation_id=str(record.get("invocation_id") or ""),
        session_id=record.get("session_id"),
        agent_name=record.get("agent_name"),
        channel=record.get("channel") or "api",
        platform=record.get("platform") or record.get("channel") or "api",
        model=record.get("model") or "",
        status=record.get("status") or "completed",
        request_content=record.get("request_content"),
        response_text=record.get("response_text"),
        error=record.get("error"),
        created_at=float(record.get("created_at") or 0.0),
        message_count=int(record.get("message_count") or 0),
        tool_call_count=int(record.get("tool_call_count") or 0),
    )


@router.get("", response_model=InvocationPage)
async def list_invocations(
    _user: Annotated[dict, Depends(get_current_user)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    limit: int | None = Query(None, ge=1, le=200),
    agent_name: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    start_at: float | None = Query(None, ge=0),
    end_at: float | None = Query(None, ge=0),
):
    """List invocation logs that were persisted outside chat sessions."""
    resolved_page_size = limit or page_size
    offset = (page - 1) * resolved_page_size
    items = [
        _summary_from_record(record)
        for record in list_invocation_records(
            workspace=runtime.workspace,
            limit=resolved_page_size,
            offset=offset,
            agent_name=agent_name,
            status=status_filter,
            start_at=start_at,
            end_at=end_at,
        )
    ]
    total = count_invocation_records(
        workspace=runtime.workspace,
        agent_name=agent_name,
        status=status_filter,
        start_at=start_at,
        end_at=end_at,
    )
    return InvocationPage(items=items, total=total, page=page, page_size=resolved_page_size)


@router.get("/{invocation_id}", response_model=InvocationDetail)
async def get_invocation(
    invocation_id: str,
    _user: Annotated[dict, Depends(get_current_user)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
):
    """Return one invocation log with full messages and tool calls."""
    record = load_invocation_record(runtime.workspace, invocation_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invocation not found")
    summary = _summary_from_record(
        {
            **record,
            "tool_call_count": len(record.get("tool_calls", [])) if isinstance(record.get("tool_calls"), list) else 0,
        }
    )
    return InvocationDetail(
        **summary.model_dump(),
        cwd=record.get("cwd") or "",
        system_prompt=record.get("system_prompt") or "",
        messages=record.get("messages") if isinstance(record.get("messages"), list) else [],
        usage=record.get("usage") if isinstance(record.get("usage"), dict) else {},
        tool_calls=record.get("tool_calls") if isinstance(record.get("tool_calls"), list) else [],
        status_messages=record.get("status_messages") if isinstance(record.get("status_messages"), list) else [],
        permission_requests=(
            record.get("permission_requests") if isinstance(record.get("permission_requests"), list) else []
        ),
        tool_metadata=record.get("tool_metadata") if isinstance(record.get("tool_metadata"), dict) else {},
    )
