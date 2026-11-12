# -*- coding: utf-8 -*-
"""Octop 专家团队（Expert Teams）管理路由。"""

from __future__ import annotations

import logging
from typing import Annotated, Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ohmo.gateway.dependencies import require_agent_admin
import ohmo.gateway.routers.agents as agents_router

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/teams", tags=["teams"])

TEAM_HOST_SOUL_MD = """# 团队主持人

你是这个专家团队的**主持人**，只负责协调和分配，自己尽量不要干活。

用户消息先到你这里，不是自动转给成员。你先消化、再改写、再派工；成员说完后判断要不要收工。成员的正文已经出现在群聊时间线上——那是他们的发言，不是你的草稿。

## 先处理，再调度

1. **先消化**：弄清意图、范围、约束、交付物。缺关键信息就先问一句，不要空派。
2. **再改写**：`ask_agent` 的 `message` 必须是给该成员的任务说明书，禁止原样粘贴用户原话。写清目标、范围/不做、交付格式、必要上下文。
3. **按人定制**：不同成员拿到不同任务，不要把同一句用户原话广播给所有人。
4. **寒暄/澄清自己答**：不需要专业产出时不要派工。

## 你怎么说话

- 寒暄、复述目标、说明「派给谁」：直接答，一两句即可。`ask_agent` 返回后不要再重复「已安排 / 稍等 / 交给谁」，直接根据成员返回结果收口或继续派下一位。
- **所有需要调研、写作、编码、分析、查资料、查知识库、改文件的工作**，必须用 `ask_agent` 派给合适的成员。
- 用户 `@` 了某位成员时，优先派给对方，但仍可再叫别人协同。
- 可以一轮里依次或并行派给多人。

## 成员回来之后

成员结束后你会拿到他的发言结果。这时只做两件事：

1. **判断**：事情是否做完，要不要再派给其他成员补充。
2. **收工**：若可以结束，最多用三句短总结收尾。

不要复述成员已经上墙的全文，不要编造成员没有给出的结论，也不要替他们重做专业工作。
"""

TEAM_HOST_AGENTS_MD = """# 团队准则

你是这个房间的**协调人**，不是执行人。用户找团队，是为了让成员各自出手，不是让你一个人答完。

## 你的核心职责

1. **听清**：先消化用户消息。用一两句确认目标、范围、交付物；缺关键信息先问，不要原样转发。
2. **拆分**：把工作拆成可独立交付的任务，匹配到合适的成员。每人一份改写后的任务说明书，不要广播用户原话。
3. **派工**：用 `ask_agent` 交给成员。`message` 必须自带目标、约束、交付格式和对方需要的上下文。
4. **收口**：成员完成后只判断「要不要再派人 / 是否收工」。收工总结不超过三句，不要复述成员全文。
"""

DEFAULT_TEAM_WELCOME_MESSAGE = (
    "你好！我是团队主持人。请告诉我你的目标或直接 @团队成员，我会为你拆解任务并协调各位专家成员共同完成。"
)


def build_default_team_system_prompt() -> str:
    return f"{TEAM_HOST_SOUL_MD.strip()}\n\n---\n\n{TEAM_HOST_AGENTS_MD.strip()}"


def extract_mentioned_experts(
    user_text: str,
    candidate_names: list[str] | None = None,
) -> list[str]:
    """从用户消息文本中提取通过 @专家名 明确点名的专家列表。"""
    if not user_text or "@" not in user_text:
        return []
    if candidate_names is None:
        all_defs = _all_definitions_map()
        candidates = [
            name
            for name, defn in all_defs.items()
            if getattr(defn, "kind", "expert") != "team"
        ]
    else:
        candidates = list(candidate_names)
    # 按名称长度降序匹配，并将已匹配片段移除，避免短名称误匹配长名称前缀
    remaining = user_text
    mentioned: list[str] = []
    for name in sorted(candidates, key=len, reverse=True):
        token = f"@{name}"
        if name and token in remaining and name not in mentioned:
            mentioned.append(name)
            remaining = remaining.replace(token, " ")
    return mentioned


