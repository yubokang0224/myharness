"""Session-aware runtime pool for ohmo gateway."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import mimetypes
from pathlib import Path
import json
import os
import string
import time

from openharness.api.usage import UsageSnapshot
from openharness.channels.bus.events import InboundMessage
from openharness.commands import CommandContext, CommandResult
from openharness.config.paths import get_data_dir
from openharness.tools.artifacts import IGNORED_ARTIFACT_DIRS
from openharness.coordinator.agent_definitions import AgentDefinition, get_agent_definition
from openharness.engine.messages import (
    ConversationMessage,
    ImageBlock,
    TextBlock,
    sanitize_conversation_messages,
)
from openharness.engine.query import MaxTurnsExceeded
from openharness.engine.stream_events import (
    AssistantTextDelta,
    AssistantTurnComplete,
    CompactProgressEvent,
    ErrorEvent,
    StatusEvent,
    ToolExecutionCompleted,
    ToolExecutionStarted,
)
from openharness.prompts import build_runtime_system_prompt
from openharness.ui.runtime import RuntimeBundle, _last_user_text, build_runtime, close_runtime, start_runtime
from openharness.utils.trace import get_trace_id

from ohmo.gateway.config import load_gateway_config
from ohmo.gateway.tool_policy import apply_agent_tool_policy
from ohmo.memory import create_memory_command_backend
from ohmo.prompts import build_ohmo_system_prompt
from ohmo.session_storage import OhmoSessionBackend, save_invocation_record
from ohmo.workspace import get_plugins_dir, get_skills_dir, initialize_workspace

logger = logging.getLogger(__name__)

_CHANNEL_THINKING_PHRASES = (
    "🤔 想一想…",
    "🧠 琢磨中…",
    "✨ 整理一下思路…",
    "🔎 看看这个…",
    "🪄 捋一捋线索…",
)

_CHANNEL_THINKING_PHRASES_EN = (
    "🤔 Thinking…",
    "🧠 Working through it…",
    "✨ Pulling the pieces together…",
    "🔎 Looking into it…",
    "🪄 Following the thread…",
)

_TEXT_PREVIEW_BYTES = 4096
_TEXT_PREVIEW_CHARS = 900
_BINARY_HEAD_BYTES = 32
# Cap on files attached to a single outbound channel reply; extras are listed by name.
_MAX_OUTBOUND_ARTIFACTS = 5
_IMAGE_FALLBACK_NOTE_PREFIX = (
    "[Image attachment omitted because the active model does not support image input"
)
_IMAGE_FALLBACK_NOTE_GUIDANCE = (
    " Ask the user to resend the image as text or switch to a vision-capable model."
)


@dataclass(frozen=True)
class GatewayStreamUpdate:
    """One outbound update produced while processing a channel message."""

    kind: str
    text: str
    metadata: dict[str, object]


class OhmoSessionRuntimePool:
    """Maintain one runtime bundle per chat/thread session."""

    def __init__(
        self,
        *,
        cwd: str | Path,
        workspace: str | Path | None = None,
        provider_profile: str,
        model: str | None = None,
        max_turns: int | None = None,
    ) -> None:
        self._cwd = str(Path(cwd).resolve())
        self._workspace = workspace
        self._provider_profile = provider_profile
        self._model = model
        self._max_turns = max_turns
        self._workspace = initialize_workspace(workspace)
        self._gateway_config = load_gateway_config(self._workspace)
        self._session_backend = OhmoSessionBackend(self._workspace)
        self._bundles: dict[str, RuntimeBundle] = {}
        self._session_agents: dict[str, str] = {}
        self._session_metadata: dict[str, dict[str, str | None]] = {}
        # Last recorded cumulative usage per session; engines are reused across
        # turns so per-turn usage is the delta against this baseline.
        self._session_usage_baseline: dict[str, tuple[int, int]] = {}
        self._artifact_roots = _compute_artifact_roots(self._cwd, self._workspace)

    @property
    def active_sessions(self) -> int:
        return len(self._bundles)

    def _remote_admin_allowed(self, command) -> bool:
        if not getattr(command, "remote_admin_opt_in", False):
            return False
        if not self._gateway_config.allow_remote_admin_commands:
            return False
        allowed = {
            str(name).strip().lower()
            for name in self._gateway_config.allowed_remote_admin_commands
            if str(name).strip()
        }
        return command.name.lower() in allowed

    async def get_bundle(
        self,
        session_key: str,
        latest_user_prompt: str | None = None,
        *,
        agent_name: str | None = None,
    ) -> RuntimeBundle:
        """Return an existing bundle or create a new one."""
        agent_def = self._resolve_agent_definition(agent_name)
        if agent_def is not None:
            self._session_agents[session_key] = agent_def.name
        bundle = self._bundles.get(session_key)
        if bundle is not None:
            self._apply_agent_tool_policy(bundle, agent_def)
            logger.info(
                "ohmo runtime reusing session session_key=%s session_id=%s prompt=%r",
                session_key,
                bundle.session_id,
                _content_snippet(latest_user_prompt or ""),
            )
            bundle.engine.set_system_prompt(self._runtime_system_prompt(bundle, latest_user_prompt))
            return bundle

        snapshot = self._session_backend.load_latest_for_session_key(session_key)
        logger.info(
            "ohmo runtime creating session session_key=%s restored=%s prompt=%r",
            session_key,
            bool(snapshot),
            _content_snippet(latest_user_prompt or ""),
        )
        bundle = await build_runtime(
            model=self._model_for_agent(agent_def),
            max_turns=agent_def.max_turns if agent_def and agent_def.max_turns else self._max_turns,
            system_prompt=self._base_system_prompt(agent_def),
            active_profile=self._provider_profile,
            permission_mode=agent_def.permission_mode if agent_def else None,
            session_backend=self._session_backend,
            enforce_max_turns=self._max_turns is not None or bool(agent_def and agent_def.max_turns),
            restore_messages=snapshot.get("messages") if snapshot else None,
            restore_tool_metadata=snapshot.get("tool_metadata") if snapshot else None,
            extra_skill_dirs=(str(get_skills_dir(self._workspace)),),
            extra_plugin_roots=(str(get_plugins_dir(self._workspace)),),
            memory_backend=create_memory_command_backend(self._workspace),
            include_project_memory=False,
        )
        self._apply_agent_tool_policy(bundle, agent_def)
        if snapshot and snapshot.get("session_id"):
            bundle.session_id = str(snapshot["session_id"])
        await start_runtime(bundle)
        bundle.engine.set_system_prompt(self._runtime_system_prompt(bundle, latest_user_prompt))
        logger.info(
            "ohmo runtime started session_key=%s session_id=%s restored_messages=%s",
            session_key,
            bundle.session_id,
            len(snapshot.get("messages") or []) if snapshot else 0,
        )
        self._bundles[session_key] = bundle
        return bundle

    async def stream_message(self, message: InboundMessage, session_key: str):
        """Submit an inbound channel message and yield progress + final reply updates."""
        self._session_metadata[session_key] = self._session_metadata_for_message(message)
        user_message = _build_inbound_user_message(message, session_key=session_key)
        user_prompt = user_message.text
        bundle = await self.get_bundle(
            session_key,
            latest_user_prompt=user_prompt,
            agent_name=self._agent_name_for_message(message),
        )
        _apply_channel_context_metadata(bundle, message, session_key)
        logger.info(
            "ohmo runtime processing start channel=%s chat_id=%s session_key=%s session_id=%s content=%r",
            message.channel,
            message.chat_id,
            session_key,
            bundle.session_id,
            _content_snippet(user_prompt),
        )

        parsed = bundle.commands.lookup(user_prompt)
        if parsed is not None and not message.media:
            command, args = parsed
            remote_allowed = getattr(command, "remote_invocable", True)
            if not remote_allowed and self._remote_admin_allowed(command):
                remote_allowed = True
                logger.warning(
                    "ohmo gateway remote administrative command accepted channel=%s chat_id=%s sender_id=%s command=%s",
                    message.channel,
                    message.chat_id,
                    message.sender_id,
                    command.name,
                )
            if not remote_allowed:
                result = CommandResult(
                    message=f"/{command.name} is only available in the local OpenHarness UI."
                )
                async for update in self._stream_command_result(
                    bundle=bundle,
                    message=message,
                    session_key=session_key,
                    user_prompt=user_prompt,
                    result=result,
                ):
                    yield update
                return
            result = await command.handler(
                args,
                CommandContext(
                    engine=bundle.engine,
                    hooks_summary=bundle.hook_summary(),
                    mcp_summary=bundle.mcp_summary(),
                    plugin_summary=bundle.plugin_summary(),
                    cwd=bundle.cwd,
                    tool_registry=bundle.tool_registry,
                    app_state=bundle.app_state,
                    session_backend=bundle.session_backend,
                    session_id=bundle.session_id,
                    extra_skill_dirs=bundle.extra_skill_dirs,
                    extra_plugin_roots=bundle.extra_plugin_roots,
                    memory_backend=create_memory_command_backend(self._workspace),
                    include_project_memory=False,
                ),
            )
            async for update in self._stream_command_result(
                bundle=bundle,
                message=message,
                session_key=session_key,
                user_prompt=user_prompt,
                result=result,
            ):
                yield update
            return

        async for update in self._stream_engine_message(
            bundle=bundle,
            message=message,
            session_key=session_key,
            user_prompt=user_prompt,
            user_message=user_message,
        ):
            yield update

    async def _stream_command_result(
        self,
        *,
        bundle: RuntimeBundle,
        message: InboundMessage,
        session_key: str,
        user_prompt: str,
        result,
    ):
        if result.refresh_runtime:
            bundle = await self._refresh_bundle(session_key, bundle, user_prompt)

        if result.message:
            yield GatewayStreamUpdate(
                kind="final",
                text=result.message,
                metadata={"_session_key": session_key, "_command": True},
            )

        if result.submit_prompt is not None:
            original_model = bundle.engine.model
            if result.submit_model:
                bundle.engine.set_model(result.submit_model)
            try:
                async for update in self._stream_engine_message(
                    bundle=bundle,
                    message=message,
                    session_key=session_key,
                    user_prompt=result.submit_prompt,
                    user_message=result.submit_prompt,
                ):
                    yield update
            finally:
                if result.submit_model:
                    bundle.engine.set_model(original_model)
            return

        if result.continue_pending:
            settings = bundle.current_settings()
            if bundle.enforce_max_turns:
                bundle.engine.set_max_turns(settings.max_turns)
            bundle.engine.set_system_prompt(
                self._runtime_system_prompt(bundle, _last_user_text(bundle.engine.messages))
            )
            turns = result.continue_turns if result.continue_turns is not None else bundle.engine.max_turns
            reply_parts: list[str] = []
            artifact_paths: list[str] = []
            turn_stats: dict = {"tool_calls": [], "error": None}
            started_at = time.monotonic()
            try:
                async for event in bundle.engine.continue_pending(max_turns=turns):
                    async for update in self._convert_stream_event(
                        event=event,
                        bundle=bundle,
                        message=message,
                        session_key=session_key,
                        content=user_prompt,
                        reply_parts=reply_parts,
                        artifact_paths=artifact_paths,
                        turn_stats=turn_stats,
                    ):
                        yield update
            except MaxTurnsExceeded as exc:
                turn_stats["error"] = f"Stopped after {exc.max_turns} turns (max_turns)."
                yield GatewayStreamUpdate(
                    kind="error",
                    text=f"Stopped after {exc.max_turns} turns (max_turns).",
                    metadata={"_session_key": session_key},
                )
            await self._save_snapshot(bundle, session_key, user_prompt)
            self._record_invocation(
                bundle=bundle,
                session_key=session_key,
                user_prompt=str(message.content or "") or user_prompt,
                response_text="".join(reply_parts).strip(),
                tool_calls=turn_stats["tool_calls"],
                status="error" if turn_stats.get("error") else "completed",
                error=turn_stats.get("error"),
                started_at=started_at,
            )
            reply = "".join(reply_parts).strip()
            prefers_chinese = _prefers_chinese_progress(user_prompt)
            send_media, overflow_note = _split_outbound_artifacts(artifact_paths, prefers_chinese=prefers_chinese)
            if send_media and not reply:
                reply = "📎 文件已生成，见附件。" if prefers_chinese else "📎 Generated files are attached."
            if overflow_note:
                reply = f"{reply}\n\n{overflow_note}" if reply else overflow_note
            if reply:
                yield GatewayStreamUpdate(
                    kind="final",
                    text=reply,
                    metadata={"_session_key": session_key, "_artifact_paths": send_media},
                )
            return

        await self._save_snapshot(bundle, session_key, user_prompt)

    async def _stream_engine_message(
        self,
        *,
        bundle: RuntimeBundle,
        message: InboundMessage,
        session_key: str,
        user_prompt: str,
        user_message: ConversationMessage | str,
    ):
        bundle.engine.set_system_prompt(self._runtime_system_prompt(bundle, user_prompt))
        reply_parts: list[str] = []
        artifact_paths: list[str] = []
        turn_stats: dict = {"tool_calls": [], "error": None}
        started_at = time.monotonic()
        yield GatewayStreamUpdate(
            kind="progress",
            text=_format_channel_progress(
                channel=message.channel,
                kind="thinking",
                text="Thinking...",
                session_key=session_key,
                content=user_prompt,
            ),
            metadata={"_progress": True, "_session_key": session_key},
        )
        try:
            async for event in bundle.engine.submit_message(user_message):
                if isinstance(event, ErrorEvent) and _should_retry_without_image_input(
                    event.message,
                    bundle.engine.messages,
                ):
                    logger.warning(
                        "ohmo runtime image input rejected; retrying without image blocks session_key=%s session_id=%s message=%r",
                        session_key,
                        bundle.session_id,
                        _content_snippet(event.message),
                    )
                    _strip_image_blocks_from_engine_history(bundle.engine)
                    yield GatewayStreamUpdate(
                        kind="progress",
                        text=_format_channel_progress(
                            channel=message.channel,
                            kind="image_fallback",
                            text=event.message,
                            session_key=session_key,
                            content=user_prompt,
                        ),
                        metadata={"_progress": True, "_session_key": session_key, "_image_fallback": True},
                    )
                    async for retry_event in bundle.engine.continue_pending(max_turns=bundle.engine.max_turns):
                        async for update in self._convert_stream_event(
                            event=retry_event,
                            bundle=bundle,
                            message=message,
                            session_key=session_key,
                            content=user_prompt,
                            reply_parts=reply_parts,
                            artifact_paths=artifact_paths,
                            turn_stats=turn_stats,
                        ):
                            yield update
                    break
                async for update in self._convert_stream_event(
                    event=event,
                    bundle=bundle,
                    message=message,
                    session_key=session_key,
                    content=user_prompt,
                    reply_parts=reply_parts,
                    artifact_paths=artifact_paths,
                    turn_stats=turn_stats,
                ):
                    yield update
        except MaxTurnsExceeded as exc:
            yield GatewayStreamUpdate(
                kind="error",
                text=f"Stopped after {exc.max_turns} turns (max_turns).",
                metadata={"_session_key": session_key},
            )
            await self._save_snapshot(bundle, session_key, user_prompt)
            self._record_invocation(
                bundle=bundle,
                session_key=session_key,
                user_prompt=str(message.content or "") or user_prompt,
                response_text="".join(reply_parts).strip(),
                tool_calls=turn_stats["tool_calls"],
                status="error",
                error=f"Stopped after {exc.max_turns} turns (max_turns).",
                started_at=started_at,
            )
            return
        await self._save_snapshot(bundle, session_key, user_prompt)
        self._record_invocation(
            bundle=bundle,
            session_key=session_key,
            user_prompt=str(message.content or "") or user_prompt,
            response_text="".join(reply_parts).strip(),
            tool_calls=turn_stats["tool_calls"],
            status="error" if turn_stats.get("error") else "completed",
            error=turn_stats.get("error"),
            started_at=started_at,
        )
        reply = "".join(reply_parts).strip()
        prefers_chinese = _prefers_chinese_progress(user_prompt)
        send_media, overflow_note = _split_outbound_artifacts(artifact_paths, prefers_chinese=prefers_chinese)
        if send_media and not reply:
            reply = "📎 文件已生成，见附件。" if prefers_chinese else "📎 Generated files are attached."
        if overflow_note:
            reply = f"{reply}\n\n{overflow_note}" if reply else overflow_note
        if reply:
            logger.info(
                "ohmo runtime processing complete session_key=%s session_id=%s reply=%r media=%s",
                session_key,
                bundle.session_id,
                _content_snippet(reply),
                len(send_media),
            )
            yield GatewayStreamUpdate(
                kind="final",
                text=reply,
                metadata={"_session_key": session_key, "_artifact_paths": send_media},
            )

    async def _convert_stream_event(
        self,
        *,
        event,
        bundle: RuntimeBundle,
        message: InboundMessage,
        session_key: str,
        content: str,
        reply_parts: list[str],
        artifact_paths: list[str],
        turn_stats: dict | None = None,
    ):
        if isinstance(event, AssistantTextDelta):
            reply_parts.append(event.text)
            return
        if isinstance(event, CompactProgressEvent):
            logger.info(
                "ohmo runtime compact progress session_key=%s session_id=%s phase=%s trigger=%s attempt=%s",
                session_key,
                bundle.session_id,
                event.phase,
                event.trigger,
                event.attempt,
            )
            rendered = _format_channel_progress(
                channel=message.channel,
                kind="compact_progress",
                text=event.message or "",
                session_key=session_key,
                content=content,
                compact_phase=event.phase,
                compact_trigger=event.trigger,
                attempt=event.attempt,
            )
            if rendered:
                yield GatewayStreamUpdate(
                    kind="progress",
                    text=rendered,
                    metadata={"_progress": True, "_session_key": session_key, "_compact": True},
                )
            return
        if isinstance(event, StatusEvent):
            logger.info(
                "ohmo runtime status session_key=%s session_id=%s message=%r",
                session_key,
                bundle.session_id,
                _content_snippet(event.message),
            )
            yield GatewayStreamUpdate(
                kind="progress",
                text=_format_channel_progress(
                    channel=message.channel,
                    kind="status",
                    text=event.message,
                    session_key=session_key,
                    content=content,
                ),
                metadata={"_progress": True, "_session_key": session_key},
            )
            return
        if isinstance(event, ToolExecutionStarted):
            if turn_stats is not None:
                turn_stats.setdefault("tool_calls", []).append(
                    {
                        "tool_name": event.tool_name,
                        "tool_input": event.tool_input,
                        "output": None,
                        "is_error": None,
                        "metadata": None,
                    }
                )
            summary = _summarize_tool_input(event.tool_name, event.tool_input)
            logger.info(
                "ohmo runtime tool start session_key=%s session_id=%s tool=%s summary=%r",
                session_key,
                bundle.session_id,
                event.tool_name,
                summary,
            )
            hint = f"Using {event.tool_name}"
            if summary:
                hint = f"{hint}: {summary}"
            yield GatewayStreamUpdate(
                kind="tool_hint",
                text=_format_channel_progress(
                    channel=message.channel,
                    kind="tool_hint",
                    text=hint,
                    session_key=session_key,
                    content=content,
                ),
                metadata={
                    "_progress": True,
                    "_tool_hint": True,
                    "_session_key": session_key,
                },
            )
            return
        if isinstance(event, ToolExecutionCompleted):
            if turn_stats is not None:
                for call in reversed(turn_stats.get("tool_calls", [])):
                    if call.get("tool_name") == event.tool_name and call.get("output") is None:
                        call["output"] = event.output
                        call["is_error"] = event.is_error
                        call["metadata"] = event.metadata
                        break
            logger.info(
                "ohmo runtime tool complete session_key=%s session_id=%s tool=%s",
                session_key,
                bundle.session_id,
                event.tool_name,
            )
            if not event.is_error:
                for path in _artifact_paths_from_tool_metadata(event.metadata, self._artifact_roots):
                    # Keep last-write order so repeated writes surface once, at the end.
                    if path in artifact_paths:
                        artifact_paths.remove(path)
                    artifact_paths.append(path)
            return
        if isinstance(event, ErrorEvent):
            if turn_stats is not None:
                turn_stats["error"] = event.message
            logger.error(
                "ohmo runtime error session_key=%s session_id=%s message=%r",
                session_key,
                bundle.session_id,
                _content_snippet(event.message),
            )
            yield GatewayStreamUpdate(
                kind="error",
                text=event.message,
                metadata={"_session_key": session_key},
            )
            return
        if isinstance(event, AssistantTurnComplete):
            await self._save_snapshot(bundle, session_key, content)
            if not reply_parts:
                reply_parts.append(event.message.text.strip())
            if self._is_output_limit_stop_reason(getattr(event, "stop_reason", None)):
                reply_parts.append(
                    "\n\n[System notice] The model reached the max output token limit for this turn. "
                    "Reply with 'continue' to keep going, or raise max output tokens in model settings."
                )

    @staticmethod
    def _is_output_limit_stop_reason(stop_reason: str | None) -> bool:
        return (stop_reason or "").strip().lower() in {
            "length",
            "max_tokens",
            "max_completion_tokens",
        }

    async def _save_snapshot(self, bundle: RuntimeBundle, session_key: str, user_prompt: str) -> None:
        tool_metadata = getattr(bundle.engine, "tool_metadata", {}) or {}
        session_metadata = self._session_metadata.get(session_key, {})
        self._session_backend.save_snapshot(
            cwd=self._cwd,
            model=bundle.current_settings().model,
            system_prompt=self._runtime_system_prompt(bundle, user_prompt),
            messages=bundle.engine.messages,
            usage=bundle.engine.total_usage,
            session_id=bundle.session_id,
            session_key=session_key,
            agent_name=session_metadata.get("agent_name") or self._session_agents.get(session_key),
            channel=session_metadata.get("channel"),
            platform=session_metadata.get("platform"),
            bot_name=session_metadata.get("bot_name"),
            chat_id=session_metadata.get("chat_id"),
            sender_id=session_metadata.get("sender_id"),
            sender_name=session_metadata.get("sender_name"),
            tool_metadata=tool_metadata,
        )
        logger.info(
            "ohmo runtime saved snapshot session_key=%s session_id=%s message_count=%s",
            session_key,
            bundle.session_id,
            len(bundle.engine.messages),
        )

    def _turn_usage_delta(self, session_key: str, bundle: RuntimeBundle) -> UsageSnapshot:
        """Per-turn usage: the reused engine accumulates across turns, so diff
        against the last recorded total. A rebuilt/cleared engine restarts its
        counter from zero, which shows up as a shrinking total."""
        total = getattr(bundle.engine, "total_usage", None)
        current_in = int(getattr(total, "input_tokens", 0) or 0)
        current_out = int(getattr(total, "output_tokens", 0) or 0)
        base_in, base_out = self._session_usage_baseline.get(session_key, (0, 0))
        if current_in < base_in or current_out < base_out:
            base_in = base_out = 0
        self._session_usage_baseline[session_key] = (current_in, current_out)
        return UsageSnapshot(input_tokens=current_in - base_in, output_tokens=current_out - base_out)

    def _record_invocation(
        self,
        *,
        bundle: RuntimeBundle,
        session_key: str,
        user_prompt: str,
        response_text: str,
        tool_calls: list,
        status: str,
        error: str | None,
        started_at: float,
    ) -> None:
        """Persist one channel turn as an invocation record so remote traffic
        (DingTalk etc.) shows up in metrics alongside web/api calls. History
        lives in the session snapshot, so messages stay empty per turn."""
        metadata = self._session_metadata.get(session_key, {})
        channel = metadata.get("channel") or "remote"
        try:
            model = bundle.current_settings().model or ""
        except Exception:
            model = getattr(bundle.engine, "model", "") or ""
        try:
            save_invocation_record(
                cwd=self._cwd,
                workspace=self._workspace,
                model=model,
                system_prompt="",
                messages=[],
                usage=self._turn_usage_delta(session_key, bundle),
                session_id=bundle.session_id,
                agent_name=metadata.get("agent_name") or self._session_agents.get(session_key),
                channel=channel,
                platform=metadata.get("platform") or channel,
                request_content=user_prompt,
                response_text=response_text or None,
                status=status,
                tool_calls=tool_calls,
                error=error,
                trace_id=get_trace_id(),
                duration_ms=int((time.monotonic() - started_at) * 1000),
            )
        except Exception:
            logger.exception(
                "Failed to persist invocation record session_key=%s session_id=%s",
                session_key,
                bundle.session_id,
            )

    async def _refresh_bundle(
        self,
        session_key: str,
        bundle: RuntimeBundle,
        latest_user_prompt: str | None,
    ) -> RuntimeBundle:
        snapshot = sanitize_conversation_messages(list(bundle.engine.messages))
        prior_session_id = bundle.session_id
        await close_runtime(bundle)
        agent_def = self._resolve_agent_definition(self._session_agents.get(session_key))
        refreshed = await build_runtime(
            cwd=self._cwd,
            model=self._model_for_agent(agent_def),
            max_turns=agent_def.max_turns if agent_def and agent_def.max_turns else self._max_turns,
            system_prompt=self._base_system_prompt(agent_def),
            active_profile=self._provider_profile,
            permission_mode=agent_def.permission_mode if agent_def else None,
            session_backend=self._session_backend,
            enforce_max_turns=self._max_turns is not None or bool(agent_def and agent_def.max_turns),
            restore_messages=[message.model_dump(mode="json") for message in snapshot],
            restore_tool_metadata=getattr(bundle.engine, "tool_metadata", {}) or {},
            extra_skill_dirs=(str(get_skills_dir(self._workspace)),),
            extra_plugin_roots=(str(get_plugins_dir(self._workspace)),),
            memory_backend=create_memory_command_backend(self._workspace),
            include_project_memory=False,
        )
        self._apply_agent_tool_policy(refreshed, agent_def)
        refreshed.session_id = prior_session_id
        await start_runtime(refreshed)
        refreshed.engine.set_system_prompt(self._runtime_system_prompt(refreshed, latest_user_prompt))
        self._bundles[session_key] = refreshed
        logger.info(
            "ohmo runtime refreshed session_key=%s session_id=%s message_count=%s",
            session_key,
            refreshed.session_id,
            len(refreshed.engine.messages),
        )
        return refreshed

    async def reset_session(self, session_key: str) -> bool:
        """Close an active bundle and drop the latest snapshot pointer for a session key."""
        reset_any = False
        bundle = self._bundles.pop(session_key, None)
        if bundle is not None:
            await close_runtime(bundle)
            reset_any = True
        if self._session_backend.delete_latest_for_session_key(session_key):
            reset_any = True
        self._session_agents.pop(session_key, None)
        self._session_metadata.pop(session_key, None)
        return reset_any

    def _runtime_system_prompt(self, bundle: RuntimeBundle, latest_user_prompt: str | None) -> str:
        if not hasattr(bundle, "current_settings"):
            return build_ohmo_system_prompt(self._cwd, workspace=self._workspace, extra_prompt=None)
        settings = bundle.current_settings()
        if not hasattr(settings, "system_prompt"):
            return build_ohmo_system_prompt(self._cwd, workspace=self._workspace, extra_prompt=None)
        return build_runtime_system_prompt(
            settings,
            cwd=self._cwd,
            latest_user_prompt=latest_user_prompt,
            extra_skill_dirs=getattr(bundle, "extra_skill_dirs", ()),
            extra_plugin_roots=getattr(bundle, "extra_plugin_roots", ()),
            include_project_memory=False,
        )

    @staticmethod
    def _agent_name_for_message(message: InboundMessage) -> str | None:
        if not (message.channel == "dingtalk" or message.channel.startswith("dingtalk:")):
            return None
        raw = message.metadata.get("default_agent")
        if not isinstance(raw, str):
            return None
        return raw.strip() or None

    def _session_metadata_for_message(self, message: InboundMessage) -> dict[str, str | None]:
        agent_name = self._agent_name_for_message(message)
        return {
            "channel": message.channel.split(":", 1)[0],
            "platform": str(message.metadata.get("platform") or message.channel.split(":", 1)[0]),
            "bot_name": _optional_str(message.metadata.get("bot_name")),
            "agent_name": agent_name,
            "chat_id": message.chat_id,
            "sender_id": message.sender_id,
            "sender_name": _optional_str(message.metadata.get("sender_name")),
        }

    @staticmethod
    def _resolve_agent_definition(agent_name: str | None) -> AgentDefinition | None:
        if not agent_name:
            return None
        return get_agent_definition(agent_name)

    def _base_system_prompt(self, agent_def: AgentDefinition | None) -> str:
        if agent_def is None:
            return build_ohmo_system_prompt(self._cwd, workspace=self._workspace, extra_prompt=None)
        return agent_def.system_prompt or ""

    def _model_for_agent(self, agent_def: AgentDefinition | None) -> str | None:
        if agent_def is not None and agent_def.model and agent_def.model != "inherit":
            return agent_def.model
        return self._model

    @staticmethod
    def _apply_agent_tool_policy(bundle: RuntimeBundle, agent_def: AgentDefinition | None) -> None:
        if hasattr(bundle, "tool_registry"):
            apply_agent_tool_policy(bundle.tool_registry, agent_def)


def _apply_channel_context_metadata(
    bundle: RuntimeBundle,
    message: InboundMessage,
    session_key: str,
) -> None:
    """Expose channel context to internal agent-side proxy routes."""
    metadata = getattr(bundle.engine, "tool_metadata", None)
    if not isinstance(metadata, dict):
        metadata = {}
        try:
            setattr(bundle.engine, "tool_metadata", metadata)
        except Exception:
            logger.debug("unable to attach channel context metadata", exc_info=True)
            return
    metadata["channel_context"] = {
        "channel": message.channel.split(":", 1)[0],
        "raw_channel": message.channel,
        "chat_id": message.chat_id,
        "sender_id": message.sender_id,
        "source_sender_id": _optional_str(message.metadata.get("source_sender_id")),
        "sender_name": _optional_str(message.metadata.get("sender_name")),
        "message_id": _optional_str(message.metadata.get("message_id")),
        "conversation_id": _optional_str(message.metadata.get("conversation_id")),
        "session_key": session_key,
        "attachment_paths": list(message.media or []),
    }


def _content_snippet(text: str, *, limit: int = 160) -> str:
    """Return a compact single-line preview for logs."""
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def _compute_artifact_roots(*candidates: object) -> list[Path]:
    """Resolve directories that generated files may legitimately live under."""
    roots: list[Path] = []
    for raw in (*candidates, get_data_dir() / "tool_artifacts"):
        if not raw:
            continue
        try:
            root = Path(str(raw)).expanduser().resolve()
        except Exception:
            continue
        if root not in roots:
            roots.append(root)
    return roots


def _artifact_paths_from_tool_metadata(metadata: object, roots: list[Path]) -> list[str]:
    """Extract validated local file paths from a tool result's artifact metadata."""
    if not isinstance(metadata, dict):
        return []
    raw_artifacts = metadata.get("artifacts")
    if not isinstance(raw_artifacts, list):
        return []
    paths: list[str] = []
    for raw_artifact in raw_artifacts:
        if not isinstance(raw_artifact, dict):
            continue
        raw_path = raw_artifact.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        try:
            path = Path(raw_path).expanduser().resolve()
        except Exception:
            continue
        if not path.is_file() or not any(path.is_relative_to(root) for root in roots):
            continue
        if any(part in IGNORED_ARTIFACT_DIRS for part in path.parts):
            continue
        if str(path) not in paths:
            paths.append(str(path))
    return paths


