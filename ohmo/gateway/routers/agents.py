"""Agent management router."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Annotated

import yaml
from fastapi import APIRouter, Depends, HTTPException, status

from ohmo.gateway.dependencies import require_agent_admin, get_runtime, _RuntimeState
from ohmo.gateway.schemas.agents import (
    AgentResponse,
    AgentToolInfo,
    CreateAgentRequest,
    RunningAgentInfo,
    UpdateAgentRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/agents", tags=["agents"])


def _agents_dir() -> Path:
    """Return (and create if needed) the user agents directory."""
    from openharness.config.paths import get_config_dir

    d = get_config_dir() / "agents"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _agent_file(name: str) -> Path:
    if not name.strip() or name in {".", ".."} or re.search(r'[<>:"/\\|?*\x00-\x1f]', name):
        raise HTTPException(status_code=400, detail="智能体名称不能包含路径或特殊字符")
    return _agents_dir() / f"{name}.md"


def _validate_model_binding(profile_name, model):
    if not profile_name:
        return
    from openharness.config import load_settings
    from openharness.config.model_catalog import profile_models

    profile = load_settings().merged_profiles().get(profile_name)
    if profile is None:
        raise HTTPException(status_code=400, detail="选择的模型连接不存在")
    if model and model != "inherit" and model not in {m.id for m in profile_models(profile)}:
        raise HTTPException(status_code=400, detail="请先在模型连接中添加该模型")


def _definition_to_response(defn) -> AgentResponse:
    return AgentResponse(
        name=defn.name,
        description=defn.description or "",
        system_prompt=defn.system_prompt or "",
        model=defn.model,
        provider_profile=defn.provider_profile,
        effort=str(defn.effort) if defn.effort is not None else None,
        permission_mode=defn.permission_mode,
        tools=defn.tools,
        disallowed_tools=defn.disallowed_tools or [],
        skills=defn.skills or [],
        mcp_servers=[
            s if isinstance(s, str) else (s.get("name", "") if isinstance(s, dict) else str(s))
            for s in (defn.mcp_servers or [])
        ],
        color=defn.color,
        avatar=getattr(defn, "avatar", None),
        max_turns=defn.max_turns,
        source=defn.source if defn.source in ("builtin", "user") else "user",
        kind=getattr(defn, "kind", "expert") or "expert",
        member_ids=list(getattr(defn, "member_ids", None) or []),
        welcome_message=getattr(defn, "welcome_message", None),
    )


@router.get("", response_model=list[AgentResponse])
async def list_agents(
    _user: Annotated[dict, Depends(require_agent_admin)],
):
    """List all agents (builtin + user-defined, user overrides builtin of same name)."""
    from openharness.coordinator.agent_definitions import (
        get_builtin_agent_definitions,
        load_agents_dir,
    )

    # Load user agents first to detect overrides
    user_agents_dir = _agents_dir()
    user_agents: dict[str, AgentResponse] = {}
    if user_agents_dir.exists():
        for defn in load_agents_dir(user_agents_dir):
            r = _definition_to_response(defn)
            user_agents[defn.name] = r

    result: list[AgentResponse] = []
    for defn in get_builtin_agent_definitions():
        if defn.name in user_agents:
            # User has an override for this builtin — mark it as overridden builtin
            override = user_agents.pop(defn.name)
            override.source = "user"  # type: ignore[assignment]
            result.append(override)
        else:
            result.append(_definition_to_response(defn))

    # Append remaining pure user-defined agents
    result.extend(user_agents.values())
    return result


@router.get("/running", response_model=list[RunningAgentInfo])
async def list_running_agents(
    _user: Annotated[dict, Depends(require_agent_admin)],
):
    """List currently running agent instances from TeamRegistry."""
    from openharness.coordinator import get_team_registry

    registry = get_team_registry()
    result = []
    for team in registry.list_teams():
        for agent_id in team.agents:
            result.append(
                RunningAgentInfo(
                    team_id=team.name,
                    agent_name=agent_id,
                    status="active",
                )
            )
    return result


@router.get("/tools", response_model=list[AgentToolInfo])
async def list_agent_tools(
    _user: Annotated[dict, Depends(require_agent_admin)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
):
    """List tools that can be selected in an agent definition."""
    from openharness.tools import create_default_tool_registry

    registry = create_default_tool_registry(runtime.mcp_manager)
    return sorted(
        [
            AgentToolInfo(
                name=tool.name,
                description=getattr(tool, "description", "") or "",
            )
            for tool in registry.list_tools()
        ],
        key=lambda tool: tool.name,
    )


@router.get("/{name}", response_model=AgentResponse)
async def get_agent(
    name: str,
    _user: Annotated[dict, Depends(require_agent_admin)],
):
    """Get a single agent definition by name. User override takes priority over builtin."""
    from openharness.coordinator.agent_definitions import (
        get_builtin_agent_definitions,
        load_agents_dir,
    )

    # Check user override first
    agent_file = _agent_file(name)
    if agent_file.exists():
        for defn in load_agents_dir(_agents_dir()):
            if defn.name == name:
                return _definition_to_response(defn)

    # Fall back to builtin
    for defn in get_builtin_agent_definitions():
        if defn.name == name:
            return _definition_to_response(defn)

    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Agent '{name}' not found")


@router.post("", response_model=AgentResponse, status_code=status.HTTP_201_CREATED)
async def create_agent(
    body: CreateAgentRequest,
    _user: Annotated[dict, Depends(require_agent_admin)],
):
    """Create a new user-defined agent (written to ~/.openharness/agents/)."""
    _validate_model_binding(body.provider_profile, body.model)
    agent_file = _agent_file(body.name)
    if agent_file.exists():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Agent '{body.name}' already exists",
        )

    frontmatter: dict = {
        "name": body.name,
        "description": body.description,
        "source": "user",
    }
    if body.provider_profile:
        frontmatter["provider_profile"] = body.provider_profile
    if body.model:
        frontmatter["model"] = body.model
    if body.effort:
        frontmatter["effort"] = body.effort
    if body.permission_mode:
        frontmatter["permission_mode"] = body.permission_mode
    if body.tools is not None:
        frontmatter["tools"] = body.tools
    if body.disallowed_tools:
        frontmatter["disallowed_tools"] = body.disallowed_tools
    if body.skills:
        frontmatter["skills"] = body.skills
    if body.mcp_servers:
        frontmatter["mcp_servers"] = body.mcp_servers
    if body.color:
        frontmatter["color"] = body.color
    if body.avatar:
        frontmatter["avatar"] = body.avatar
    if body.max_turns is not None:
        frontmatter["max_turns"] = body.max_turns
    if body.kind and body.kind != "expert":
        frontmatter["kind"] = body.kind
    if body.member_ids:
        frontmatter["member_ids"] = body.member_ids
    if body.welcome_message:
        frontmatter["welcome_message"] = body.welcome_message

    effective_prompt = body.system_prompt or ""
    if body.kind == "team":
        if "tools" not in frontmatter or not frontmatter["tools"]:
            frontmatter["tools"] = ["agent_list", "ask_agent"]
        if not effective_prompt.strip():
            from ohmo.gateway.routers.teams import build_default_team_system_prompt

            effective_prompt = build_default_team_system_prompt()

    content = f"---\n{yaml.dump(frontmatter, allow_unicode=True)}---\n\n{effective_prompt}"
    agent_file.write_text(content, encoding="utf-8")

    return AgentResponse(
        name=body.name,
        description=body.description,
        system_prompt=effective_prompt,
        model=body.model,
        provider_profile=body.provider_profile,
        effort=body.effort,
        permission_mode=body.permission_mode,
        tools=frontmatter.get("tools"),
        disallowed_tools=body.disallowed_tools,
        skills=body.skills,
        mcp_servers=body.mcp_servers,
        color=body.color,
        avatar=body.avatar,
        max_turns=body.max_turns,
        source="user",
        kind=body.kind or "expert",
        member_ids=body.member_ids or [],
        welcome_message=body.welcome_message,
    )


@router.put("/{name}", response_model=AgentResponse)
async def update_agent(
    name: str,
    body: UpdateAgentRequest,
    _user: Annotated[dict, Depends(require_agent_admin)],
):
    """Update a user-defined agent or create/update an override for a builtin agent."""
    from openharness.coordinator.agent_definitions import (
        get_builtin_agent_definitions,
        load_agents_dir,
    )

    agent_file = _agent_file(name)

    # Load existing definition: prefer user file, fall back to builtin
    if agent_file.exists():
        existing_list = load_agents_dir(_agents_dir())
        existing = next((d for d in existing_list if d.name == name), None)
    else:
        existing = next((d for d in get_builtin_agent_definitions() if d.name == name), None)

    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Agent '{name}' not found"
        )

    preserved = {}
    if agent_file.exists():
        raw = agent_file.read_text(encoding="utf-8")
        if raw.startswith("---"):
            preserved = yaml.safe_load(raw.split("---", 2)[1]) or {}
    frontmatter: dict = {
        **preserved,
        "name": name,
        "description": body.description if body.description is not None else existing.description,
        "source": "user",
    }
    new_system_prompt = existing.system_prompt or ""
    if body.system_prompt is not None:
        new_system_prompt = body.system_prompt

    for field, attr in [
        ("model", "model"),
        ("provider_profile", "provider_profile"),
        ("effort", "effort"),
        ("permission_mode", "permission_mode"),
        ("tools", "tools"),
        ("disallowed_tools", "disallowed_tools"),
        ("skills", "skills"),
        ("mcp_servers", "mcp_servers"),
        ("color", "color"),
        ("avatar", "avatar"),
        ("max_turns", "max_turns"),
        ("kind", "kind"),
        ("member_ids", "member_ids"),
        ("welcome_message", "welcome_message"),
    ]:
        if field in body.model_fields_set:
            val = getattr(body, field)
        else:
            val = getattr(existing, attr, None)
        if val is not None:
            frontmatter[field] = val
        else:
            frontmatter.pop(field, None)

    _validate_model_binding(frontmatter.get("provider_profile"), frontmatter.get("model"))
    if frontmatter.get("kind") == "team":
        if not frontmatter.get("tools"):
            frontmatter["tools"] = ["agent_list", "ask_agent"]
        if not new_system_prompt.strip():
            from ohmo.gateway.routers.teams import build_default_team_system_prompt

            new_system_prompt = build_default_team_system_prompt()
    content = f"---\n{yaml.dump(frontmatter, allow_unicode=True)}---\n\n{new_system_prompt}"
    agent_file.write_text(content, encoding="utf-8")

    return AgentResponse(
        name=name,
        description=frontmatter.get("description", ""),
        system_prompt=new_system_prompt,
        model=frontmatter.get("model"),
        provider_profile=frontmatter.get("provider_profile"),
        effort=str(frontmatter["effort"]) if frontmatter.get("effort") is not None else None,
        permission_mode=frontmatter.get("permission_mode"),
        tools=frontmatter.get("tools"),
        disallowed_tools=frontmatter.get("disallowed_tools", []),
        skills=frontmatter.get("skills", []),
        mcp_servers=frontmatter.get("mcp_servers", []),
        color=frontmatter.get("color"),
        avatar=frontmatter.get("avatar"),
        max_turns=frontmatter.get("max_turns"),
        source="user",
        kind="team" if str(frontmatter.get("kind") or "").lower() == "team" else "expert",
        member_ids=list(frontmatter.get("member_ids") or []),
        welcome_message=frontmatter.get("welcome_message"),
    )


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    name: str,
    _user: Annotated[dict, Depends(require_agent_admin)],
):
    """Delete a user-defined agent or reset a builtin override (restores original)."""
    from openharness.coordinator.agent_definitions import get_builtin_agent_definitions

    agent_file = _agent_file(name)
    if not agent_file.exists():
        # Check if it is a builtin (cannot delete)
        is_builtin = any(d.name == name for d in get_builtin_agent_definitions())
        if is_builtin:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"内置智能体 '{name}' 没有自定义覆盖，无需重置",
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent '{name}' not found",
        )
    agent_file.unlink()