def build_team_roster_prompt_suffix(agent_def: Any, user_text: str = "") -> str:
    """为团队主持人（或包含 @指定专家 的普通会话）动态生成花名册与点名派工提示词后缀。"""
    is_team = agent_def is not None and getattr(agent_def, "kind", "expert") == "team"
    if is_team:
        member_ids = list(getattr(agent_def, "member_ids", None) or [])
        if not member_ids:
            return ""
        all_defs = _all_definitions_map()
        lines = [
            "\n\n## 当前团队成员花名册（仅可向以下成员调用 `ask_agent` 派工）",
        ]
        for idx, mid in enumerate(member_ids, start=1):
            mdef = all_defs.get(mid)
            desc = mdef.description if mdef and mdef.description else "通用业务专家"
            lines.append(f"{idx}. **{mid}**：{desc}")
        lines.append(
            "\n调用 `ask_agent` 时，`agent_name` 参数必须精确填写上方花名册中的成员名称（如 "
            f"`{member_ids[0]}`），`message` 参数必须填写消化改写后的定制任务说明书。"
        )
        mentioned = extract_mentioned_experts(user_text, member_ids)
        if mentioned:
            lines.append(
                "\n### 【本轮用户 @指定专家 强制派工指令】\n"
                f"用户在本条消息中明确通过 `@` 点名了团队成员：**{', '.join(mentioned)}**。\n"
                f"你必须优先调用 `ask_agent` 工具（`agent_name` 填 `{mentioned[0]}`）将任务派发给被点名的专家，"
                "严禁忽略用户的 `@` 点名或自己直接代答！"
            )
        return "\n".join(lines)

    # 普通会话中若用户通过 @指定专家 点名了其他专家，也生成协同指令
    current_name = getattr(agent_def, "name", "") if agent_def is not None else ""
    mentioned_others = [
        m for m in extract_mentioned_experts(user_text) if m != current_name
    ]
    if mentioned_others:
        return (
            "\n\n## 【本轮用户 @指定专家 协同派工指令】\n"
            f"用户在本条消息中明确通过 `@` 点名了专家：**{', '.join(mentioned_others)}**。\n"
            f"请优先调用 `ask_agent` 工具（`agent_name` 填 `{mentioned_others[0]}`，`message` 填清晰完整的任务说明）"
            "请被点名的专家处理该问题，并在专家返回发言后做简短收尾总结。"
        )
    return ""


class TeamMemberSummary(BaseModel):
    name: str
    description: str = ""
    color: str | None = None
    avatar: str | None = None
    model: str | None = None
    provider_profile: str | None = None
    skills: list[str] = Field(default_factory=list)


class TeamResponse(BaseModel):
    name: str
    description: str = ""
    system_prompt: str = ""
    model: str | None = None
    provider_profile: str | None = None
    effort: str | None = None
    max_turns: int | None = None
    color: str | None = None
    avatar: str | None = None
    welcome_message: str | None = None
    member_ids: list[str] = Field(default_factory=list)
    members: list[TeamMemberSummary] = Field(default_factory=list)
    kind: str = "team"
    source: str = "user"


class CreateTeamRequest(BaseModel):
    name: str
    description: str = ""
    model: str | None = None
    provider_profile: str | None = None
    effort: str | None = None
    max_turns: int | None = None
    color: str | None = "cyan"
    avatar: str | None = None
    welcome_message: str | None = DEFAULT_TEAM_WELCOME_MESSAGE
    member_ids: list[str] = Field(default_factory=list)
    system_prompt: str | None = None


class UpdateTeamRequest(BaseModel):
    description: str | None = None
    model: str | None = None
    provider_profile: str | None = None
    effort: str | None = None
    max_turns: int | None = None
    color: str | None = None
    avatar: str | None = None
    welcome_message: str | None = None
    member_ids: list[str] | None = None
    system_prompt: str | None = None


def _all_definitions_map() -> dict[str, Any]:
    from openharness.coordinator.agent_definitions import (
        get_builtin_agent_definitions,
        load_agents_dir,
    )

    defs: dict[str, Any] = {d.name: d for d in get_builtin_agent_definitions()}
    user_dir = agents_router._agents_dir()
    if user_dir.exists():
        for d in load_agents_dir(user_dir):
            defs[d.name] = d
    return defs


