"""Chat session router — SSE streaming conversation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, AsyncIterator
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse

from openharness.api.usage import UsageSnapshot
from openharness.config.paths import get_data_dir
from openharness.tools.artifacts import preview_kind_for_path
from ohmo.gateway.dependencies import (
    AuthContext,
    get_auth_context,
    get_current_user,
    get_optional_auth_context,
    get_optional_user,
    get_runtime,
    _RuntimeState,
)
from ohmo.gateway.schemas.chat import (
    ApproveRequest,
    ArtifactInfo,
    CreateSessionRequest,
    MemoryInfo,
    MessageInfo,
    MessageRequest,
    MessageSyncResponse,
    PermissionRequestInfo,
    SessionInfo,
    SSEDone,
    SSEError,
    SSEPermissionRequest,
    SSEStatus,
    SSETextDelta,
    SSEToolCall,
    SSEToolResult,
)
from ohmo.gateway.tool_policy import apply_agent_tool_policy
from ohmo.session_storage import (
    get_session_dir,
    list_snapshots,
    load_by_id,
    save_invocation_record,
    save_session_snapshot,
)
from ohmo.workspace import get_attachments_dir

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sessions", tags=["chat"])

_AUTH_CONFIG_ERROR_MESSAGE = (
    "Authentication is not configured for the current model provider. "
    "Run `oh auth login` or set ANTHROPIC_API_KEY / OPENAI_API_KEY."
)

# ---------------------------------------------------------------------------
# In-process abort registry
# ---------------------------------------------------------------------------
_abort_events: dict[str, asyncio.Event] = {}

# ---------------------------------------------------------------------------
# In-process permission approval registry (keyed by request_id)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PermissionApproval:
    approved: bool
    scope: str = "once"


_pending_approvals: dict[str, "asyncio.Future[PermissionApproval]"] = {}
_session_allowed_tools: dict[str, set[str]] = {}

_EMPTY_ASSISTANT_MESSAGE = (
    "Model returned an empty assistant message. "
    "The turn was ignored to keep the session healthy."
)
_EMPTY_ASSISTANT_SYNC_ERROR = (
    "Model returned no visible content. Check server logs for empty-response diagnostics."
)

_ARTIFACT_PREVIEW_KINDS = {"code", "text", "markdown", "html", "pdf", "word", "image", "binary"}
_LEGACY_PATH_TOOLS = {"write_file", "file_write", "edit_file", "file_edit", "todo_write", "notebook_edit"}
_LEGACY_OUTPUT_PREFIXES = (
    "Updated notebook cell ",
    "Wrote ",
    "Updated ",
    "Created ",
    "Saved ",
    "Generated ",
)
_LEGACY_PATH_LABEL_RE = re.compile(r"^\s*(?:路径|path|file|output)\s*[:：]\s*(?P<path>.+?)\s*$", re.IGNORECASE)


def _safe_attachment_filename(filename: str | None) -> str:
    name = Path(filename or "attachment.bin").name.strip() or "attachment.bin"
    return re.sub(r"[^A-Za-z0-9._ -]+", "_", name)


def _sse_line(payload: Any) -> str:
    data = payload.model_dump_json() if hasattr(payload, "model_dump_json") else json.dumps(payload)
    return f"data: {data}\n\n"


def _attachment_label(attachment: dict[str, Any], index: int) -> str:
    name = attachment.get("name") or attachment.get("fileName")
    return str(name or f"attachment-{index + 1}")


def _attachment_path(attachment: dict[str, Any], cwd: Path) -> Path | None:
    raw_path = attachment.get("path") or attachment.get("filePath")
    if not raw_path:
        return None
    try:
        candidate = Path(str(raw_path)).expanduser()
        if not candidate.is_absolute():
            candidate = cwd / candidate
        candidate = candidate.resolve()
    except Exception:
        return None
    return candidate if candidate.exists() else None


def _is_image_path(path: Path) -> bool:
    return path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def _build_attachment_notes(attachments: list[dict[str, Any]], cwd: Path) -> str:
    if not attachments:
        return ""

    lines = [
        "[Attachments]",
        "The user attached the following files. Use local paths when present.",
    ]
    for index, attachment in enumerate(attachments):
        label = _attachment_label(attachment, index)
        local_path = _attachment_path(attachment, cwd)
        url = attachment.get("url") or attachment.get("serverUrl")
        file_id = attachment.get("fileId") or attachment.get("id")
        file_size = attachment.get("fileSize") or attachment.get("size")
        file_type = attachment.get("fileType") or attachment.get("type")
        parts = [f"name={label}"]
        if local_path:
            parts.append(f"path={local_path}")
            try:
                parts.append(f"bytes={local_path.stat().st_size}")
            except OSError:
                pass
        elif attachment.get("path") or attachment.get("filePath"):
            parts.append(f"path={attachment.get('path') or attachment.get('filePath')}")
        if url:
            parts.append(f"url={url}")
        if file_id:
            parts.append(f"file_id={file_id}")
        if file_type:
            parts.append(f"type={file_type}")
        if file_size and not local_path:
            parts.append(f"bytes={file_size}")
        lines.append(f"{index + 1}. " + "; ".join(parts))
    return "\n".join(lines)


def _prepare_user_text(body: MessageRequest, attachment_notes: str = "") -> str:
    """Build model input with attachment notes."""
    user_text = body.content
    if attachment_notes:
        user_text = f"{body.content.strip() or '[Attachment message]'}\n\n{attachment_notes}"
    return user_text


def _record_id_from_path(path: Path) -> str:
    stem = path.stem
    return stem.removeprefix("invocation-")


def _resolve_persist_mode(body: MessageRequest, default: str) -> str:
    return body.persist_mode or default


def _default_persist_mode_for_session(workspace: str | None, session_id: str) -> str:
    try:
        snap = load_by_id(workspace=workspace, session_id=session_id)
    except Exception:
        snap = None
    if snap and (snap.get("channel") or "web") == "api":
        return "log"
    return "session"


def _artifact_id(path: Path) -> str:
    return hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()[:16]


def _safe_artifact_roots(snap: dict[str, Any], runtime: _RuntimeState) -> list[Path]:
    roots: list[Path] = []
    for raw in (snap.get("cwd"), runtime.workspace, get_data_dir() / "tool_artifacts"):
        if not raw:
            continue
        try:
            root = Path(raw).expanduser().resolve()
        except Exception:
            continue
        if root not in roots:
            roots.append(root)
    return roots


def _is_under_any_root(path: Path, roots: list[Path]) -> bool:
    try:
        resolved = path.expanduser().resolve()
    except Exception:
        return False
    for root in roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _artifact_from_metadata(
    raw_artifact: dict[str, Any],
    *,
    tool_name: str | None,
    tool_use_id: str | None,
    roots: list[Path],
) -> ArtifactInfo | None:
    raw_path = raw_artifact.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    try:
        path = Path(raw_path).expanduser().resolve()
        if not path.is_file() or not _is_under_any_root(path, roots):
            return None
        stat = path.stat()
    except OSError:
        return None

    mime_type = raw_artifact.get("mime_type")
    if not isinstance(mime_type, str) or not mime_type:
        mime_type, _ = mimetypes.guess_type(str(path))

    relative_path = ""
    for root in roots:
        try:
            relative_path = str(path.relative_to(root))
            break
        except ValueError:
            continue

    preview_kind = raw_artifact.get("preview_kind")
    if preview_kind not in _ARTIFACT_PREVIEW_KINDS:
        preview_kind = preview_kind_for_path(path)

    return ArtifactInfo(
        id=_artifact_id(path),
        name=str(raw_artifact.get("name") or path.name),
        path=str(path),
        relative_path=str(raw_artifact.get("relative_path") or relative_path),
        extension=str(raw_artifact.get("extension") or path.suffix.lower()),
        mime_type=mime_type or "application/octet-stream",
        size=int(raw_artifact.get("size") or stat.st_size),
        updated_at=float(raw_artifact.get("updated_at") or stat.st_mtime),
        tool_name=tool_name,
        tool_use_id=tool_use_id,
        preview_kind=preview_kind,
    )


def _legacy_path_from_output_line(line: str) -> str | None:
    stripped = line.strip()
    label_match = _LEGACY_PATH_LABEL_RE.match(stripped)
    if label_match:
        return label_match.group("path").strip().strip("'\"`")
    for prefix in _LEGACY_OUTPUT_PREFIXES:
        if not stripped.startswith(prefix):
            continue
        raw = stripped[len(prefix):]
        if prefix == "Updated notebook cell ":
            _, _, raw = stripped.partition(" in ")
        return raw.strip().strip("'\"`")
    return None


def _legacy_artifacts_from_tool(
    *,
    tool_name: str | None,
    tool_input: dict[str, Any],
    output: str,
    cwd: Path,
    roots: list[Path],
) -> list[dict[str, Any]]:
    raw_paths: list[str] = []

    if tool_name in _LEGACY_PATH_TOOLS:
        raw_path = tool_input.get("path")
        if isinstance(raw_path, str) and raw_path.strip():
            raw_paths.append(raw_path)
        elif tool_name == "todo_write":
            raw_paths.append("TODO.md")

    for line in output.splitlines():
        raw_path = _legacy_path_from_output_line(line)
        if raw_path:
            raw_paths.append(raw_path)

    artifacts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_path in raw_paths:
        try:
            path = Path(raw_path).expanduser()
            if not path.is_absolute():
                path = cwd / path
            path = path.resolve()
        except Exception:
            continue
        if str(path) in seen or not path.is_file() or not _is_under_any_root(path, roots):
            continue
        seen.add(str(path))
        artifacts.append({"path": str(path)})
    return artifacts


def _collect_artifacts_from_snapshot(snap: dict[str, Any], runtime: _RuntimeState) -> list[ArtifactInfo]:
    roots = _safe_artifact_roots(snap, runtime)
    try:
        cwd = Path(snap.get("cwd") or runtime.workspace or Path.cwd()).expanduser().resolve()
    except Exception:
        cwd = Path.cwd()
    tool_uses: dict[str, dict[str, Any]] = {}
    artifacts_by_path: dict[str, ArtifactInfo] = {}

    for msg in snap.get("messages", []):
        if not isinstance(msg, dict):
            continue
        for block in msg.get("content", []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                tool_use_id = str(block.get("id") or "")
                if tool_use_id:
                    tool_uses[tool_use_id] = {
                        "tool_name": block.get("name"),
                        "tool_input": block.get("input") if isinstance(block.get("input"), dict) else {},
                    }
            elif block.get("type") == "tool_result" and not block.get("is_error"):
                metadata = block.get("metadata") if isinstance(block.get("metadata"), dict) else {}
                raw_artifacts = metadata.get("artifacts") if isinstance(metadata.get("artifacts"), list) else []
                tool_use_id = str(block.get("tool_use_id") or "")
                tool_info = tool_uses.get(tool_use_id, {})
                tool_name = tool_info.get("tool_name")
                if not raw_artifacts:
                    raw_artifacts = _legacy_artifacts_from_tool(
                        tool_name=str(tool_name) if tool_name else None,
                        tool_input=tool_info.get("tool_input") if isinstance(tool_info.get("tool_input"), dict) else {},
                        output=str(block.get("content") or ""),
                        cwd=cwd,
                        roots=roots,
                    )
                for raw_artifact in raw_artifacts:
                    if not isinstance(raw_artifact, dict):
                        continue
                    artifact = _artifact_from_metadata(
                        raw_artifact,
                        tool_name=str(tool_name) if tool_name else None,
                        tool_use_id=tool_use_id or None,
                        roots=roots,
                    )
                    if artifact is None:
                        continue
                    existing = artifacts_by_path.get(artifact.path)
                    if existing is None or artifact.updated_at >= existing.updated_at:
                        artifacts_by_path[artifact.path] = artifact

    return sorted(artifacts_by_path.values(), key=lambda item: item.updated_at, reverse=True)


# ---------------------------------------------------------------------------
# Session CRUD
# ---------------------------------------------------------------------------

_SESSION_LIST_SCAN_LIMIT = 500


@router.get("", response_model=list[SessionInfo])
async def list_sessions(
    _user: Annotated[dict, Depends(get_current_user)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
    include_remote: bool = Query(False, description="Include sessions created by remote channels."),
    channel: str | None = Query(None, description="Filter sessions by channel, e.g. web or dingtalk."),
    agent_name: str | None = Query(None, description="Filter sessions by agent name."),
):
    """List all persisted sessions."""
    try:
        snapshots = list_snapshots(workspace=runtime.workspace, limit=_SESSION_LIST_SCAN_LIMIT)
    except Exception:
        snapshots = []
    result = []
    for snap in snapshots:
        if snap.get("session_key") and not include_remote:
            continue
        if (snap.get("channel") or "web") == "api" and channel != "api":
            continue
        if channel and snap.get("channel") != channel:
            continue
        if agent_name and (snap.get("agent_name") or "") != agent_name:
            continue
        result.append(
            SessionInfo(
                id=snap.get("session_id", ""),
                title=snap.get("summary", snap.get("session_id", "Conversation")),
                agent_name=snap.get("agent_name"),
                conversation_id=snap.get("conversation_id"),
                session_key=snap.get("session_key"),
                channel=snap.get("channel") or "web",
                platform=snap.get("platform") or snap.get("channel") or "web",
                bot_name=snap.get("bot_name"),
                chat_id=snap.get("chat_id"),
                sender_id=snap.get("sender_id"),
                sender_name=snap.get("sender_name"),
                created_at=snap.get("created_at", 0.0),
                updated_at=snap.get("updated_at", snap.get("created_at", 0.0)),
                message_count=snap.get("message_count", 0),
            )
        )
    return result


@router.post("", response_model=SessionInfo, status_code=status.HTTP_201_CREATED)
async def create_session(
    body: CreateSessionRequest,
    _user: Annotated[dict | None, Depends(get_optional_user)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
):
    """Create a new chat session."""
    session_id = uuid4().hex[:12]
    now = time.time()
    persist_mode = body.persist_mode or "log"
    channel = "web" if persist_mode == "session" else "api"
    if persist_mode != "none":
        try:
            save_session_snapshot(
                cwd=Path.cwd(),
                workspace=runtime.workspace,
                model="",
                system_prompt="",
                messages=[],
                usage=UsageSnapshot(),
                session_id=session_id,
                title=body.title or "New Conversation",
                agent_name=body.agent_name,
                channel=channel,
                platform=channel,
            )
        except Exception:
            logger.exception("Failed to create initial session snapshot for %s", session_id)
    return SessionInfo(
        id=session_id,
        title=body.title or "New Conversation",
        agent_name=body.agent_name,
        conversation_id=session_id,
        session_key=None,
        channel=channel,
        platform=channel,
        created_at=now,
        updated_at=now,
    )


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: str,
    _user: Annotated[dict, Depends(get_current_user)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
):
    """Delete a session and its persisted snapshot."""
    session_dir = get_session_dir(runtime.workspace)
    for path in session_dir.glob(f"*{session_id}*"):
        try:
            path.unlink()
        except OSError:
            pass


@router.get("/{session_id}/messages", response_model=list[MessageInfo])
async def get_messages(
    session_id: str,
    _user: Annotated[dict, Depends(get_current_user)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
):
    """Return conversation history for a session."""
    try:
        snap = load_by_id(workspace=runtime.workspace, session_id=session_id)
    except Exception:
        return []
    if snap is None:
        return []

    result: list[MessageInfo] = []
    for msg in snap.get("messages", []):
        role = msg.get("role", "user")
        text = ""
        tool_calls: list[dict] = []
        attachments: list[dict] = msg.get("attachments", []) if isinstance(msg.get("attachments"), list) else []
        has_explicit_attachments = bool(attachments)
        for block in msg.get("content", []):
            if isinstance(block, dict):
                if block.get("type") == "text":
                    text += block.get("text", "")
                elif block.get("type") == "image" and block.get("source_path") and not has_explicit_attachments:
                    attachments.append(
                        {
                            "name": Path(str(block.get("source_path"))).name,
                            "path": block.get("source_path"),
                            "fileType": block.get("media_type"),
                            "imgPreview": True,
                            "showDelIcon": False,
                        }
                    )
                elif block.get("type") == "tool_use":
                    tool_calls.append(
                        {
                            "tool_use_id": block.get("id", ""),
                            "tool_name": block.get("name", ""),
                            "tool_input": block.get("input", {}),
                            "metadata": {},
                        }
                    )
                elif block.get("type") == "tool_result":
                    target_id = block.get("tool_use_id", "")
                    for existing in reversed(result):
                        matched = False
                        for call in existing.tool_calls:
                            if call.get("tool_use_id") == target_id:
                                call["output"] = block.get("content", "")
                                call["is_error"] = bool(block.get("is_error", False))
                                call["metadata"] = block.get("metadata", {}) if isinstance(block.get("metadata"), dict) else {}
                                matched = True
                                break
                        if matched:
                            break
            elif isinstance(block, str):
                text += block
        result.append(
            MessageInfo(
                id=msg.get("id", uuid4().hex[:8]),
                role=role,
                content=text,
                created_at=msg.get("created_at", 0.0),
                tool_calls=tool_calls,
                attachments=attachments,
            )
        )
    return result


@router.get("/{session_id}/memory", response_model=MemoryInfo)
async def get_memory(
    session_id: str,
    _user: Annotated[dict, Depends(get_current_user)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
):
    """Return memory entries and files relevant to a session."""
    try:
        from openharness.memory import list_memory_files, scan_memory_files

        cwd = str(Path.cwd())
        files = list_memory_files(cwd)
        entries = scan_memory_files(cwd)
    except Exception:
        files = []
        entries = []
    return MemoryInfo(
        entries=[{"content": e} if isinstance(e, str) else e for e in entries],
        files=[str(f) for f in files],
    )


@router.get("/{session_id}/artifacts", response_model=list[ArtifactInfo])
async def list_artifacts(
    session_id: str,
    _user: Annotated[dict, Depends(get_current_user)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
):
    """Return files generated by AI tool execution in a session."""
    try:
        snap = load_by_id(workspace=runtime.workspace, session_id=session_id)
    except Exception:
        return []
    if snap is None:
        return []
    return _collect_artifacts_from_snapshot(snap, runtime)


@router.get("/{session_id}/artifacts/{artifact_id}/content")
async def get_artifact_content(
    session_id: str,
    artifact_id: str,
    _user: Annotated[dict, Depends(get_current_user)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
):
    """Return a generated artifact's content after session-scoped validation."""
    try:
        snap = load_by_id(workspace=runtime.workspace, session_id=session_id)
    except Exception:
        snap = None
    if snap is None:
        raise HTTPException(status_code=404, detail="Session not found")

    artifact = next(
        (item for item in _collect_artifacts_from_snapshot(snap, runtime) if item.id == artifact_id),
        None,
    )
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")

    path = Path(artifact.path).expanduser().resolve()
    if artifact.preview_kind in {"code", "text", "markdown", "html"}:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            raise HTTPException(status_code=404, detail="Artifact not found") from None
        media_type = "text/html; charset=utf-8" if artifact.preview_kind == "html" else "text/plain; charset=utf-8"
        return PlainTextResponse(text, media_type=media_type)

    return FileResponse(
        path,
        media_type=artifact.mime_type,
        filename=artifact.name,
    )


