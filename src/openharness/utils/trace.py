"""Request tracing helpers: a contextvar-based trace id plus a logging filter.

A trace id is generated once at each entry point (channel inbound message,
HTTP request) and propagates through asyncio context to every log record
emitted while handling that message — LLM calls, tool execution, replies.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from uuid import uuid4

_trace_id_var: ContextVar[str | None] = ContextVar("openharness_trace_id", default=None)


def new_trace_id() -> str:
    """Return a fresh short trace id."""
    return uuid4().hex[:12]


def set_trace_id(trace_id: str | None = None) -> str:
    """Set the current trace id (generating one when omitted) and return it."""
    resolved = trace_id or new_trace_id()
    _trace_id_var.set(resolved)
    return resolved


def get_trace_id() -> str | None:
    """Return the trace id for the current context, if any."""
    return _trace_id_var.get()


def clear_trace_id() -> None:
    _trace_id_var.set(None)


class TraceIdFilter(logging.Filter):
    """Attach the current trace id to every record passing through a handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = get_trace_id() or "-"
        return True