def _validate_and_normalize_members(
    member_ids: list[str],
    all_defs: dict[str, Any],
    *,
    team_name: str = "",
) -> list[str]:
    normalized: list[str] = []
    for raw in member_ids:
        name = str(raw or "").strip()
        if not name or name in normalized:
            continue
        if name == team_name:
            raise HTTPException(status_code=400, detail="团队不能将自身添加为成员")
        target = all_defs.get(name)
        if target is None:
            raise HTTPException(status_code=400, detail=f"成员专家「{name}」不存在")
        if getattr(target, "kind", "expert") == "team":
            raise HTTPException(
                status_code=400,
                detail=f"「{name}」是专家团队，成员只能是普通专家，不能嵌套团队",
            )
        normalized.append(name)

    if len(normalized) < 2:
        raise HTTPException(
            status_code=400,
            detail="创建或保存专家团队至少需要选择 2 名普通专家成员（不含主持人）",
        )
    return normalized


def _build_team_response(defn: Any, all_defs: dict[str, Any]) -> TeamResponse:
    member_ids = list(getattr(defn, "member_ids", None) or [])
    members: list[TeamMemberSummary] = []
    for mid in member_ids:
        mdef = all_defs.get(mid)
        if mdef is not None:
            members.append(
                TeamMemberSummary(
                    name=mdef.name,
                    description=mdef.description or "",
                    color=mdef.color,
                    avatar=getattr(mdef, "avatar", None),
                    model=mdef.model,
                    provider_profile=mdef.provider_profile,
                    skills=list(mdef.skills or []),
                )
            )
        else:
            members.append(TeamMemberSummary(name=mid, description="（专家已移除）"))
    return TeamResponse(
        name=defn.name,
        description=defn.description or "",
        system_prompt=defn.system_prompt or build_default_team_system_prompt(),
        model=defn.model,
        provider_profile=defn.provider_profile,
        effort=str(defn.effort) if getattr(defn, "effort", None) is not None else None,
        max_turns=getattr(defn, "max_turns", None),
        color=defn.color or "cyan",
        avatar=getattr(defn, "avatar", None),
        welcome_message=getattr(defn, "welcome_message", None) or DEFAULT_TEAM_WELCOME_MESSAGE,
        member_ids=member_ids,
        members=members,
        kind="team",
        source="user",
    )


@router.get("/template")
async def get_team_template(
    _user: Annotated[dict, Depends(require_agent_admin)],
) -> list[dict[str, str]]:
    """返回 Octop 默认团队主持人工作区模板文件预览。"""
    return [
        {"name": "SOUL.md", "content": TEAM_HOST_SOUL_MD.strip()},
        {"name": "AGENTS.md", "content": TEAM_HOST_AGENTS_MD.strip()},
    ]


@router.get("", response_model=list[TeamResponse])
async def list_teams(
    _user: Annotated[dict, Depends(require_agent_admin)],
) -> list[TeamResponse]:
    """列出所有专家团队（kind == 'team'）。"""
    all_defs = _all_definitions_map()
    teams: list[TeamResponse] = []
    for defn in all_defs.values():
        if getattr(defn, "kind", "expert") == "team":
            teams.append(_build_team_response(defn, all_defs))
    return teams


@router.get("/{name}", response_model=TeamResponse)
async def get_team(
    name: str,
    _user: Annotated[dict, Depends(require_agent_admin)],
) -> TeamResponse:
    """获取指定专家团队详情。"""
    all_defs = _all_definitions_map()
    defn = all_defs.get(name)
    if defn is None or getattr(defn, "kind", "expert") != "team":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"未找到团队 '{name}'")
    return _build_team_response(defn, all_defs)


