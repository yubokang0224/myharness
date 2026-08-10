"""SQLite metadata index for persisted invocation and session JSON records.

The JSON files remain the source of truth for record details.  This module keeps
only the fields needed by list and aggregate endpoints so those endpoints do
not need to open every historical JSON file on every request.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from ohmo.workspace import get_invocations_dir, get_sessions_dir, get_workspace_root

_INDEX_FILENAME = "record-index.sqlite3"
_BOOTSTRAP_KEY = "json_bootstrap_v1"
_PREVIEW_LENGTH = 1_000
_schema_lock = threading.Lock()
_initialized_indexes: set[Path] = set()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invocations (
    invocation_id TEXT PRIMARY KEY,
    source_path TEXT NOT NULL UNIQUE,
    session_id TEXT,
    agent_name TEXT,
    channel TEXT NOT NULL,
    platform TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    request_content TEXT,
    response_text TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    message_count INTEGER NOT NULL,
    tool_call_count INTEGER NOT NULL,
    trace_id TEXT,
    duration_ms INTEGER,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_invocations_created_at
    ON invocations(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_invocations_agent_created_at
    ON invocations(agent_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_invocations_status_created_at
    ON invocations(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_invocations_session_id
    ON invocations(session_id);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    source_path TEXT NOT NULL UNIQUE,
    session_key TEXT,
    conversation_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    platform TEXT NOT NULL,
    bot_name TEXT,
    agent_name TEXT,
    chat_id TEXT,
    sender_id TEXT,
    sender_name TEXT,
    summary TEXT NOT NULL,
    response_text TEXT,
    message_count INTEGER NOT NULL,
    model TEXT NOT NULL,
    created_at REAL NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_created_at
    ON sessions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_agent_created_at
    ON sessions(agent_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_channel_created_at
    ON sessions(channel, created_at DESC);
"""


def _index_path(workspace: str | Path | None) -> Path:
    root = get_workspace_root(workspace)
    root.mkdir(parents=True, exist_ok=True)
    return root / _INDEX_FILENAME


def _connect(workspace: str | Path | None) -> sqlite3.Connection:
    path = _index_path(workspace)
    connection = sqlite3.connect(path, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA synchronous = NORMAL")
    if path not in _initialized_indexes:
        with _schema_lock:
            if path not in _initialized_indexes:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.executescript(_SCHEMA)
                _initialized_indexes.add(path)
    return connection


def _preview(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:_PREVIEW_LENGTH]


def _usage(data: dict[str, Any]) -> tuple[int, int]:
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)


def _last_assistant_text(data: dict[str, Any]) -> str | None:
    messages = data.get("messages") if isinstance(data.get("messages"), list) else []
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        parts = []
        content = message.get("content") if isinstance(message.get("content"), list) else []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        text = "".join(parts)
        if text:
            return _preview(text)
    return None


def _remote_metadata(session_key: str | None) -> dict[str, str | None]:
    result: dict[str, str | None] = {
        "channel": "web",
        "platform": "web",
        "bot_name": None,
        "agent_name": None,
        "chat_id": None,
        "sender_id": None,
    }
    if not session_key:
        return result
    parts = session_key.split(":")
    result["channel"] = parts[0] if parts else "remote"
    result["platform"] = result["channel"]
    if result["channel"] == "dingtalk" and len(parts) >= 5:
        result.update(
            {
                "bot_name": parts[1],
                "agent_name": None if parts[2] == "default" else parts[2],
                "chat_id": parts[3],
                "sender_id": parts[4],
            }
        )
    elif len(parts) >= 3:
        result.update({"chat_id": parts[1], "sender_id": parts[-1]})
    return result


def _conversation_id(session_key: str | None, session_id: str) -> str:
    if not session_key:
        return session_id
    return hashlib.sha1(session_key.encode("utf-8")).hexdigest()[:12]


