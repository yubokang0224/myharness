"""Session persistence for ``ohmo``."""

from __future__ import annotations

import json
import hashlib
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage, sanitize_conversation_messages
from openharness.services.session_backend import SessionBackend
from openharness.services.session_storage import (
    _persistable_tool_metadata,
    _sanitize_snapshot_payload,
)
from openharness.utils.fs import atomic_write_text

from ohmo.workspace import get_invocations_dir, get_sessions_dir


def get_session_dir(workspace: str | Path | None = None) -> Path:
    """Return the ohmo sessions directory."""
    session_dir = get_sessions_dir(workspace)
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def get_invocation_dir(workspace: str | Path | None = None) -> Path:
    """Return the ohmo invocation-log directory."""
    invocation_dir = get_invocations_dir(workspace)
    invocation_dir.mkdir(parents=True, exist_ok=True)
    return invocation_dir


# Sentinel filter value meaning "records without an agent_name" (the default agent).
DEFAULT_AGENT_SENTINEL = "__default__"


def matches_agent_filter(record_agent_name: object, agent_name: str | None) -> bool:
    """Return True when a record's agent_name passes the requested filter."""
    if not agent_name:
        return True
    if agent_name == DEFAULT_AGENT_SENTINEL:
        return not (record_agent_name or "")
    return (record_agent_name or "") == agent_name


def _session_key_token(session_key: str) -> str:
    return hashlib.sha1(session_key.encode("utf-8")).hexdigest()[:12]


def _conversation_id(session_key: str | None, session_id: str) -> str:
    if session_key:
        return _session_key_token(session_key)
    return session_id


def _remote_metadata_from_session_key(session_key: str | None) -> dict[str, str | None]:
    metadata: dict[str, str | None] = {
        "channel": "web",
        "platform": "web",
        "bot_name": None,
        "chat_id": None,
        "sender_id": None,
        "agent_name": None,
    }
    if not session_key:
        return metadata

    parts = session_key.split(":")
    metadata["channel"] = parts[0] if parts else "remote"
    metadata["platform"] = metadata["channel"]
    if metadata["channel"] == "dingtalk" and len(parts) >= 5:
        metadata.update(
            {
                "bot_name": parts[1],
                "agent_name": None if parts[2] == "default" else parts[2],
                "chat_id": parts[3],
                "sender_id": parts[4],
            }
        )
    elif len(parts) >= 3:
        metadata.update({"chat_id": parts[1], "sender_id": parts[-1]})
    return metadata


def _session_key_latest_path(workspace: str | Path | None, session_key: str) -> Path:
    session_dir = get_session_dir(workspace)
    token = _session_key_token(session_key)
    return session_dir / f"latest-{token}.json"


def save_session_snapshot(
    *,
    cwd: str | Path,
    workspace: str | Path | None = None,
    model: str,
    system_prompt: str,
    messages: list[ConversationMessage],
    usage: UsageSnapshot,
    session_id: str | None = None,
    session_key: str | None = None,
    title: str | None = None,
    agent_name: str | None = None,
    channel: str | None = None,
    platform: str | None = None,
    bot_name: str | None = None,
    chat_id: str | None = None,
    sender_id: str | None = None,
    sender_name: str | None = None,
    tool_metadata: dict[str, object] | None = None,
) -> Path:
    """Persist the latest ohmo session snapshot."""
    session_dir = get_session_dir(workspace)
    sid = session_id or uuid4().hex[:12]
    now = time.time()
    messages = sanitize_conversation_messages(messages)
    summary = ""
    for msg in messages:
        if msg.role == "user" and msg.text.strip():
            summary = msg.text.strip()[:80]
            break
    if not summary and title:
        summary = title.strip()[:80]

    remote_metadata = _remote_metadata_from_session_key(session_key)
    resolved_agent_name = agent_name or remote_metadata.get("agent_name")
    payload = {
        "app": "ohmo",
        "session_id": sid,
        "session_key": session_key,
        "conversation_id": _conversation_id(session_key, sid),
        "channel": channel or remote_metadata.get("channel") or "web",
        "platform": platform or remote_metadata.get("platform") or channel or "web",
        "bot_name": bot_name if bot_name is not None else remote_metadata.get("bot_name"),
        "agent_name": resolved_agent_name,
        "chat_id": chat_id if chat_id is not None else remote_metadata.get("chat_id"),
        "sender_id": sender_id if sender_id is not None else remote_metadata.get("sender_id"),
        "sender_name": sender_name,
        "cwd": str(Path(cwd).resolve()),
        "model": model,
        "system_prompt": system_prompt,
        "messages": [message.model_dump(mode="json") for message in messages],
        "usage": usage.model_dump(),
        "tool_metadata": _persistable_tool_metadata(tool_metadata),
        "created_at": now,
        "summary": summary,
        "message_count": len(messages),
    }
    data = json.dumps(payload, indent=2) + "\n"
    latest_path = session_dir / "latest.json"
    if not session_key:
        atomic_write_text(latest_path, data)
    if session_key:
        atomic_write_text(_session_key_latest_path(workspace, session_key), data)
    session_path = session_dir / f"session-{sid}.json"
    atomic_write_text(session_path, data)
    return latest_path