@router.post("", response_model=TeamResponse, status_code=status.HTTP_201_CREATED)
async def create_team(
    body: CreateTeamRequest,
    _user: Annotated[dict, Depends(require_agent_admin)],
) -> TeamResponse:
    """创建一个新的专家团队（生成团队主持人配置文件并绑定成员列表）。"""
    name = body.name.strip()
    agent_file = agents_router._agent_file(name)
    all_defs = _all_definitions_map()
    if agent_file.exists() or name in all_defs:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"名称 '{name}' 已存在，请使用其他团队名称",
        )

    agents_router._validate_model_binding(body.provider_profile, body.model)
    member_ids = _validate_and_normalize_members(body.member_ids, all_defs, team_name=name)
    system_prompt = (
        body.system_prompt.strip()
        if isinstance(body.system_prompt, str) and body.system_prompt.strip()
        else build_default_team_system_prompt()
    )
    welcome_msg = (
        body.welcome_message.strip()
        if isinstance(body.welcome_message, str) and body.welcome_message.strip()
        else DEFAULT_TEAM_WELCOME_MESSAGE
    )

    frontmatter: dict[str, Any] = {
        "name": name,
        "description": body.description.strip() or f"由 {', '.join(member_ids)} 组成的专家协作团队",
        "source": "user",
        "kind": "team",
        "member_ids": member_ids,
        "welcome_message": welcome_msg,
        "tools": ["agent_list", "ask_agent"],
        "color": body.color or "cyan",
    }
    if body.provider_profile:
        frontmatter["provider_profile"] = body.provider_profile
    if body.model:
        frontmatter["model"] = body.model
    if body.effort:
        frontmatter["effort"] = body.effort
    if body.max_turns is not None:
        frontmatter["max_turns"] = body.max_turns
    if body.avatar:
        frontmatter["avatar"] = body.avatar

    content = f"---\n{yaml.dump(frontmatter, allow_unicode=True)}---\n\n{system_prompt}"
    agent_file.write_text(content, encoding="utf-8")

    updated_defs = _all_definitions_map()
    created_defn = updated_defs.get(name)
    if created_defn is None:
        raise HTTPException(status_code=500, detail="团队创建后读取失败")
    return _build_team_response(created_defn, updated_defs)


@router.put("/{name}", response_model=TeamResponse)
@router.patch("/{name}", response_model=TeamResponse)
async def update_team(
    name: str,
    body: UpdateTeamRequest,
    _user: Annotated[dict, Depends(require_agent_admin)],
) -> TeamResponse:
    """更新专家团队配置与成员编制。"""
    all_defs = _all_definitions_map()
    existing = all_defs.get(name)
    if existing is None or getattr(existing, "kind", "expert") != "team":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"未找到团队 '{name}'")

    agent_file = agents_router._agent_file(name)
    preserved: dict[str, Any] = {}
    if agent_file.exists():
        raw = agent_file.read_text(encoding="utf-8")
        if raw.startswith("---"):
            preserved = yaml.safe_load(raw.split("---", 2)[1]) or {}

    next_member_ids = (
        _validate_and_normalize_members(body.member_ids, all_defs, team_name=name)
        if body.member_ids is not None
        else list(getattr(existing, "member_ids", None) or [])
    )

    frontmatter: dict[str, Any] = {
        **preserved,
        "name": name,
        "description": (
            body.description.strip()
            if body.description is not None
            else existing.description
        ),
        "source": "user",
        "kind": "team",
        "member_ids": next_member_ids,
        "tools": ["agent_list", "ask_agent"],
    }

    for field_name, attr_name in [
        ("model", "model"),
        ("provider_profile", "provider_profile"),
        ("effort", "effort"),
        ("max_turns", "max_turns"),
        ("color", "color"),
        ("avatar", "avatar"),
        ("welcome_message", "welcome_message"),
    ]:
        if field_name in body.model_fields_set:
            val = getattr(body, field_name)
        else:
            val = getattr(existing, attr_name, None)
        if val is not None and str(val).strip() != "":
            frontmatter[field_name] = val
        else:
            frontmatter.pop(field_name, None)

    agents_router._validate_model_binding(frontmatter.get("provider_profile"), frontmatter.get("model"))
    new_system_prompt = (
        body.system_prompt.strip()
        if isinstance(body.system_prompt, str) and body.system_prompt.strip()
        else (existing.system_prompt or build_default_team_system_prompt())
    )

    content = f"---\n{yaml.dump(frontmatter, allow_unicode=True)}---\n\n{new_system_prompt}"
    agent_file.write_text(content, encoding="utf-8")

    updated_defs = _all_definitions_map()
    updated_defn = updated_defs.get(name)
    if updated_defn is None:
        raise HTTPException(status_code=500, detail="团队更新后读取失败")
    return _build_team_response(updated_defn, updated_defs)


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_team(
    name: str,
    _user: Annotated[dict, Depends(require_agent_admin)],
) -> None:
    """删除指定的专家团队。"""
    agent_file = agents_router._agent_file(name)
    if not agent_file.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"未找到团队 '{name}'")
    agent_file.unlink()