@router.post("/attachments")
async def upload_attachment(
    file: Annotated[UploadFile, File(...)],
    _user: Annotated[dict, Depends(get_current_user)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
):
    """Persist a browser-selected attachment so local tools can read it."""
    safe_name = _safe_attachment_filename(file.filename)
    target_dir = get_attachments_dir(runtime.workspace) / time.strftime("%Y%m%d")
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = (target_dir / f"{uuid4().hex[:12]}-{safe_name}").resolve()

    try:
        size = 0
        with target_path.open("wb") as stream:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                stream.write(chunk)
    finally:
        await file.close()

    return {
        "uid": uuid4().hex[:12],
        "name": safe_name,
        "path": str(target_path),
        "fileSize": size,
        "fileType": file.content_type,
    }


# ---------------------------------------------------------------------------
# SSE streaming message endpoint
# ---------------------------------------------------------------------------

@router.post("/{session_id}/messages")
async def send_message(
    session_id: str,
    body: MessageRequest,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
):
    """Send a user message and receive an SSE stream of assistant events."""
    abort_event = asyncio.Event()
    _abort_events[session_id] = abort_event

    # SSE event queue: str items are serialised SSE lines, None signals end-of-stream
    sse_queue: asyncio.Queue[str | None] = asyncio.Queue()
    persist_mode = _resolve_persist_mode(body, _default_persist_mode_for_session(runtime.workspace, session_id))
    engine_holder: dict[str, Any] = {}
    settings_holder: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # permission_prompt callback — called by the engine when a tool needs
    # interactive confirmation.  Pushes a permission_request SSE event and
    # suspends until the user responds via the /approve endpoint.
    # ------------------------------------------------------------------
    async def permission_prompt(tool_name: str, reason: str) -> bool:
        req_id = uuid4().hex[:8]
        loop = asyncio.get_running_loop()
        future: asyncio.Future[PermissionApproval] = loop.create_future()
        _pending_approvals[req_id] = future
        await sse_queue.put(
            _sse_line(SSEPermissionRequest(tool_name=tool_name, reason=reason, request_id=req_id))
        )
        try:
            approval = await asyncio.wait_for(asyncio.shield(future), timeout=300.0)
            if isinstance(approval, bool):
                return approval
            if approval.approved and approval.scope == "session":
                _session_allowed_tools.setdefault(session_id, set()).add(tool_name)
                settings_for_session = settings_holder.get("settings")
                engine_for_session = engine_holder.get("engine")
                if settings_for_session is not None and engine_for_session is not None:
                    from openharness.permissions import PermissionChecker

                    settings_for_session.permission.allowed_tools = list(
                        dict.fromkeys([
                            *settings_for_session.permission.allowed_tools,
                            *_session_allowed_tools.get(session_id, set()),
                        ])
                    )
                    engine_for_session.set_permission_checker(PermissionChecker(settings_for_session.permission))
            return approval.approved
        except asyncio.TimeoutError:
            return False
        finally:
            _pending_approvals.pop(req_id, None)

    # ------------------------------------------------------------------
    # Engine coroutine — runs in a background Task so that the SSE
    # generator can keep draining the queue while the engine is suspended
    # waiting for a permission response.
    # ------------------------------------------------------------------
    async def run_engine() -> None:
        try:
            from openharness.config import load_settings
            from openharness.ui.runtime import _resolve_api_client_from_settings
            from openharness.engine import QueryEngine
            from openharness.engine.messages import ConversationMessage, ImageBlock, TextBlock
            from openharness.tools import create_default_tool_registry
            from openharness.mcp.client import McpClientManager
            from openharness.mcp.config import load_mcp_server_configs
            from openharness.permissions import PermissionChecker
            from openharness.utils.internal_api_auth import make_hsjm_auth_metadata
            from openharness.engine.stream_events import (
                AssistantTextDelta,
                AssistantTurnComplete,
                ErrorEvent,
                StatusEvent,
                ToolExecutionCompleted,
                ToolExecutionStarted,
            )

            settings = load_settings()
            agent_def = None
            agent_name: str | None = None
            snap = None
            try:
                snap = load_by_id(workspace=runtime.workspace, session_id=session_id)
                if snap:
                    agent_name = snap.get("agent_name")
            except Exception:
                snap = None
            if agent_name:
                try:
                    from openharness.coordinator.agent_definitions import get_agent_definition

                    agent_def = get_agent_definition(agent_name)
                    if agent_def is not None:
                        settings = settings.merge_cli_overrides(
                            model=agent_def.model if agent_def.model and agent_def.model != "inherit" else None,
                            system_prompt=agent_def.system_prompt,
                            permission_mode=agent_def.permission_mode,
                            max_turns=agent_def.max_turns,
                        )
                except Exception:
                    logger.exception("Failed to apply agent definition for %s", agent_name)
            try:
                api_client = _resolve_api_client_from_settings(settings)
            except SystemExit:
                logger.warning("Model provider authentication is not configured for session %s", session_id)
                await sse_queue.put(
                    _sse_line(SSEError(message=_AUTH_CONFIG_ERROR_MESSAGE, recoverable=False))
                )
                return
            hsjm_auth = make_hsjm_auth_metadata(auth.raw_token)
            tool_metadata = {"hsjm_auth": hsjm_auth} if hsjm_auth else {}
            mcp_manager = McpClientManager(
                load_mcp_server_configs(settings, []),
                auth_metadata=tool_metadata,
            )
            await mcp_manager.connect_all()
            tool_metadata["mcp_manager"] = mcp_manager
            tool_registry = create_default_tool_registry(mcp_manager)
            apply_agent_tool_policy(tool_registry, agent_def)
            if session_id in _session_allowed_tools:
                settings.permission.allowed_tools = list(
                    dict.fromkeys([
                        *settings.permission.allowed_tools,
                        *_session_allowed_tools.get(session_id, set()),
                    ])
                )
            settings_holder["settings"] = settings
            permission_checker = PermissionChecker(settings.permission)

            # Resolve working directory: prefer agent-definition cwd, then session
            # snapshot cwd, then server process cwd.
            effective_cwd = Path.cwd()

            if agent_name:
                try:
                    if agent_def is not None and agent_def.cwd:
                        candidate = Path(agent_def.cwd).expanduser().resolve()
                        if candidate.is_dir():
                            effective_cwd = candidate
                except Exception:
                    logger.exception("Failed to resolve agent cwd for %s", agent_name)

            from openharness.prompts.context import build_runtime_system_prompt
            attachment_notes = _build_attachment_notes(body.attachments, effective_cwd)
            user_text = _prepare_user_text(body, attachment_notes)

            system_prompt = build_runtime_system_prompt(
                settings,
                cwd=effective_cwd,
                latest_user_prompt=user_text,
            )

            engine = QueryEngine(
                api_client=api_client,
                tool_registry=tool_registry,
                permission_checker=permission_checker,
                cwd=effective_cwd,
                model=settings.model,
                system_prompt=system_prompt,
                max_tokens=settings.max_tokens,
                context_window_tokens=settings.context_window_tokens or settings.memory.context_window_tokens,
                auto_compact_threshold_tokens=(
                    settings.auto_compact_threshold_tokens
                    or settings.memory.auto_compact_threshold_tokens
                ),
                permission_prompt=permission_prompt,
                max_turns=getattr(settings, "max_turns", None) or 50,
                tool_metadata=tool_metadata,
            )
            engine_holder["engine"] = engine

            # Load existing messages if session has history
            try:
                if snap and snap.get("messages"):
                    from openharness.engine.messages import ConversationMessage
                    msgs = [ConversationMessage(**m) for m in snap["messages"] if isinstance(m, dict)]
                    engine.load_messages(msgs)
            except Exception:
                pass

            user_blocks = [TextBlock(text=user_text)]
            for attachment in body.attachments:
                local_path = _attachment_path(attachment, effective_cwd)
                if local_path and _is_image_path(local_path):
                    try:
                        user_blocks.append(ImageBlock.from_path(local_path))
                    except Exception:
                        logger.exception("Failed to attach image %s", local_path)

            user_message = ConversationMessage(
                role="user",
                content=user_blocks,
                attachments=body.attachments,
            )

            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            status_messages: list[str] = []
            async for event in engine.submit_message(user_message):
                if abort_event.is_set():
                    await sse_queue.put(_sse_line(SSEError(message="Generation aborted", recoverable=True)))
                    break

                if isinstance(event, AssistantTextDelta):
                    text_parts.append(event.text)
                    await sse_queue.put(_sse_line(SSETextDelta(text=event.text)))
                elif isinstance(event, ToolExecutionStarted):
                    tool_calls.append(
                        {
                            "tool_name": event.tool_name,
                            "tool_input": event.tool_input,
                            "output": None,
                            "is_error": None,
                            "metadata": None,
                        }
                    )
                    await sse_queue.put(_sse_line(SSEToolCall(tool_name=event.tool_name, tool_input=event.tool_input)))
                elif isinstance(event, ToolExecutionCompleted):
                    for call in reversed(tool_calls):
                        if call.get("tool_name") == event.tool_name and call.get("output") is None:
                            call["output"] = event.output
                            call["is_error"] = event.is_error
                            call["metadata"] = event.metadata
                            break
                    await sse_queue.put(
                        _sse_line(
                            SSEToolResult(
                                tool_name=event.tool_name,
                                output=event.output,
                                is_error=event.is_error,
                                metadata=event.metadata,
                            )
                        )
                    )
                elif isinstance(event, AssistantTurnComplete):
                    usage_dict = None
                    try:
                        usage_dict = event.usage.__dict__ if event.usage else None
                    except Exception:
                        pass
                    try:
                        from openharness.api.usage import UsageSnapshot
                        usage_snap = engine.total_usage if hasattr(engine, "total_usage") else UsageSnapshot()
                        if not isinstance(usage_snap, UsageSnapshot):
                            usage_snap = UsageSnapshot()
                        if persist_mode == "session":
                            save_session_snapshot(
                                cwd=Path.cwd(),
                                workspace=runtime.workspace,
                                model=settings.model or "",
                                system_prompt=system_prompt,
                                messages=engine.messages,
                                usage=usage_snap,
                                session_id=session_id,
                                agent_name=agent_name,
                                channel="web",
                                platform="web",
                            )
                        elif persist_mode == "log":
                            save_invocation_record(
                                cwd=Path.cwd(),
                                workspace=runtime.workspace,
                                model=settings.model or "",
                                system_prompt=system_prompt,
                                messages=engine.messages,
                                usage=usage_snap,
                                session_id=session_id,
                                agent_name=agent_name,
                                request_content=body.content,
                                response_text="".join(text_parts).strip(),
                                status="completed",
                                tool_calls=tool_calls,
                                status_messages=status_messages,
                                tool_metadata=tool_metadata,
                            )
                    except Exception:
                        logger.exception("Failed to persist completed turn for %s", session_id)
                    await sse_queue.put(_sse_line(SSEDone(usage=usage_dict)))
                elif isinstance(event, ErrorEvent):
                    if event.message == _EMPTY_ASSISTANT_MESSAGE:
                        await sse_queue.put(
                            _sse_line(SSEStatus(message="模型返回了空回复，本轮已跳过，未写入会话。"))
                        )
                        await sse_queue.put(_sse_line(SSEDone(usage=None)))
                    else:
                        await sse_queue.put(_sse_line(SSEError(message=event.message, recoverable=event.recoverable)))
                elif isinstance(event, StatusEvent):
                    status_messages.append(event.message)
                    await sse_queue.put(_sse_line(SSEStatus(message=event.message)))

        except Exception as exc:
            logger.exception("Error in engine task for session %s", session_id)
            await sse_queue.put(_sse_line(SSEError(message=str(exc), recoverable=False)))
        finally:
            _abort_events.pop(session_id, None)
            await sse_queue.put(None)  # signal end-of-stream

    async def event_stream() -> AsyncIterator[str]:
        engine_task = asyncio.create_task(run_engine())
        try:
            while True:
                item = await sse_queue.get()
                if item is None:
                    break
                yield item
        except asyncio.CancelledError:
            engine_task.cancel()
            raise
        finally:
            if not engine_task.done():
                engine_task.cancel()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{session_id}/messages/sync", response_model=MessageSyncResponse)
async def send_message_sync(
    session_id: str,
    body: MessageRequest,
    auth: Annotated[AuthContext | None, Depends(get_optional_auth_context)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
):
    """Send a user message and return the final assistant text as JSON."""
    permission_requests: list[PermissionRequestInfo] = []
    tool_calls: list[dict[str, Any]] = []
    status_messages: list[str] = []
    text_parts: list[str] = []
    usage_dict: dict[str, Any] | None = None
    invocation_id: str | None = None
    persist_mode = _resolve_persist_mode(body, "log")

    async def permission_prompt(tool_name: str, reason: str) -> bool:
        req_id = uuid4().hex[:8]
        permission_requests.append(
            PermissionRequestInfo(tool_name=tool_name, reason=reason, request_id=req_id)
        )
        return False

    mcp_manager = None
    try:
        from openharness.config import load_settings
        from openharness.ui.runtime import _resolve_api_client_from_settings
        from openharness.engine import QueryEngine
        from openharness.engine.messages import ConversationMessage, ImageBlock, TextBlock
        from openharness.tools import create_default_tool_registry
        from openharness.mcp.client import McpClientManager
        from openharness.mcp.config import load_mcp_server_configs
        from openharness.permissions import PermissionChecker
        from openharness.utils.internal_api_auth import make_hsjm_auth_metadata
        from openharness.engine.stream_events import (
            AssistantTextDelta,
            AssistantTurnComplete,
            ErrorEvent,
            StatusEvent,
            ToolExecutionCompleted,
            ToolExecutionStarted,
        )

        settings = load_settings()
        agent_def = None
        agent_name: str | None = None
        snap = None
        try:
            snap = load_by_id(workspace=runtime.workspace, session_id=session_id)
            if snap:
                agent_name = snap.get("agent_name")
        except Exception:
            snap = None
        if agent_name:
            try:
                from openharness.coordinator.agent_definitions import get_agent_definition

                agent_def = get_agent_definition(agent_name)
                if agent_def is not None:
                    settings = settings.merge_cli_overrides(
                        model=agent_def.model if agent_def.model and agent_def.model != "inherit" else None,
                        system_prompt=agent_def.system_prompt,
                        permission_mode=agent_def.permission_mode,
                        max_turns=agent_def.max_turns,
                    )
            except Exception:
                logger.exception("Failed to apply agent definition for %s", agent_name)

        try:
            api_client = _resolve_api_client_from_settings(settings)
        except SystemExit:
            logger.warning("Model provider authentication is not configured for session %s", session_id)
            return MessageSyncResponse(
                session_id=session_id,
                status="error",
                text="",
                tool_calls=tool_calls,
                status_messages=status_messages,
                permission_requests=permission_requests,
                error=_AUTH_CONFIG_ERROR_MESSAGE,
                recoverable=False,
            )

        hsjm_auth = make_hsjm_auth_metadata(auth.raw_token) if auth is not None else None
        tool_metadata = {"hsjm_auth": hsjm_auth} if hsjm_auth else {}
        mcp_manager = McpClientManager(
            load_mcp_server_configs(settings, []),
            auth_metadata=tool_metadata,
        )
        await mcp_manager.connect_all()
        tool_metadata["mcp_manager"] = mcp_manager
        tool_registry = create_default_tool_registry(mcp_manager)
        apply_agent_tool_policy(tool_registry, agent_def)
        if session_id in _session_allowed_tools:
            settings.permission.allowed_tools = list(
                dict.fromkeys([
                    *settings.permission.allowed_tools,
                    *_session_allowed_tools.get(session_id, set()),
                ])
            )
        permission_checker = PermissionChecker(settings.permission)

        effective_cwd = Path.cwd()
        if agent_name:
            try:
                if agent_def is not None and agent_def.cwd:
                    candidate = Path(agent_def.cwd).expanduser().resolve()
                    if candidate.is_dir():
                        effective_cwd = candidate
            except Exception:
                logger.exception("Failed to resolve agent cwd for %s", agent_name)

        from openharness.prompts.context import build_runtime_system_prompt

        attachment_notes = _build_attachment_notes(body.attachments, effective_cwd)
        user_text = _prepare_user_text(body, attachment_notes)

        system_prompt = build_runtime_system_prompt(
            settings,
            cwd=effective_cwd,
            latest_user_prompt=user_text,
        )

        engine = QueryEngine(
            api_client=api_client,
            tool_registry=tool_registry,
            permission_checker=permission_checker,
            cwd=effective_cwd,
            model=settings.model,
            system_prompt=system_prompt,
            max_tokens=settings.max_tokens,
            context_window_tokens=settings.context_window_tokens or settings.memory.context_window_tokens,
            auto_compact_threshold_tokens=(
                settings.auto_compact_threshold_tokens
                or settings.memory.auto_compact_threshold_tokens
            ),
            permission_prompt=permission_prompt,
            max_turns=getattr(settings, "max_turns", None) or 50,
            tool_metadata=tool_metadata,
        )

        try:
            if snap and snap.get("messages"):
                msgs = [ConversationMessage(**m) for m in snap["messages"] if isinstance(m, dict)]
                engine.load_messages(msgs)
        except Exception:
            pass

        user_blocks = [TextBlock(text=user_text)]
        for attachment in body.attachments:
            local_path = _attachment_path(attachment, effective_cwd)
            if local_path and _is_image_path(local_path):
                try:
                    user_blocks.append(ImageBlock.from_path(local_path))
                except Exception:
                    logger.exception("Failed to attach image %s", local_path)

        user_message = ConversationMessage(
            role="user",
            content=user_blocks,
            attachments=body.attachments,
        )

        async for event in engine.submit_message(user_message):
            if isinstance(event, AssistantTextDelta):
                text_parts.append(event.text)
            elif isinstance(event, ToolExecutionStarted):
                tool_calls.append(
                    {
                        "tool_name": event.tool_name,
                        "tool_input": event.tool_input,
                        "output": None,
                        "is_error": None,
                        "metadata": None,
                    }
                )
            elif isinstance(event, ToolExecutionCompleted):
                for call in reversed(tool_calls):
                    if call.get("tool_name") == event.tool_name and call.get("output") is None:
                        call["output"] = event.output
                        call["is_error"] = event.is_error
                        call["metadata"] = event.metadata
                        break
                else:
                    tool_calls.append(
                        {
                            "tool_name": event.tool_name,
                            "tool_input": {},
                            "output": event.output,
                            "is_error": event.is_error,
                            "metadata": event.metadata,
                        }
                    )
            elif isinstance(event, AssistantTurnComplete):
                try:
                    usage_dict = event.usage.__dict__ if event.usage else None
                except Exception:
                    usage_dict = None
                try:
                    usage_snap = engine.total_usage if hasattr(engine, "total_usage") else UsageSnapshot()
                    if not isinstance(usage_snap, UsageSnapshot):
                        usage_snap = UsageSnapshot()
                    response_text = "".join(text_parts).strip()
                    if persist_mode == "session":
                        save_session_snapshot(
                            cwd=Path.cwd(),
                            workspace=runtime.workspace,
                            model=settings.model or "",
                            system_prompt=system_prompt,
                            messages=engine.messages,
                            usage=usage_snap,
                            session_id=session_id,
                            agent_name=agent_name,
                            channel="web",
                            platform="web",
                        )
                    elif persist_mode == "log":
                        path = save_invocation_record(
                            cwd=Path.cwd(),
                            workspace=runtime.workspace,
                            model=settings.model or "",
                            system_prompt=system_prompt,
                            messages=engine.messages,
                            usage=usage_snap,
                            session_id=session_id,
                            agent_name=agent_name,
                            request_content=body.content,
                            response_text=response_text,
                            status="completed",
                            tool_calls=tool_calls,
                            status_messages=status_messages,
                            permission_requests=[item.model_dump() for item in permission_requests],
                            tool_metadata=tool_metadata,
                        )
                        invocation_id = _record_id_from_path(path)
                except Exception:
                    logger.exception("Failed to persist completed turn for %s", session_id)
            elif isinstance(event, ErrorEvent):
                if event.message == _EMPTY_ASSISTANT_MESSAGE:
                    status_messages.append(
                        "Model returned an empty assistant message; this turn was skipped."
                    )
                    return MessageSyncResponse(
                        session_id=session_id,
                        status="error",
                        text="".join(text_parts).strip(),
                        tool_calls=tool_calls,
                        status_messages=status_messages,
                        permission_requests=permission_requests,
                        usage=usage_dict,
                        error=_EMPTY_ASSISTANT_SYNC_ERROR,
                        recoverable=True,
                    )
                else:
                    return MessageSyncResponse(
                        session_id=session_id,
                        status="error",
                        text="".join(text_parts).strip(),
                        tool_calls=tool_calls,
                        status_messages=status_messages,
                        permission_requests=permission_requests,
                        usage=usage_dict,
                        error=event.message,
                        recoverable=event.recoverable,
                    )
            elif isinstance(event, StatusEvent):
                status_messages.append(event.message)

        return MessageSyncResponse(
            session_id=session_id,
            status="completed",
            text="".join(text_parts).strip(),
            invocation_id=invocation_id,
            tool_calls=tool_calls,
            status_messages=status_messages,
            permission_requests=permission_requests,
            usage=usage_dict,
        )

    except Exception as exc:
        logger.exception("Error in sync engine task for session %s", session_id)
        return MessageSyncResponse(
            session_id=session_id,
            status="error",
            text="".join(text_parts).strip(),
            tool_calls=tool_calls,
            status_messages=status_messages,
            permission_requests=permission_requests,
            usage=usage_dict,
            error=str(exc),
            recoverable=False,
        )
    finally:
        if mcp_manager is not None:
            try:
                await mcp_manager.close()
            except Exception:
                pass


@router.post("/{session_id}/messages/approve", status_code=status.HTTP_204_NO_CONTENT)
async def approve_tool(
    session_id: str,
    body: ApproveRequest,
    _user: Annotated[dict, Depends(get_current_user)],
):
    """Resolve a pending permission confirmation request."""
    future = _pending_approvals.get(body.request_id)
    if future is not None and not future.done():
        future.set_result(PermissionApproval(body.approved, body.scope))


@router.post("/{session_id}/messages/abort", status_code=status.HTTP_204_NO_CONTENT)
async def abort_message(
    session_id: str,
    _user: Annotated[dict, Depends(get_current_user)],
):
    """Signal the active generation for this session to stop."""
    event = _abort_events.get(session_id)
    if event:
        event.set()
