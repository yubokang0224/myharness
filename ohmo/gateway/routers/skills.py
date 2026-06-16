"""Skills and MCP router."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from ohmo.gateway.dependencies import get_current_user, get_runtime, _RuntimeState
from ohmo.gateway.schemas.skills import (
    AddMcpServerRequest,
    CreateSkillRequest,
    McpServerResponse,
    SkillResponse,
    UpdateSkillRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["skills"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _user_skills_dir() -> Path:
    from openharness.skills.loader import get_user_skills_dir

    return get_user_skills_dir()


def _skill_to_response(skill, disabled: set[str]) -> SkillResponse:
    return SkillResponse(
        name=skill.name,
        description=skill.description,
        content=skill.content,
        source=skill.source,
        path=skill.path,
        enabled=skill.name not in disabled,
    )


def _load_skill_registry_for_runtime(runtime: _RuntimeState):
    """Load the same skill universe that ohmo runtimes expose to agents."""
    from openharness.config.settings import load_settings
    from openharness.skills.loader import load_skill_registry
    from ohmo.workspace import get_plugins_dir, get_skills_dir

    workspace = getattr(runtime, "workspace", None)
    return load_skill_registry(
        Path.cwd(),
        extra_skill_dirs=(get_skills_dir(workspace),),
        extra_plugin_roots=(get_plugins_dir(workspace),),
        settings=load_settings(),
    )


async def _sync_mcp_manager_from_settings(runtime: _RuntimeState) -> None:
    """Keep gateway MCP manager aligned with the persisted settings file."""
    from openharness.config import load_settings
    from openharness.mcp.client import McpClientManager
    from openharness.mcp.config import load_mcp_server_configs

    server_configs = load_mcp_server_configs(load_settings(), [])
    if runtime.mcp_manager is None:
        runtime.mcp_manager = McpClientManager(server_configs)
        await runtime.mcp_manager.connect_all()
        return
    if hasattr(runtime.mcp_manager, "sync_server_configs"):
        await runtime.mcp_manager.sync_server_configs(server_configs)


def _mcp_status_to_response(status) -> McpServerResponse:
    transport = status.transport if status.transport in ("stdio", "http", "sse", "ws") else "stdio"
    return McpServerResponse(
        name=status.name,
        type=transport,
        state=status.state,
        detail=status.detail,
        transport=status.transport,
        tools=[
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in status.tools
        ],
    )


# ---------------------------------------------------------------------------
# Skill endpoints
# ---------------------------------------------------------------------------

@router.get("/skills", response_model=list[SkillResponse])
async def list_skills(
    _user: Annotated[dict, Depends(get_current_user)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
):
    """List all registered Agent-framework skills."""
    registry = _load_skill_registry_for_runtime(runtime)
    disabled = runtime.disabled_skills
    return [_skill_to_response(s, disabled) for s in registry.list_skills()]


@router.get("/skills/{name}", response_model=SkillResponse)
async def get_skill(
    name: str,
    _user: Annotated[dict, Depends(get_current_user)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
):
    registry = _load_skill_registry_for_runtime(runtime)
    skill = registry.get(name)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Skill '{name}' not found")
    return _skill_to_response(skill, runtime.disabled_skills)


@router.post("/skills/{name}/enable", status_code=status.HTTP_204_NO_CONTENT)
async def enable_skill(
    name: str,
    _user: Annotated[dict, Depends(get_current_user)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
):
    runtime.disabled_skills.discard(name)


@router.post("/skills/{name}/disable", status_code=status.HTTP_204_NO_CONTENT)
async def disable_skill(
    name: str,
    _user: Annotated[dict, Depends(get_current_user)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
):
    runtime.disabled_skills.add(name)


@router.post("/skills", response_model=SkillResponse, status_code=status.HTTP_201_CREATED)
async def create_skill(
    body: CreateSkillRequest,
    _user: Annotated[dict, Depends(get_current_user)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
):
    """Create a custom skill in the user skills directory."""
    skills_dir = _user_skills_dir()
    skill_dir = skills_dir / body.name
    skill_file = skill_dir / "SKILL.md"
    if skill_file.exists():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"Skill '{body.name}' already exists"
        )
    skill_dir.mkdir(parents=True, exist_ok=True)
    content = f"---\nname: {body.name}\ndescription: {body.description}\n---\n\n{body.content}"
    skill_file.write_text(content, encoding="utf-8")
    return SkillResponse(
        name=body.name,
        description=body.description,
        content=body.content,
        source="user",
        path=str(skill_file),
        enabled=True,
    )


@router.put("/skills/{name}", response_model=SkillResponse)
async def update_skill(
    name: str,
    body: UpdateSkillRequest,
    _user: Annotated[dict, Depends(get_current_user)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
):
    """Update a custom skill."""
    registry = _load_skill_registry_for_runtime(runtime)
    skill = registry.get(name)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Skill '{name}' not found")
    if skill.source != "user":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Only user-created skills can be modified"
        )
    if skill.path is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Skill has no file path")

    new_description = body.description if body.description is not None else skill.description
    new_content = body.content if body.content is not None else skill.content
    full_content = f"---\nname: {name}\ndescription: {new_description}\n---\n\n{new_content}"
    Path(skill.path).write_text(full_content, encoding="utf-8")
    return SkillResponse(
        name=name,
        description=new_description,
        content=new_content,
        source="user",
        path=skill.path,
        enabled=name not in runtime.disabled_skills,
    )


@router.delete("/skills/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_skill(
    name: str,
    _user: Annotated[dict, Depends(get_current_user)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
):
    """Delete a user-created custom skill."""
    registry = _load_skill_registry_for_runtime(runtime)
    skill = registry.get(name)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Skill '{name}' not found")
    if skill.source != "user":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Only user-created skills can be deleted"
        )
    if skill.path:
        skill_path = Path(skill.path)
        skill_path.unlink(missing_ok=True)
        # Remove parent dir if empty
        try:
            skill_path.parent.rmdir()
        except OSError:
            pass
    runtime.disabled_skills.discard(name)


# ---------------------------------------------------------------------------
# MCP endpoints
# ---------------------------------------------------------------------------

@router.get("/mcp/servers", response_model=list[McpServerResponse])
async def list_mcp_servers(
    _user: Annotated[dict, Depends(get_current_user)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
):
    """List all MCP servers and their connection status."""
    await _sync_mcp_manager_from_settings(runtime)
    if runtime.mcp_manager is None:
        return []
    try:
        if hasattr(runtime.mcp_manager, "get_all_statuses"):
            statuses = await runtime.mcp_manager.get_all_statuses()
        else:
            statuses = runtime.mcp_manager.list_statuses()
    except Exception:
        logger.exception("failed to list MCP server statuses")
        statuses = []
    return [_mcp_status_to_response(s) for s in statuses]


@router.post("/mcp/servers", response_model=McpServerResponse, status_code=status.HTTP_201_CREATED)
async def add_mcp_server(
    body: AddMcpServerRequest,
    _user: Annotated[dict, Depends(get_current_user)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
):
    """Add and connect a new MCP server."""
    from openharness.config import load_settings, save_settings
    from openharness.mcp.types import (
        McpHttpServerConfig,
        McpSseServerConfig,
        McpStdioServerConfig,
        McpWebSocketServerConfig,
    )

    cfg_type = str(body.config.get("type", "stdio"))
    if cfg_type == "http":
        config = McpHttpServerConfig(**body.config)
    elif cfg_type == "sse":
        config = McpSseServerConfig(**body.config)
    elif cfg_type == "ws":
        config = McpWebSocketServerConfig(**body.config)
    else:
        config = McpStdioServerConfig(**{k: v for k, v in body.config.items() if k != "type"})

    settings = load_settings()
    settings.mcp_servers[body.name] = config
    save_settings(settings)
    await _sync_mcp_manager_from_settings(runtime)

    if runtime.mcp_manager is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="MCP manager not initialized")

    for server_status in runtime.mcp_manager.list_statuses():
        if server_status.name == body.name:
            return _mcp_status_to_response(server_status)
    return McpServerResponse(name=body.name, type=cfg_type, state="pending", transport=cfg_type)


@router.delete("/mcp/servers/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_mcp_server(
    name: str,
    _user: Annotated[dict, Depends(get_current_user)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
):
    from openharness.config import load_settings, save_settings

    settings = load_settings()
    settings.mcp_servers.pop(name, None)
    save_settings(settings)
    await _sync_mcp_manager_from_settings(runtime)

    if runtime.mcp_manager is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="MCP manager not initialized")
    await runtime.mcp_manager.remove_server(name)


@router.post("/mcp/servers/{name}/connect", status_code=status.HTTP_204_NO_CONTENT)
async def connect_mcp_server(
    name: str,
    _user: Annotated[dict, Depends(get_current_user)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
):
    await _sync_mcp_manager_from_settings(runtime)
    if runtime.mcp_manager is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="MCP manager not initialized")
    await runtime.mcp_manager.connect(name)


@router.post("/mcp/servers/{name}/disconnect", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_mcp_server(
    name: str,
    _user: Annotated[dict, Depends(get_current_user)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
):
    await _sync_mcp_manager_from_settings(runtime)
    if runtime.mcp_manager is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="MCP manager not initialized")
    await runtime.mcp_manager.disconnect(name)


@router.get("/mcp/servers/{name}/tools")
async def get_mcp_server_tools(
    name: str,
    _user: Annotated[dict, Depends(get_current_user)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
):
    await _sync_mcp_manager_from_settings(runtime)
    if runtime.mcp_manager is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="MCP manager not initialized")
    try:
        tools = await runtime.mcp_manager.list_tools(server_name=name)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools]