def _split_outbound_artifacts(paths: list[str], *, prefers_chinese: bool) -> tuple[list[str], str]:
    """Return (paths to attach, overflow note) honouring the per-reply cap."""
    if len(paths) <= _MAX_OUTBOUND_ARTIFACTS:
        return list(paths), ""
    send = paths[-_MAX_OUTBOUND_ARTIFACTS:]
    skipped = [Path(path).name for path in paths[: -_MAX_OUTBOUND_ARTIFACTS]]
    if prefers_chinese:
        note = (
            f"📎 本轮共生成 {len(paths)} 个文件，已随消息发送最新 {len(send)} 个。"
            f"未发送：{'、'.join(skipped)}。"
        )
    else:
        note = (
            f"📎 {len(paths)} files were generated this turn; the latest {len(send)} are attached. "
            f"Not sent: {', '.join(skipped)}."
        )
    return send, note


def _summarize_tool_input(tool_name: str, tool_input: dict[str, object]) -> str:
    if not tool_input:
        return ""
    for key in ("url", "query", "pattern", "path", "file_path", "command"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            return text if len(text) <= 120 else text[:120] + "..."
    try:
        raw = json.dumps(tool_input, ensure_ascii=False, sort_keys=True)
    except TypeError:
        raw = str(tool_input)
    return raw if len(raw) <= 120 else raw[:120] + "..."


def _format_channel_progress(
    *,
    channel: str,
    kind: str,
    text: str,
    session_key: str,
    content: str,
    compact_phase: str | None = None,
    compact_trigger: str | None = None,
    attempt: int | None = None,
) -> str:
    if channel not in {
        "feishu",
        "telegram",
        "slack",
        "discord",
        "matrix",
        "whatsapp",
        "email",
        "dingtalk",
        "qq",
        "wechat",
    }:
        return text
    prefers_chinese = _prefers_chinese_progress(content)
    if kind == "thinking":
        seed = f"{session_key}|{content}".encode("utf-8")
        phrases = _CHANNEL_THINKING_PHRASES if prefers_chinese else _CHANNEL_THINKING_PHRASES_EN
        idx = int(hashlib.sha256(seed).hexdigest(), 16) % len(phrases)
        return phrases[idx]
    if kind == "tool_hint":
        if prefers_chinese:
            if text.startswith("Using "):
                return "🛠️ " + text.replace("Using ", "正在使用 ", 1)
            return f"🛠️ {text}"
        return text if text.startswith("🛠️ ") else f"🛠️ {text}"
    if kind == "image_fallback":
        if prefers_chinese:
            return "🖼️ 当前模型不支持图片输入，我先改用附件路径和摘要继续。"
        return "🖼️ The active model does not support image input. I’ll retry with attachment paths and summaries."
    if kind == "status":
        normalized = text.strip()
        if normalized == "Auto-compacting conversation memory to keep things fast and focused.":
            if prefers_chinese:
                return "🧠 聊天有点长啦，我先帮你蹦蹦跳跳压缩一下记忆，马上带着重点回来～"
            return "🧠 This chat is getting long — I’m doing a quick memory squeeze and hopping right back with the good bits."
        if text.startswith(("🤔", "🧠", "✨", "🔎", "🪄", "🛠️", "🫧")):
            return text
        return f"🫧 {text}"
    if kind == "compact_progress":
        if compact_phase == "hooks_start":
            if prefers_chinese:
                if compact_trigger == "reactive":
                    return "🫧 上下文有点超长，我先准备压缩一下记忆，然后立刻继续重试～"
                return "🫧 我先把上下文和记忆准备一下，马上开始压缩重点～"
            if compact_trigger == "reactive":
                return "🫧 The context got too large. I’m preparing a quick memory compaction before retrying."
            return "🫧 Let me get the context ready before I compact the conversation."
        if compact_phase == "context_collapse_start":
            if prefers_chinese:
                return "🫧 我先把太长的上下文折叠一下，让后面的压缩更快一点～"
            return "🫧 I’m collapsing the oversized context first so compaction can move faster."
        if compact_phase == "context_collapse_end":
            if prefers_chinese:
                return "🫧 上下文已经先收紧了一层，继续压缩重点～"
            return "🫧 The context is trimmed down now. Continuing with the main compaction."
        if compact_phase in {"session_memory_start", "compact_start"}:
            if prefers_chinese:
                if compact_phase == "session_memory_start":
                    return "🧠 我先把前面的聊天重点悄悄捋顺一下，马上继续～"
                if compact_trigger == "reactive":
                    return "🧠 这轮上下文太长了，我先压缩一下记忆，然后马上继续重试～"
                return "🧠 聊天有点长啦，我先帮你悄悄压缩一下记忆，马上继续～"
            if compact_phase == "session_memory_start":
                return "🧠 Let me quickly condense the earlier parts of this chat, then I’ll keep going."
            if compact_trigger == "reactive":
                return "🧠 The context is too large for this turn. I’ll compact the memory and retry."
            return "🧠 This chat is getting long. I’ll compact the memory and keep going."
        if compact_phase == "compact_retry":
            suffix = f" (attempt {attempt})" if attempt is not None else ""
            if prefers_chinese:
                return f"🔁 压缩记忆这一步有点卡，我换个方式再试一次{suffix}。"
            return f"🔁 Compaction got stuck, trying a lighter retry{suffix}."
        if compact_phase == "compact_failed":
            if prefers_chinese:
                return "⚠️ 这次记忆压缩没成功，我先跳过它继续处理你的消息。"
            return "⚠️ Memory compaction did not complete. I’m skipping it and continuing."
        return ""
    return text


def _build_inbound_user_message(message: InboundMessage, *, session_key: str | None = None) -> ConversationMessage:
    """Convert an inbound channel message into user content blocks."""
    content: list[TextBlock | ImageBlock] = []
    channel_metadata_context = _build_channel_metadata_context(message, session_key)
    speaker_context = _build_speaker_context(message)
    base = (message.content or "").strip()
    if channel_metadata_context:
        content.append(TextBlock(text=channel_metadata_context))
    if speaker_context:
        prefix = "\n\n" if channel_metadata_context else ""
        content.append(TextBlock(text=prefix + speaker_context))
    if base:
        content.append(TextBlock(text=base))

    attachment_notes = _build_attachment_notes(message.media)
    if attachment_notes:
        prefix = "\n\n" if base else ""
        content.append(TextBlock(text=prefix + attachment_notes))

    for media_path in message.media:
        if not _is_image_attachment(media_path):
            continue
        try:
            content.append(ImageBlock.from_path(media_path))
        except Exception:
            logger.exception("ohmo runtime failed to encode image attachment path=%s", media_path)

    return ConversationMessage.from_user_content(content)


def _build_channel_metadata_context(message: InboundMessage, session_key: str | None) -> str:
    """Return channel metadata that skills can persist through internal APIs."""
    if not (message.channel == "dingtalk" or message.channel.startswith("dingtalk:")):
        return ""
    metadata = message.metadata or {}
    # DingTalk may only expose a display/nick name for business use. The
    # transport sender_id can be a routing key, so do not persist it as a
    # business source sender id unless the channel explicitly provides one.
    payload = {
        "channel": message.channel.split(":", 1)[0],
        "chatId": message.chat_id,
        "senderId": _optional_str(metadata.get("source_sender_id")),
        "senderName": _optional_str(metadata.get("sender_name")),
        "messageId": _optional_str(metadata.get("message_id")),
        "conversationId": _optional_str(metadata.get("conversation_id")),
        "sessionKey": session_key,
        "attachmentPaths": list(message.media or []),
    }
    return "[Channel metadata for API recording]\n" + json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
    )