def _upsert_invocation(
    connection: sqlite3.Connection,
    path: Path,
    data: dict[str, Any],
) -> None:
    invocation_id = str(data.get("invocation_id") or path.stem.removeprefix("invocation-"))
    created_at = float(data.get("created_at", path.stat().st_mtime) or 0.0)
    tool_calls = data.get("tool_calls") if isinstance(data.get("tool_calls"), list) else []
    messages = data.get("messages") if isinstance(data.get("messages"), list) else []
    input_tokens, output_tokens = _usage(data)
    duration = data.get("duration_ms")
    connection.execute(
        """
        INSERT INTO invocations (
            invocation_id, source_path, session_id, agent_name, channel, platform,
            model, status, request_content, response_text, error, created_at,
            message_count, tool_call_count, trace_id, duration_ms,
            input_tokens, output_tokens
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(invocation_id) DO UPDATE SET
            source_path = excluded.source_path,
            session_id = excluded.session_id,
            agent_name = excluded.agent_name,
            channel = excluded.channel,
            platform = excluded.platform,
            model = excluded.model,
            status = excluded.status,
            request_content = excluded.request_content,
            response_text = excluded.response_text,
            error = excluded.error,
            created_at = excluded.created_at,
            message_count = excluded.message_count,
            tool_call_count = excluded.tool_call_count,
            trace_id = excluded.trace_id,
            duration_ms = excluded.duration_ms,
            input_tokens = excluded.input_tokens,
            output_tokens = excluded.output_tokens
        """,
        (
            invocation_id,
            str(path),
            data.get("session_id"),
            data.get("agent_name"),
            data.get("channel") or "api",
            data.get("platform") or data.get("channel") or "api",
            data.get("model") or "",
            data.get("status") or "completed",
            _preview(data.get("request_content")),
            _preview(data.get("response_text")),
            _preview(data.get("error")),
            created_at,
            int(data.get("message_count", len(messages)) or 0),
            len(tool_calls),
            data.get("trace_id"),
            int(duration) if isinstance(duration, (int, float)) and duration >= 0 else None,
            input_tokens,
            output_tokens,
        ),
    )


def _upsert_session(
    connection: sqlite3.Connection,
    path: Path,
    data: dict[str, Any],
) -> None:
    session_id = str(data.get("session_id") or path.stem.removeprefix("session-"))
    session_key = data.get("session_key")
    remote = _remote_metadata(session_key)
    channel = data.get("channel") or remote.get("channel") or "web"
    platform = data.get("platform") or remote.get("platform") or channel
    messages = data.get("messages") if isinstance(data.get("messages"), list) else []
    input_tokens, output_tokens = _usage(data)
    connection.execute(
        """
        INSERT INTO sessions (
            session_id, source_path, session_key, conversation_id, channel,
            platform, bot_name, agent_name, chat_id, sender_id, sender_name,
            summary, response_text, message_count, model, created_at,
            input_tokens, output_tokens
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            source_path = excluded.source_path,
            session_key = excluded.session_key,
            conversation_id = excluded.conversation_id,
            channel = excluded.channel,
            platform = excluded.platform,
            bot_name = excluded.bot_name,
            agent_name = excluded.agent_name,
            chat_id = excluded.chat_id,
            sender_id = excluded.sender_id,
            sender_name = excluded.sender_name,
            summary = excluded.summary,
            response_text = excluded.response_text,
            message_count = excluded.message_count,
            model = excluded.model,
            created_at = excluded.created_at,
            input_tokens = excluded.input_tokens,
            output_tokens = excluded.output_tokens
        """,
        (
            session_id,
            str(path),
            session_key,
            data.get("conversation_id") or _conversation_id(session_key, session_id),
            channel,
            platform,
            data.get("bot_name") if data.get("bot_name") is not None else remote.get("bot_name"),
            data.get("agent_name") or remote.get("agent_name"),
            data.get("chat_id") if data.get("chat_id") is not None else remote.get("chat_id"),
            data.get("sender_id") if data.get("sender_id") is not None else remote.get("sender_id"),
            data.get("sender_name"),
            str(data.get("summary") or ""),
            _last_assistant_text(data),
            int(data.get("message_count", len(messages)) or 0),
            str(data.get("model") or ""),
            float(data.get("created_at", path.stat().st_mtime) or 0.0),
            input_tokens,
            output_tokens,
        ),
    )


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def ensure_index(workspace: str | Path | None) -> None:
    """Create the index and backfill historical JSON files once."""
    with _connect(workspace) as connection:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key = ?", (_BOOTSTRAP_KEY,)
        ).fetchone()
        if row is not None:
            return

        # Serialize the one-time backfill across gateway processes.  New JSON
        # writes can still complete; their small index writes wait for commit.
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT value FROM metadata WHERE key = ?", (_BOOTSTRAP_KEY,)
        ).fetchone()
        if row is not None:
            connection.commit()
            return

        for path in get_invocations_dir(workspace).glob("invocation-*.json"):
            data = _read_json(path)
            if data is not None:
                _upsert_invocation(connection, path, data)
        for path in get_sessions_dir(workspace).glob("session-*.json"):
            data = _read_json(path)
            if data is not None:
                _upsert_session(connection, path, data)

        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, 'complete')",
            (_BOOTSTRAP_KEY,),
        )
        connection.commit()


