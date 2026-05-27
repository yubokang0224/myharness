"""Chat session router — SSE streaming conversation."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, AsyncIterator
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from openharness.api.usage import UsageSnapshot
from ohmo.gateway.dependencies import AuthContext, get_auth_context, get_current_user, get_runtime, _RuntimeState
from ohmo.gateway.schemas.chat import (
    ApproveRequest,
    CreateSessionRequest,
    MemoryInfo,
    MessageInfo,
    MessageRequest,
    SessionInfo,
    SSEDone,
    SSEError,
    SSEPermissionRequest,
    SSEStatus,
    SSETextDelta,
    SSEToolCall,
    SSEToolResult,
)
from ohmo.session_storage import (
    get_session_dir,
    list_snapshots,
    load_by_id,
    save_session_snapshot,
)

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


# ---------------------------------------------------------------------------
# Session CRUD
# ---------------------------------------------------------------------------

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
        snapshots = list_snapshots(workspace=runtime.workspace)
    except Exception:
        snapshots = []
    result = []
    for snap in snapshots:
        if snap.get("session_key") and not include_remote:
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
    _user: Annotated[dict, Depends(get_current_user)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
):
    """Create a new chat session."""
    session_id = uuid4().hex[:12]
    now = time.time()
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
            channel="web",
            platform="web",
        )
    except Exception:
        logger.exception("Failed to create initial session snapshot for %s", session_id)
    return SessionInfo(
        id=session_id,
        title=body.title or "New Conversation",
        agent_name=body.agent_name,
        conversation_id=session_id,
        session_key=None,
        channel="web",
        platform="web",
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
                        {"tool_name": block.get("name", ""), "tool_input": block.get("input", {})}
                    )
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
            user_text = body.content
            if attachment_notes:
                user_text = f"{body.content.strip() or '[Attachment message]'}\n\n{attachment_notes}"

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

            async for event in engine.submit_message(user_message):
                if abort_event.is_set():
                    await sse_queue.put(_sse_line(SSEError(message="Generation aborted", recoverable=True)))
                    break

                if isinstance(event, AssistantTextDelta):
                    await sse_queue.put(_sse_line(SSETextDelta(text=event.text)))
                elif isinstance(event, ToolExecutionStarted):
                    await sse_queue.put(_sse_line(SSEToolCall(tool_name=event.tool_name, tool_input=event.tool_input)))
                elif isinstance(event, ToolExecutionCompleted):
                    await sse_queue.put(
                        _sse_line(
                            SSEToolResult(
                                tool_name=event.tool_name,
                                output=event.output,
                                is_error=event.is_error,
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
                        from ohmo.session_storage import save_session_snapshot
                        from openharness.api.usage import UsageSnapshot
                        usage_snap = engine.total_usage if hasattr(engine, "total_usage") else UsageSnapshot()
                        if not isinstance(usage_snap, UsageSnapshot):
                            usage_snap = UsageSnapshot()
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
                    except Exception:
                        logger.exception("Failed to save session snapshot for %s", session_id)
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