def save_invocation_record(
    *,
    cwd: str | Path,
    workspace: str | Path | None = None,
    model: str,
    system_prompt: str,
    messages: list[ConversationMessage],
    usage: UsageSnapshot,
    session_id: str | None = None,
    agent_name: str | None = None,
    channel: str = "api",
    platform: str = "api",
    request_content: str | None = None,
    response_text: str | None = None,
    status: str = "completed",
    tool_calls: list[dict[str, Any]] | None = None,
    status_messages: list[str] | None = None,
    permission_requests: list[dict[str, Any]] | None = None,
    error: str | None = None,
    tool_metadata: dict[str, object] | None = None,
    trace_id: str | None = None,
    duration_ms: int | None = None,
) -> Path:
    """Persist a non-conversation API invocation record."""
    invocation_dir = get_invocation_dir(workspace)
    invocation_id = uuid4().hex[:12]
    messages = sanitize_conversation_messages(messages)
    payload = {
        "app": "ohmo",
        "kind": "agent_invocation",
        "invocation_id": invocation_id,
        "session_id": session_id,
        "agent_name": agent_name,
        "channel": channel,
        "platform": platform,
        "cwd": str(Path(cwd).resolve()),
        "model": model,
        "system_prompt": system_prompt,
        "request_content": request_content,
        "response_text": response_text,
        "status": status,
        "trace_id": trace_id,
        "duration_ms": duration_ms,
        "messages": [message.model_dump(mode="json") for message in messages],
        "usage": usage.model_dump(),
        "tool_calls": tool_calls or [],
        "status_messages": status_messages or [],
        "permission_requests": permission_requests or [],
        "error": error,
        "tool_metadata": _persistable_tool_metadata(tool_metadata),
        "created_at": time.time(),
        "message_count": len(messages),
    }
    path = invocation_dir / f"invocation-{invocation_id}.json"
    atomic_write_text(path, json.dumps(payload, indent=2) + "\n")
    return path