def _should_retry_without_image_input(error_message: str, messages: list[ConversationMessage]) -> bool:
    """Return True when a provider rejects image input and history contains images."""
    if not _history_has_image_blocks(messages):
        return False
    normalized = error_message.lower()
    image_signal = any(
        phrase in normalized
        for phrase in (
            "image input",
            "image_url",
            "multimodal",
            "vision",
            "image content",
        )
    )
    rejection_signal = any(
        phrase in normalized
        for phrase in (
            "no endpoints found",
            "not support",
            "does not support",
            "unsupported",
            "cannot support",
            "can't support",
        )
    )
    return image_signal and rejection_signal


def _history_has_image_blocks(messages: list[ConversationMessage]) -> bool:
    return any(any(isinstance(block, ImageBlock) for block in message.content) for message in messages)


def _strip_image_blocks_from_engine_history(engine) -> None:
    messages = _strip_image_blocks_from_messages(list(engine.messages))
    if hasattr(engine, "load_messages"):
        engine.load_messages(messages)
    else:
        engine.messages = messages


def _strip_image_blocks_from_messages(messages: list[ConversationMessage]) -> list[ConversationMessage]:
    # The resend/switch-model guidance is only appended to the most recent
    # image-bearing message; earlier stripped messages keep a compact marker so
    # a long history is not flooded with repeated instructions.
    last_image_index = max(
        (
            index
            for index, message in enumerate(messages)
            if any(isinstance(block, ImageBlock) for block in message.content)
        ),
        default=-1,
    )
    return [
        _strip_image_blocks_from_message(message, include_guidance=index == last_image_index)
        for index, message in enumerate(messages)
    ]


