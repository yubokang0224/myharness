# -*- coding: utf-8 -*-
"""Octop 专家团队协同工具：agent_list 与 ask_agent。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from openharness.coordinator.agent_definitions import (
    AgentDefinition,
    get_all_agent_definitions,
)
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult

logger = logging.getLogger(__name__)

_HIDDEN_SYSTEM_AGENTS = {
    "statusline-setup",
    "claude-code-guide",
    "Explore",
    "Plan",
    "worker",
    "verification",
    "general-purpose",
}


class AgentListToolInput(BaseModel):
    """查询团队成员或可协作专家列表的参数。"""

    include_details: bool = Field(
        default=True,
        description="是否返回每位专家的技能与工具摘要",
    )


class AgentListTool(BaseTool):
    """列出当前团队内的专家成员及其职责说明。"""

    name = "agent_list"
    description = (
        "列出当前专家团队中的所有成员（或系统可协作专家）及其职责说明、专属技能与工具，"
        "供团队主持人在拆解任务和派工前匹配合适的专家成员。"
    )
    input_model = AgentListToolInput

    def is_read_only(self, arguments: AgentListToolInput) -> bool:
        del arguments
        return True

    async def execute(
        self,
        arguments: AgentListToolInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        all_defs = {
            d.name: d
            for d in get_all_agent_definitions()
            if d.name not in _HIDDEN_SYSTEM_AGENTS
        }
        team_members_filter: list[str] | None = None
        raw_members = context.metadata.get("team_member_ids")
        if isinstance(raw_members, list) and raw_members:
            team_members_filter = [str(m).strip() for m in raw_members if str(m).strip()]

        selected: list[AgentDefinition] = []
        if team_members_filter:
            for member_id in team_members_filter:
                defn = all_defs.get(member_id)
                if defn and getattr(defn, "kind", "expert") != "team":
                    selected.append(defn)
        else:
            selected = [
                d for d in all_defs.values() if getattr(d, "kind", "expert") != "team"
            ]

        if not selected:
            return ToolResult(
                output="当前没有可调度的专家成员，请先在团队配置中添加至少 2 名专家成员。",
                metadata={"members": []},
            )

        lines = ["当前可调度的团队专家成员列表："]
        member_items: list[dict[str, Any]] = []
        for idx, defn in enumerate(selected, start=1):
            skills_text = "、".join(defn.skills) if defn.skills else "通用能力"
            line = f"{idx}. 【{defn.name}】— {defn.description or '未填写职责说明'}"
            if arguments.include_details:
                line += f"（技能：{skills_text}）"
            lines.append(line)
            member_items.append(
                {
                    "name": defn.name,
                    "description": defn.description or "",
                    "color": defn.color,
                    "skills": list(defn.skills or []),
                }
            )

        return ToolResult(
            output="\n".join(lines),
            metadata={"members": member_items},
        )


class AskAgentToolInput(BaseModel):
    """向指定团队成员专家派工的参数。"""

    agent_name: str = Field(
        description="目标团队成员专家的名称（必须与 agent_list 中的专家名称一致，例如：采购助手）"
    )
    message: str = Field(
        description=(
            "给该专家成员定制的任务说明书。必须写清：1) 明确目标；2) 范围与约束；"
            "3) 期望交付格式；4) 必要的上下文材料。严禁原样粘贴用户原话。"
        )
    )
    context_summary: str | None = Field(
        default=None,
        description="可选的补充背景材料或前序专家产出的关键结论",
    )


class AskAgentTool(BaseTool):
    """调用指定团队成员专家执行专业任务并返回其发言结果。"""

    name = "ask_agent"
    description = (
        "向指定的团队成员专家派发任务说明书并由该专家独立完成专业工作（调研、查知识库、分析、写文档、写代码等）。"
        "成员的回复会直接作为独立专家发言展示在团队群聊时间线上。"
    )
    input_model = AskAgentToolInput

    def is_read_only(self, arguments: AskAgentToolInput) -> bool:
        del arguments
        return True

    async def execute(
        self,
        arguments: AskAgentToolInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        target_name = arguments.agent_name.strip().lstrip("@")
        if not target_name:
            return ToolResult(output="请指定要派工的目标专家名称（agent_name）。", is_error=True)

        all_defs = {
            d.name: d
            for d in get_all_agent_definitions()
            if d.name not in _HIDDEN_SYSTEM_AGENTS
        }
        member_def = all_defs.get(target_name)
        if member_def is None:
            # 尝试大小写不敏感或包含匹配
            for name, defn in all_defs.items():
                if name.lower() == target_name.lower():
                    member_def = defn
                    break

        raw_members = context.metadata.get("team_member_ids")
        allowed_members = (
            [str(m).strip() for m in raw_members if str(m).strip()]
            if isinstance(raw_members, list) and raw_members
            else None
        )

        if member_def is None:
            available = allowed_members or [
                d.name for d in all_defs.values() if getattr(d, "kind", "expert") != "team"
            ]
            return ToolResult(
                output=f"未找到名为「{target_name}」的专家成员。当前可用成员：{', '.join(available)}",
                is_error=True,
            )

        if getattr(member_def, "kind", "expert") == "team":
            return ToolResult(
                output=f"「{member_def.name}」是团队主持人，不能将团队作为成员嵌套派工。",
                is_error=True,
            )

        if allowed_members and member_def.name not in allowed_members:
            return ToolResult(
                output=(
                    f"专家「{member_def.name}」不在当前团队编制中。"
                    f"当前团队仅允许调度以下成员：{', '.join(allowed_members)}"
                ),
                is_error=True,
            )

        notify_cb = context.metadata.get("team_status_callback")
        if callable(notify_cb):
            try:
                maybe_coro = notify_cb(f"👥 已请「{member_def.name}」处理任务，正在生成回复…")
                if hasattr(maybe_coro, "__await__"):
                    await maybe_coro
            except Exception:
                logger.debug("team_status_callback failed", exc_info=True)

        try:
            from openharness.config import load_settings
            from openharness.config.model_catalog import resolve_agent_settings
            from openharness.engine import QueryEngine
            from openharness.engine.messages import ConversationMessage, TextBlock
            from openharness.engine.stream_events import (
                AssistantTextDelta,
                AssistantTurnComplete,
                ErrorEvent,
                ToolExecutionCompleted,
                ToolExecutionStarted,
            )
            from openharness.permissions import PermissionChecker
            from openharness.prompts.context import build_runtime_system_prompt
            from openharness.tools import create_default_tool_registry
            from openharness.ui.runtime import _resolve_api_client_from_settings
            from ohmo.gateway.tool_policy import apply_agent_tool_policy

            base_settings = load_settings()
            member_settings = resolve_agent_settings(base_settings, member_def)
            api_client = _resolve_api_client_from_settings(member_settings)

            mcp_manager = context.metadata.get("mcp_manager")
            member_registry = create_default_tool_registry(mcp_manager)
            apply_agent_tool_policy(member_registry, member_def)
            # 禁止成员再次异步往群里拉人或递归创建团队
            for forbidden in ("ask_agent", "team_create", "team_delete"):
                member_registry._tools.pop(forbidden, None)

            member_cwd = Path(context.cwd).resolve()
            if member_def.cwd:
                try:
                    cand = Path(member_def.cwd).expanduser().resolve()
                    if cand.is_dir():
                        member_cwd = cand
                except Exception:
                    pass

            task_prompt_parts = [
                f"【团队主持人派工任务】\n{arguments.message.strip()}",
            ]
            if arguments.context_summary and arguments.context_summary.strip():
                task_prompt_parts.append(
                    f"【补充背景与上下文】\n{arguments.context_summary.strip()}"
                )
            full_task_prompt = "\n\n".join(task_prompt_parts)

            team_name = str(context.metadata.get("team_name") or "专家团队")
            member_system_prompt = (
                build_runtime_system_prompt(
                    member_settings,
                    cwd=member_cwd,
                    latest_user_prompt=full_task_prompt,
                )
                + f"\n\n[团队协同模式]\n"
                f"你当前正在「{team_name}」中作为专家成员【{member_def.name}】发言。"
                f"请直接针对团队主持人派发的任务说明书交付专业、详实的分析或执行结果，"
                f"你的回答将直接展示在团队群聊时间线上。无需重复寒暄。"
            )

            member_tool_metadata = dict(context.metadata)
            # 防止子成员继承团队主持人的 member 过滤标识
            member_tool_metadata.pop("team_member_ids", None)
            member_tool_metadata["active_agent_name"] = member_def.name

            permission_checker = PermissionChecker(member_settings.permission)
            member_engine = QueryEngine(
                api_client=api_client,
                tool_registry=member_registry,
                permission_checker=permission_checker,
                cwd=member_cwd,
                model=member_settings.model,
                system_prompt=member_system_prompt,
                max_tokens=member_settings.max_tokens,
                context_window_tokens=member_settings.context_window_tokens
                or member_settings.memory.context_window_tokens,
                auto_compact_threshold_tokens=(
                    member_settings.auto_compact_threshold_tokens
                    or member_settings.memory.auto_compact_threshold_tokens
                ),
                max_turns=member_def.max_turns or 20,
                tool_metadata=member_tool_metadata,
            )

            msg = ConversationMessage(
                role="user",
                content=[TextBlock(text=full_task_prompt)],
            )
            reply_chunks: list[str] = []
            member_tool_calls: list[dict[str, Any]] = []
            member_artifacts: list[dict[str, Any]] = []
            error_msg: str | None = None

            async for ev in member_engine.submit_message(msg):
                if isinstance(ev, AssistantTextDelta):
                    reply_chunks.append(ev.text)
                elif isinstance(ev, ToolExecutionStarted):
                    member_tool_calls.append(
                        {
                            "tool_name": ev.tool_name,
                            "tool_input": ev.tool_input,
                            "output": None,
                            "is_error": False,
                        }
                    )
                elif isinstance(ev, ToolExecutionCompleted):
                    for tc in reversed(member_tool_calls):
                        if tc["tool_name"] == ev.tool_name and tc["output"] is None:
                            tc["output"] = ev.output
                            tc["is_error"] = ev.is_error
                            break
                    if (
                        not ev.is_error
                        and isinstance(ev.metadata, dict)
                        and isinstance(ev.metadata.get("artifacts"), list)
                    ):
                        member_artifacts.extend(ev.metadata["artifacts"])
                elif isinstance(ev, AssistantTurnComplete):
                    if not reply_chunks and ev.message and ev.message.text:
                        reply_chunks.append(ev.message.text.strip())
                elif isinstance(ev, ErrorEvent):
                    error_msg = ev.message

            member_reply = "".join(reply_chunks).strip()
            if not member_reply and error_msg:
                return ToolResult(
                    output=f"专家「{member_def.name}」执行任务时遇到异常：{error_msg}",
                    is_error=True,
                    metadata={
                        "team_dispatch": True,
                        "speaker_agent": member_def.name,
                        "speaker_color": member_def.color or "cyan",
                        "speaker_avatar": getattr(member_def, "avatar", None),
                        "speaker_description": member_def.description or "",
                        "task_instruction": arguments.message.strip(),
                    },
                )
            if not member_reply:
                member_reply = "（已完成任务处理，无额外文本输出）"

            formatted_output = (
                f"【团队成员发言：{member_def.name}】\n"
                f"{member_reply}\n\n"
                f"---\n"
                f"[系统提示：以上「{member_def.name}」的发言已直接展示在团队群聊时间线上。"
                f"请你作为团队主持人判断是否还需要派工给其他成员；若所有工作已完成，请用不超过三句话做精简收尾总结，不要复述成员已经上墙的正文。]"
            )
            return ToolResult(
                output=formatted_output,
                metadata={
                    "team_dispatch": True,
                    "speaker_agent": member_def.name,
                    "speaker_color": member_def.color or "cyan",
                    "speaker_avatar": getattr(member_def, "avatar", None),
                    "speaker_description": member_def.description or "",
                    "task_instruction": arguments.message.strip(),
                    "member_reply": member_reply,
                    "member_tool_calls": member_tool_calls,
                    "artifacts": member_artifacts,
                },
            )
        except Exception as exc:
            logger.exception("ask_agent execution failed for member %s", member_def.name)
            return ToolResult(
                output=f"调用专家「{member_def.name}」失败：{exc}",
                is_error=True,
                metadata={
                    "team_dispatch": True,
                    "speaker_agent": member_def.name,
                    "speaker_color": member_def.color or "cyan",
                    "task_instruction": arguments.message.strip(),
                },
            )