def list_invocation_records(
    workspace: str | Path | None = None,
    *,
    limit: int = 50,
    offset: int = 0,
    agent_name: str | None = None,
    status: str | None = None,
    start_at: float | None = None,
    end_at: float | None = None,
) -> list[dict[str, Any]]:
    """List non-conversation invocation records, newest first."""
    offset = max(0, offset)
    invocation_dir = get_invocation_dir(workspace)
    records: list[dict[str, Any]] = []
    seen_session_ids: set[str] = set()
    max_needed = max(0, offset) + max(1, limit)
    for path in sorted(invocation_dir.glob("invocation-*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not matches_agent_filter(data.get("agent_name"), agent_name):
            continue
        if status and (data.get("status") or "") != status:
            continue
        created_at = float(data.get("created_at", path.stat().st_mtime) or 0.0)
        if start_at is not None and created_at < start_at:
            continue
        if end_at is not None and created_at > end_at:
            continue
        session_id = data.get("session_id")
        if session_id:
            seen_session_ids.add(str(session_id))
        records.append(
            {
                "invocation_id": data.get("invocation_id") or path.stem.removeprefix("invocation-"),
                "session_id": session_id,
                "agent_name": data.get("agent_name"),
                "channel": data.get("channel") or "api",
                "platform": data.get("platform") or data.get("channel") or "api",
                "model": data.get("model") or "",
                "status": data.get("status") or "completed",
                "request_content": data.get("request_content"),
                "response_text": data.get("response_text"),
                "error": data.get("error"),
                "created_at": created_at,
                "message_count": data.get("message_count", len(data.get("messages", []))),
                "tool_call_count": len(data.get("tool_calls", [])) if isinstance(data.get("tool_calls"), list) else 0,
                "trace_id": data.get("trace_id"),
                "duration_ms": data.get("duration_ms"),
                "usage": data.get("usage") if isinstance(data.get("usage"), dict) else {},
            }
        )
        if len(records) >= max_needed:
            break
    if len(records) < max_needed:
        session_dir = get_session_dir(workspace)
        for path in sorted(session_dir.glob("session-*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if (data.get("channel") or "web") != "api":
                continue
            session_id = str(data.get("session_id") or path.stem.removeprefix("session-"))
            if session_id in seen_session_ids:
                continue
            if not matches_agent_filter(data.get("agent_name"), agent_name):
                continue
            fallback_status = "completed" if data.get("messages") else "created"
            if status and fallback_status != status:
                continue
            created_at = float(data.get("created_at", path.stat().st_mtime) or 0.0)
            if start_at is not None and created_at < start_at:
                continue
            if end_at is not None and created_at > end_at:
                continue
            messages = data.get("messages") if isinstance(data.get("messages"), list) else []
            response_text = ""
            for msg in reversed(messages):
                if not isinstance(msg, dict) or msg.get("role") != "assistant":
                    continue
                for block in msg.get("content", []):
                    if isinstance(block, dict) and block.get("type") == "text":
                        response_text += str(block.get("text") or "")
                if response_text:
                    break
            records.append(
                {
                    "invocation_id": f"session-{session_id}",
                    "session_id": session_id,
                    "agent_name": data.get("agent_name"),
                    "channel": "api",
                    "platform": data.get("platform") or "api",
                    "model": data.get("model") or "",
                    "status": fallback_status,
                    "request_content": data.get("summary"),
                    "response_text": response_text or None,
                    "error": None,
                    "created_at": created_at,
                    "message_count": data.get("message_count", len(messages)),
                    "tool_call_count": 0,
                }
            )
            if len(records) >= max_needed:
                break
    records.sort(key=lambda item: float(item.get("created_at") or 0.0), reverse=True)
    return records[offset : offset + limit]


def count_invocation_records(
    workspace: str | Path | None = None,
    *,
    agent_name: str | None = None,
    status: str | None = None,
    start_at: float | None = None,
    end_at: float | None = None,
) -> int:
    """Count invocation records after applying filters."""
    return len(
        list_invocation_records(
            workspace=workspace,
            limit=1_000_000,
            offset=0,
            agent_name=agent_name,
            status=status,
            start_at=start_at,
            end_at=end_at,
        )
    )


def load_invocation_record(workspace: str | Path | None, invocation_id: str) -> dict[str, Any] | None:
    """Load one invocation record by id."""
    safe_id = Path(invocation_id).name.removeprefix("invocation-").removesuffix(".json")
    if safe_id.startswith("session-"):
        session_id = safe_id.removeprefix("session-")
        session_path = get_session_dir(workspace) / f"session-{session_id}.json"
        if not session_path.exists():
            return None
        try:
            data = json.loads(session_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if (data.get("channel") or "web") != "api":
            return None
        return {
            **data,
            "kind": "agent_invocation",
            "invocation_id": f"session-{session_id}",
            "request_content": data.get("summary"),
            "response_text": None,
            "status": "completed" if data.get("messages") else "created",
            "tool_calls": [],
            "status_messages": [],
            "permission_requests": [],
        }
    path = get_invocation_dir(workspace) / f"invocation-{safe_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def load_latest(workspace: str | Path | None = None) -> dict[str, Any] | None:
    path = get_session_dir(workspace) / "latest.json"
    if not path.exists():
        return None
    return _sanitize_snapshot_payload(json.loads(path.read_text(encoding="utf-8")))


def load_latest_for_session_key(workspace: str | Path | None, session_key: str) -> dict[str, Any] | None:
    path = _session_key_latest_path(workspace, session_key)
    if path.exists():
        return _sanitize_snapshot_payload(json.loads(path.read_text(encoding="utf-8")))
    return None


def delete_latest_for_session_key(workspace: str | Path | None, session_key: str) -> bool:
    path = _session_key_latest_path(workspace, session_key)
    if not path.exists():
        return False
    path.unlink()
    return True


def list_snapshots(workspace: str | Path | None = None, limit: int = 20) -> list[dict[str, Any]]:
    session_dir = get_session_dir(workspace)
    sessions: list[dict[str, Any]] = []
    for path in sorted(session_dir.glob("session-*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        sessions.append(
            {
                "session_id": data.get("session_id", path.stem.replace("session-", "")),
                "session_key": data.get("session_key"),
                "conversation_id": data.get("conversation_id")
                or _conversation_id(data.get("session_key"), data.get("session_id", path.stem.replace("session-", ""))),
                "channel": data.get("channel") or _remote_metadata_from_session_key(data.get("session_key")).get("channel"),
                "platform": data.get("platform") or _remote_metadata_from_session_key(data.get("session_key")).get("platform"),
                "bot_name": data.get("bot_name"),
                "agent_name": data.get("agent_name"),
                "chat_id": data.get("chat_id"),
                "sender_id": data.get("sender_id"),
                "sender_name": data.get("sender_name"),
                "summary": data.get("summary", ""),
                "message_count": data.get("message_count", len(data.get("messages", []))),
                "model": data.get("model", ""),
                "created_at": data.get("created_at", path.stat().st_mtime),
            }
        )
        if len(sessions) >= limit:
            break
    return sessions


def load_by_id(workspace: str | Path | None, session_id: str) -> dict[str, Any] | None:
    path = get_session_dir(workspace) / f"session-{session_id}.json"
    if path.exists():
        return _sanitize_snapshot_payload(json.loads(path.read_text(encoding="utf-8")))
    latest = load_latest(workspace)
    if latest and (latest.get("session_id") == session_id or session_id == "latest"):
        return latest
    return None


def export_session_markdown(
    *,
    cwd: str | Path,
    workspace: str | Path | None = None,
    messages: list[ConversationMessage],
) -> Path:
    path = get_session_dir(workspace) / "transcript.md"
    parts = ["# ohmo Session Transcript"]
    for message in messages:
        parts.append(f"\n## {message.role.capitalize()}\n")
        text = message.text.strip()
        if text:
            parts.append(text)
    atomic_write_text(path, "\n".join(parts).strip() + "\n")
    return path


class OhmoSessionBackend(SessionBackend):
    """Session backend rooted in ``.ohmo/sessions``."""

    def __init__(self, workspace: str | Path | None = None) -> None:
        self._workspace = workspace

    def get_session_dir(self, cwd: str | Path) -> Path:
        return get_session_dir(self._workspace)

    def save_snapshot(
        self,
        *,
        cwd: str | Path,
        model: str,
        system_prompt: str,
        messages: list[ConversationMessage],
        usage: UsageSnapshot,
        session_id: str | None = None,
        session_key: str | None = None,
        title: str | None = None,
        agent_name: str | None = None,
        channel: str | None = None,
        platform: str | None = None,
        bot_name: str | None = None,
        chat_id: str | None = None,
        sender_id: str | None = None,
        sender_name: str | None = None,
        tool_metadata: dict[str, object] | None = None,
    ) -> Path:
        return save_session_snapshot(
            cwd=cwd,
            workspace=self._workspace,
            model=model,
            system_prompt=system_prompt,
            messages=messages,
            usage=usage,
            session_id=session_id,
            session_key=session_key,
            title=title,
            agent_name=agent_name,
            channel=channel,
            platform=platform,
            bot_name=bot_name,
            chat_id=chat_id,
            sender_id=sender_id,
            sender_name=sender_name,
            tool_metadata=tool_metadata,
        )

    def load_latest(self, cwd: str | Path) -> dict[str, Any] | None:
        return load_latest(self._workspace)

    def list_snapshots(self, cwd: str | Path, limit: int = 20) -> list[dict[str, Any]]:
        return list_snapshots(self._workspace, limit=limit)

    def load_by_id(self, cwd: str | Path, session_id: str) -> dict[str, Any] | None:
        return load_by_id(self._workspace, session_id)

    def load_latest_for_session_key(self, session_key: str) -> dict[str, Any] | None:
        return load_latest_for_session_key(self._workspace, session_key)

    def delete_latest_for_session_key(self, session_key: str) -> bool:
        return delete_latest_for_session_key(self._workspace, session_key)

    def export_markdown(
        self,
        *,
        cwd: str | Path,
        messages: list[ConversationMessage],
    ) -> Path:
        return export_session_markdown(cwd=cwd, workspace=self._workspace, messages=messages)