def _strip_image_blocks_from_message(
    message: ConversationMessage,
    *,
    include_guidance: bool = True,
) -> ConversationMessage:
    stripped = [block for block in message.content if isinstance(block, ImageBlock)]
    if not stripped:
        return message
    content = [block for block in message.content if not isinstance(block, ImageBlock)]
    has_text = any(isinstance(block, TextBlock) and block.text.strip() for block in content)
    note = _image_fallback_note(stripped, include_guidance=include_guidance)
    # Provider clients join adjacent text blocks without a separator, so pad
    # the note when the message keeps its own text.
    content.append(TextBlock(text="\n\n" + note if has_text else note))
    return message.model_copy(update={"content": content})


def _image_fallback_note(stripped: list[ImageBlock], *, include_guidance: bool) -> str:
    sources = ", ".join(
        block.source_path or f"<{block.media_type or 'image'}>" for block in stripped
    )
    note = _IMAGE_FALLBACK_NOTE_PREFIX
    note += f": {sources}." if sources else "."
    if include_guidance:
        note += _IMAGE_FALLBACK_NOTE_GUIDANCE
    return note + "]"


def _build_speaker_context(message: InboundMessage) -> str:
    """Return a lightweight speaker header for group-chat messages."""
    metadata = message.metadata or {}
    chat_type = str(metadata.get("chat_type") or "").strip().lower()
    sender_label = (
        str(metadata.get("sender_display_name") or "").strip()
        or str(metadata.get("sender_label") or "").strip()
        or str(message.sender_id).strip()
    )
    if chat_type != "group":
        return ""
    if not sender_label:
        sender_label = "unknown"
    return (
        "[Channel speaker]\n"
        f"This message was sent in a group chat by: {sender_label}\n"
        f"Sender id: {message.sender_id}"
    )


