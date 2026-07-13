"""Daily aggregated metrics over persisted invocation/session records.

Deliberately file-based (no Prometheus): scans the workspace JSON records and
aggregates per calendar day so the web UI can chart call volume, error rate,
token spend, and latency without extra infrastructure.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from ohmo.gateway.dependencies import _RuntimeState, get_current_user, get_runtime
from ohmo.gateway.schemas.metrics import DailyMetrics, InvocationDayMetrics, SessionDayMetrics
from ohmo.session_storage import list_invocation_records, list_snapshots, matches_agent_filter

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/metrics", tags=["metrics"])

_MAX_RECORDS = 20000


def _day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


@router.get("/daily", response_model=DailyMetrics)
async def daily_metrics(
    _user: Annotated[dict, Depends(get_current_user)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
    days: int = Query(14, ge=1, le=90),
    agent_name: str | None = Query(None),
):
    """Aggregate invocation and session records per day, newest day first."""
    cutoff = time.time() - days * 86400

    inv_days: dict[str, dict] = {}
    durations: dict[str, list[int]] = {}
    for record in list_invocation_records(
        workspace=runtime.workspace,
        limit=_MAX_RECORDS,
        offset=0,
        agent_name=agent_name,
        start_at=cutoff,
    ):
        day = _day_of(float(record.get("created_at") or 0.0))
        bucket = inv_days.setdefault(
            day,
            {
                "invocations": 0,
                "completed": 0,
                "errors": 0,
                "tool_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
            },
        )
        bucket["invocations"] += 1
        status = str(record.get("status") or "completed")
        if status in ("error", "failed") or record.get("error"):
            bucket["errors"] += 1
        else:
            bucket["completed"] += 1
        bucket["tool_calls"] += int(record.get("tool_call_count") or 0)
        usage = record.get("usage") if isinstance(record.get("usage"), dict) else {}
        bucket["input_tokens"] += int(usage.get("input_tokens") or 0)
        bucket["output_tokens"] += int(usage.get("output_tokens") or 0)
        duration = record.get("duration_ms")
        if isinstance(duration, (int, float)) and duration >= 0:
            durations.setdefault(day, []).append(int(duration))

    invocation_daily = []
    for day in sorted(inv_days, reverse=True):
        bucket = inv_days[day]
        day_durations = durations.get(day, [])
        invocation_daily.append(
            InvocationDayMetrics(
                date=day,
                avg_duration_ms=(sum(day_durations) // len(day_durations)) if day_durations else None,
                **bucket,
            )
        )

    sess_days: dict[str, dict] = {}
    try:
        for snapshot in list_snapshots(runtime.workspace, limit=_MAX_RECORDS):
            created_at = float(snapshot.get("created_at") or 0.0)
            if created_at < cutoff:
                continue
            if not matches_agent_filter(snapshot.get("agent_name"), agent_name):
                continue
            day = _day_of(created_at)
            bucket = sess_days.setdefault(day, {"sessions": 0, "messages": 0})
            bucket["sessions"] += 1
            bucket["messages"] += int(snapshot.get("message_count") or 0)
    except Exception:
        logger.exception("Failed to aggregate session snapshots")

    session_daily = [
        SessionDayMetrics(date=day, **sess_days[day]) for day in sorted(sess_days, reverse=True)
    ]

    return DailyMetrics(
        days=days,
        invocation_daily=invocation_daily,
        session_daily=session_daily,
    )
