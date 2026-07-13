"""Structured logging setup shared by ohmo processes (gateway / agent API).

Every process gets two sinks:

- console (stderr): human-readable, ends up in journald under systemd
- ``<workspace>/logs/<process>.jsonl``: JSON Lines with rotation, consumed by
  the ``/logs`` HTTP endpoint and offline analysis

Both sinks carry the per-message trace id (see ``openharness.utils.trace``).
"""

from __future__ import annotations

import json
import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from openharness.utils.trace import TraceIdFilter

from ohmo.workspace import get_logs_dir

_CONSOLE_FORMAT = "%(asctime)s [%(name)s] %(levelname)s [%(trace_id)s] %(message)s"
_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 5


class JsonLinesFormatter(logging.Formatter):
    """Serialize log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": round(record.created, 3),
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
            + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "trace_id": getattr(record, "trace_id", "-"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def log_file_path(process_name: str, workspace: str | Path | None = None) -> Path:
    return get_logs_dir(workspace) / f"{process_name}.jsonl"


def configure_process_logging(
    process_name: str,
    *,
    workspace: str | Path | None = None,
    level: int = logging.INFO,
) -> None:
    """Configure console + rotating JSONL file logging for this process."""
    logging.basicConfig(level=level, format=_CONSOLE_FORMAT, force=True)

    trace_filter = TraceIdFilter()
    root = logging.getLogger()
    for handler in root.handlers:
        handler.addFilter(trace_filter)

    try:
        file_path = log_file_path(process_name, workspace)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            file_path,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
            delay=True,
        )
        file_handler.setFormatter(JsonLinesFormatter())
        file_handler.addFilter(trace_filter)
        root.addHandler(file_handler)
    except OSError:
        logging.getLogger(__name__).exception(
            "Could not attach file log handler for %s", process_name
        )

    _attach_alert_handler(process_name, workspace, root)


def _attach_alert_handler(
    process_name: str,
    workspace: str | Path | None,
    root: logging.Logger,
) -> None:
    try:
        from ohmo.alerting import EmailAlertHandler
        from ohmo.gateway.config import load_gateway_config

        alerting = load_gateway_config(workspace).alerting
        if not alerting.enabled:
            return
        root.addHandler(EmailAlertHandler(alerting, process_name=process_name))
        logging.getLogger(__name__).info(
            "Email alerting enabled recipients=%s", ",".join(alerting.to_addresses)
        )
    except Exception:
        logging.getLogger(__name__).exception("Could not attach alert handler")
