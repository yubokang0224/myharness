"""Process-log query router.

Serves the JSONL log files written by ``ohmo.logging_setup`` so that
operators can inspect gateway / agent-API logs from the web UI without
shelling into the server.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ohmo.gateway.dependencies import _RuntimeState, get_current_user, get_runtime
from ohmo.gateway.schemas.logs import LogEntry, LogPage
from ohmo.logging_setup import log_file_path

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/logs", tags=["logs"])

_SOURCES = ("gateway", "agent-api")
_LEVEL_ORDER = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
_MAX_TAIL = 2000


def _iter_log_files(source: str, workspace: str | None) -> list[Path]:
    """Return log files for a source, oldest first (rotated backup, then current)."""
    current = log_file_path(source, workspace)
    files: list[Path] = []
    first_backup = current.with_name(current.name + ".1")
    if first_backup.exists():
        files.append(first_backup)
    if current.exists():
        files.append(current)
    return files


def _parse_line(line: str) -> LogEntry | None:
    line = line.strip()
    if not line:
        return None
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return LogEntry(
        ts=float(data.get("ts") or 0.0),
        time=str(data.get("time") or ""),
        level=str(data.get("level") or "INFO"),
        logger=str(data.get("logger") or ""),
        trace_id=str(data.get("trace_id") or "-"),
        message=str(data.get("message") or ""),
        exc=data.get("exc"),
    )


@router.get("", response_model=LogPage)
async def query_logs(
    _user: Annotated[dict, Depends(get_current_user)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
    source: str = Query("gateway"),
    tail: int = Query(200, ge=1, le=_MAX_TAIL),
    level: str | None = Query(None, description="Minimum level, e.g. WARNING"),
    q: str | None = Query(None, description="Substring filter on message/logger"),
    trace_id: str | None = Query(None),
    start_at: float | None = Query(None, ge=0),
    end_at: float | None = Query(None, ge=0),
):
    """Return the newest matching log lines (newest first)."""
    if source not in _SOURCES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"source must be one of {_SOURCES}",
        )
    min_level = None
    if level:
        min_level = _LEVEL_ORDER.get(level.upper())
        if min_level is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"level must be one of {sorted(_LEVEL_ORDER)}",
            )
    needle = (q or "").strip().lower()

    matched: list[LogEntry] = []
    files = _iter_log_files(source, runtime.workspace)
    # Newest lines win: walk files newest-first and stop once ``tail`` matched.
    for file_path in reversed(files):
        try:
            lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            logger.exception("Failed to read log file %s", file_path)
            continue
        for line in reversed(lines):
            entry = _parse_line(line)
            if entry is None:
                continue
            if min_level is not None and _LEVEL_ORDER.get(entry.level, 20) < min_level:
                continue
            if trace_id and entry.trace_id != trace_id:
                continue
            if start_at is not None and entry.ts < start_at:
                continue
            if end_at is not None and entry.ts > end_at:
                continue
            if needle and needle not in entry.message.lower() and needle not in entry.logger.lower():
                continue
            matched.append(entry)
            if len(matched) >= tail:
                break
        if len(matched) >= tail:
            break

    current = log_file_path(source, runtime.workspace)
    return LogPage(
        items=matched,
        source=source,
        file=str(current),
        total_returned=len(matched),
    )