def index_invocation(
    workspace: str | Path | None,
    path: Path,
    data: dict[str, Any],
) -> None:
    with _connect(workspace) as connection:
        _upsert_invocation(connection, path, data)


def index_session(
    workspace: str | Path | None,
    path: Path,
    data: dict[str, Any],
) -> None:
    with _connect(workspace) as connection:
        _upsert_session(connection, path, data)


def delete_session(workspace: str | Path | None, session_id: str) -> None:
    with _connect(workspace) as connection:
        connection.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))


def _agent_filter_sql(agent_name: str | None) -> tuple[str, list[object]]:
    if not agent_name:
        return "", []
    if agent_name == "__default__":
        return " AND (agent_name IS NULL OR agent_name = '')", []
    return " AND agent_name = ?", [agent_name]


def _invocation_filters(
    *,
    agent_name: str | None,
    status: str | None,
    start_at: float | None,
    end_at: float | None,
) -> tuple[str, list[object]]:
    sql = " WHERE 1 = 1"
    params: list[object] = []
    agent_sql, agent_params = _agent_filter_sql(agent_name)
    sql += agent_sql
    params.extend(agent_params)
    if status:
        sql += " AND status = ?"
        params.append(status)
    if start_at is not None:
        sql += " AND created_at >= ?"
        params.append(start_at)
    if end_at is not None:
        sql += " AND created_at <= ?"
        params.append(end_at)
    return sql, params


_COMBINED_INVOCATIONS = """
WITH combined AS (
    SELECT
        invocation_id, session_id, agent_name, channel, platform, model,
        status, request_content, response_text, error, created_at,
        message_count, tool_call_count, trace_id, duration_ms,
        input_tokens, output_tokens
    FROM invocations
    UNION ALL
    SELECT
        'session-' || sessions.session_id AS invocation_id,
        sessions.session_id,
        sessions.agent_name,
        'api' AS channel,
        sessions.platform,
        sessions.model,
        CASE WHEN sessions.message_count > 0 THEN 'completed' ELSE 'created' END AS status,
        sessions.summary AS request_content,
        sessions.response_text,
        NULL AS error,
        sessions.created_at,
        sessions.message_count,
        0 AS tool_call_count,
        NULL AS trace_id,
        NULL AS duration_ms,
        sessions.input_tokens,
        sessions.output_tokens
    FROM sessions
    WHERE sessions.channel = 'api'
      AND NOT EXISTS (
          SELECT 1 FROM invocations WHERE invocations.session_id = sessions.session_id
      )
)
"""


def list_invocations(
    workspace: str | Path | None,
    *,
    limit: int,
    offset: int,
    agent_name: str | None,
    status: str | None,
    start_at: float | None,
    end_at: float | None,
) -> list[dict[str, Any]]:
    ensure_index(workspace)
    where_sql, params = _invocation_filters(
        agent_name=agent_name,
        status=status,
        start_at=start_at,
        end_at=end_at,
    )
    sql = (
        _COMBINED_INVOCATIONS
        + "SELECT * FROM combined"
        + where_sql
        + " ORDER BY created_at DESC, invocation_id DESC LIMIT ? OFFSET ?"
    )
    params.extend([max(1, limit), max(0, offset)])
    with _connect(workspace) as connection:
        return [dict(row) for row in connection.execute(sql, params).fetchall()]