def _build_attachment_notes(media_paths: list[str]) -> str:
    """Build textual attachment notes for non-image context and persistence."""
    non_image_paths = [media_path for media_path in media_paths if not _is_image_attachment(media_path)]
    if not non_image_paths:
        return ""
    lines = [
        "[Channel attachments]",
        "The following attachments were downloaded locally for this message.",
        "Inspect them by path if needed.",
    ]
    for media_path in non_image_paths:
        lines.append(f"- {_describe_media_path(media_path)}")
        summary = _summarize_attachment(media_path)
        if summary:
            for part in summary.splitlines():
                lines.append(f"  {part}")
    return "\n".join(lines).strip()


def _describe_media_path(media_path: str) -> str:
    """Return a short type + path description for an inbound attachment."""
    suffix = Path(media_path).suffix.lower()
    if _is_image_attachment(media_path):
        kind = "image"
    elif suffix in {".mp3", ".wav", ".m4a", ".opus", ".aac"}:
        kind = "audio"
    elif suffix in {".mp4", ".mov", ".avi", ".mkv", ".webm"}:
        kind = "video"
    else:
        kind = "file"
    filename = os.path.basename(media_path)
    return f"{kind}: {filename} (path: {media_path})"


def _is_image_attachment(media_path: str) -> bool:
    mime, _ = mimetypes.guess_type(media_path)
    return bool(mime and mime.startswith("image/"))


