"""Employee-safe workspace discovery. No prompts, credentials or admin settings."""

from typing import Annotated
from fastapi import APIRouter, Depends
from ohmo.gateway.dependencies import (
    AuthContext,
    get_workspace_auth_context,
    principal_from_user_claims,
)

router = APIRouter(prefix="/workbench", tags=["workbench"])
HIDDEN = {
    "statusline-setup",
    "claude-code-guide",
    "Explore",
    "Plan",
    "worker",
    "verification",
    "general-purpose",
}


@router.get("/capabilities")
async def capabilities(auth: Annotated[AuthContext, Depends(get_workspace_auth_context)]):
    p = auth.principal or principal_from_user_claims(auth.user)
    return {"can_manage": p.source == "os" and p.is_admin, "can_chat": True}


@router.get("/assistants")
async def assistants(auth: Annotated[AuthContext, Depends(get_workspace_auth_context)]):
    from ohmo.gateway.routers.agents import _agents_dir
    from openharness.coordinator.agent_definitions import (
        get_builtin_agent_definitions,
        load_agents_dir,
    )
    from openharness.config import load_settings

    definitions = {a.name: a for a in get_builtin_agent_definitions()}
    definitions.update({a.name: a for a in load_agents_dir(_agents_dir())})
    kb = load_settings().kb
    effective_ns = kb.effective_namespaces()
    result = []
    for a in definitions.values():
        if a.name in HIDDEN:
            continue
        access = kb.agents.get(a.name)
        if not kb.is_configured:
            enabled = False
            read_list: list[str] = []
        elif access is None:
            enabled = True
            read_list = list(effective_ns.keys())
        else:
            enabled = bool(access.enabled)
            read_list = list(access.read) if access.read else (list(effective_ns.keys()) if enabled else [])
        result.append(
            {
                "name": a.name,
                "description": a.description,
                "color": a.color,
                "avatar": getattr(a, "avatar", None),
                "kind": getattr(a, "kind", "expert") or "expert",
                "member_ids": list(getattr(a, "member_ids", None) or []),
                "welcome_message": getattr(a, "welcome_message", None),
                "knowledge_enabled": enabled,
                "knowledge": [
                    {"name": n, "label": effective_ns.get(n) or kb.namespaces.get(n) or n}
                    for n in read_list
                ],
            }
        )
    return result