def count_invocations(
    workspace: str | Path | None,
    *,
    agent_name: str | None,
    status: str | None,
    start_at: float | None,
    end_at: float | None,
) -> int:
    ensure_index(workspace)
    where_sql, params = _invocation_filters(
        agent_name=agent_name,
        status=status,
        start_at=start_at,
        end_at=end_at,
    )
    sql = _COMBINED_INVOCATIONS + "SELECT COUNT(*) FROM combined" + where_sql
    with _connect(workspace) as connection:
        row = connection.execute(sql, params).fetchone()
    return int(row[0]) if row is not None else 0


def list_sessions(
    workspace: str | Path | None,
    *,
    limit: int,
    include_remote: bool | None = None,
    channel: str | None = None,
    exclude_channel: str | None = None,
    agent_name: str | None = None,
) -> list[dict[str, Any]]:
    ensure_index(workspace)
    sql = "SELECT * FROM sessions WHERE 1 = 1"
    params: list[object] = []
    if include_remote is False:
        sql += " AND (session_key IS NULL OR session_key = '')"
    if channel:
        sql += " AND channel = ?"
        params.append(channel)
    if exclude_channel:
        sql += " AND channel != ?"
        params.append(exclude_channel)
    agent_sql, agent_params = _agent_filter_sql(agent_name)
    sql += agent_sql
    params.extend(agent_params)
    sql += " ORDER BY created_at DESC, session_id DESC LIMIT ?"
    params.append(max(1, limit))
    with _connect(workspace) as connection:
        rows = connection.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def invocation_daily(
    workspace: str | Path | None,
    *,
    start_at: float,
    agent_name: str | None,
) -> list[dict[str, Any]]:
    ensure_index(workspace)
    where_sql, params = _invocation_filters(
        agent_name=agent_name,
        status=None,
        start_at=start_at,
        end_at=None,
    )
    sql = (
        _COMBINED_INVOCATIONS
        + """
        SELECT
            strftime('%Y-%m-%d', created_at, 'unixepoch', 'localtime') AS date,
            COUNT(*) AS invocations,
            SUM(CASE WHEN status IN ('error', 'failed') OR COALESCE(error, '') != ''
                     THEN 0 ELSE 1 END) AS completed,
            SUM(CASE WHEN status IN ('error', 'failed') OR COALESCE(error, '') != ''
                     THEN 1 ELSE 0 END) AS errors,
            SUM(tool_call_count) AS tool_calls,
            SUM(input_tokens) AS input_tokens,
            SUM(output_tokens) AS output_tokens,
            CAST(AVG(CASE WHEN duration_ms >= 0 THEN duration_ms END) AS INTEGER)
                AS avg_duration_ms
        FROM combined
        """
        + where_sql
        + " GROUP BY date ORDER BY date DESC"
    )
    with _connect(workspace) as connection:
        return [dict(row) for row in connection.execute(sql, params).fetchall()]


def session_daily(
    workspace: str | Path | None,
    *,
    start_at: float,
    agent_name: str | None,
) -> list[dict[str, Any]]:
    ensure_index(workspace)
    sql = """
        SELECT
            strftime('%Y-%m-%d', created_at, 'unixepoch', 'localtime') AS date,
            COUNT(*) AS sessions,
            SUM(message_count) AS messages
        FROM sessions
        WHERE created_at >= ?
    """
    params: list[object] = [start_at]
    agent_sql, agent_params = _agent_filter_sql(agent_name)
    sql += agent_sql
    params.extend(agent_params)
    sql += " GROUP BY date ORDER BY date DESC"
    with _connect(workspace) as connection:
        return [dict(row) for row in connection.execute(sql, params).fetchall()]