def _summarize_attachment(media_path: str) -> str:
    """Return a compact summary/header for a downloaded attachment."""
    path = Path(media_path)
    if not path.exists() or not path.is_file():
        return "summary: attachment is unavailable on disk"
    try:
        stat = path.stat()
    except OSError:
        return "summary: attachment metadata is unavailable"

    mime, _ = mimetypes.guess_type(str(path))
    summary_lines = [f"summary: size={stat.st_size} bytes mime={mime or 'unknown'}"]
    try:
        head = path.read_bytes()[:_TEXT_PREVIEW_BYTES]
    except OSError:
        return "\n".join(summary_lines)

    if _is_image_attachment(str(path)):
        return "\n".join(summary_lines)

    text_preview = _decode_text_preview(head)
    if text_preview is not None:
        summary_lines.append(f"text preview: {text_preview}")
        return "\n".join(summary_lines)

    head_hex = head[:_BINARY_HEAD_BYTES].hex(" ")
    if head_hex:
        summary_lines.append(f"binary header: {head_hex}")
    return "\n".join(summary_lines)


def _decode_text_preview(data: bytes) -> str | None:
    """Return a compact text preview when a file looks text-like."""
    if not data:
        return ""
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    printable = sum(1 for char in decoded if char in string.printable or char.isprintable() or char in "\n\r\t")
    if printable / max(len(decoded), 1) < 0.9:
        return None
    normalized = " ".join(decoded.split())
    if not normalized:
        return ""
    if len(normalized) > _TEXT_PREVIEW_CHARS:
        return normalized[: _TEXT_PREVIEW_CHARS - 3] + "..."
    return normalized


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _prefers_chinese_progress(content: str) -> bool:
    cjk_count = 0
    latin_count = 0
    for char in content:
        codepoint = ord(char)
        if (
            0x4E00 <= codepoint <= 0x9FFF
            or 0x3400 <= codepoint <= 0x4DBF
            or 0x20000 <= codepoint <= 0x2A6DF
            or 0x2A700 <= codepoint <= 0x2B73F
            or 0x2B740 <= codepoint <= 0x2B81F
            or 0x2B820 <= codepoint <= 0x2CEAF
            or 0xF900 <= codepoint <= 0xFAFF
        ):
            cjk_count += 1
        elif ("A" <= char <= "Z") or ("a" <= char <= "z"):
            latin_count += 1
    if cjk_count == 0:
        return False
    if latin_count == 0:
        return True
    return cjk_count >= latin_count
