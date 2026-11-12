"""Router for model/provider configuration management."""

from __future__ import annotations

import logging
import asyncio
import time
import httpx
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status

from ohmo.gateway.dependencies import require_agent_admin
from ohmo.gateway.schemas.models import (
    ApiKeyStatusOut,
    CreateProfileIn,
    ModelConfigOut,
    ProviderProfileOut,
    SetApiKeyIn,
    SetModelIn,
    SwitchProfileIn,
    UpdateProfileIn,
    UpdateVisionConfigIn,
    VisionConfigOut,
    TestModelIn,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/models", tags=["models"])


def _positive_int_or_none(value: int | None) -> int | None:
    if value is None:
        return None
    return value if value > 0 else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_auth_status(settings, profile_name: str, profile) -> tuple[str, str]:
    """Return (status, source) for a profile's auth state."""
    import os
    from openharness.config.settings import (
        auth_source_uses_api_key,
        auth_source_provider_name,
    )
    from openharness.auth.storage import load_credential

    auth_source = profile.auth_source
    if not auth_source_uses_api_key(auth_source):
        # subscription / oauth 鈥?simplified check
        if auth_source in {"codex_subscription", "claude_subscription"}:
            from openharness.auth.storage import load_external_binding

            binding = load_external_binding(auth_source_provider_name(auth_source))
            if binding is not None:
                return "configured", "external"
            return "missing", "missing"
        if auth_source == "copilot_oauth":
            try:
                from openharness.api.copilot_auth import load_copilot_auth

                if load_copilot_auth():
                    return "configured", "file"
            except Exception:
                pass
            return "missing", "missing"
        return "unknown", "unknown"

    # API-key auth sources
    env_map = {
        "anthropic_api_key": "ANTHROPIC_API_KEY",
        "openai_api_key": "OPENAI_API_KEY",
        "dashscope_api_key": "DASHSCOPE_API_KEY",
        "moonshot_api_key": "MOONSHOT_API_KEY",
        "gemini_api_key": "GEMINI_API_KEY",
        "minimax_api_key": "MINIMAX_API_KEY",
        "modelscope_api_key": "MODELSCOPE_API_KEY",
    }
    env_var = env_map.get(auth_source)
    if env_var and os.environ.get(env_var):
        return "configured", f"env:{env_var}"

    # Check credential slot first
    if profile.credential_slot:
        scoped = load_credential(f"profile:{profile.credential_slot}", "api_key", use_keyring=False)
        if scoped:
            return "configured", "file"

    storage_provider = auth_source_provider_name(auth_source)
    stored = load_credential(storage_provider, "api_key", use_keyring=False)
    if stored:
        return "configured", "file"

    return "missing", "missing"


def _build_profile_out(settings, profile_name: str, is_active: bool = False) -> ProviderProfileOut:
    """Build a ProviderProfileOut from settings + profile_name."""
    from openharness.config.settings import builtin_provider_profile_names

    profiles = settings.merged_profiles()
    profile = profiles.get(profile_name)
    if profile is None:
        raise KeyError(profile_name)

    auth_st, auth_src = _resolve_auth_status(settings, profile_name, profile)
    resolved = profile.resolved_model
    from openharness.config.model_catalog import profile_models

    return ProviderProfileOut(
        models=profile_models(profile),
        name=profile_name,
        label=profile.label,
        provider=profile.provider,
        api_format=profile.api_format,
        auth_source=profile.auth_source,
        default_model=profile.default_model,
        last_model=profile.last_model,
        base_url=profile.base_url,
        max_tokens=profile.max_tokens,
        context_window_tokens=profile.context_window_tokens,
        auto_compact_threshold_tokens=profile.auto_compact_threshold_tokens,
        supports_vision=profile.supports_vision,
        credential_slot=profile.credential_slot,
        is_builtin=profile_name in builtin_provider_profile_names(),
        resolved_model=resolved,
        auth_status=auth_st,
        is_active=is_active,
    )


def _build_vision_out(settings) -> VisionConfigOut:
    """Return current vision config summary without exposing secrets."""
    from openharness.config.settings import VisionModelConfig

    if settings.vision.is_configured:
        return VisionConfigOut(
            model=settings.vision.model,
            base_url=settings.vision.base_url,
            configured=True,
            source="settings",
        )

    env_cfg = VisionModelConfig.from_env()
    if env_cfg.is_configured:
        return VisionConfigOut(
            model=env_cfg.model,
            base_url=env_cfg.base_url,
            configured=True,
            source="env",
        )

    return VisionConfigOut(
        model=settings.vision.model,
        base_url=settings.vision.base_url,
        configured=False,
        source="missing",
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/config", response_model=ModelConfigOut)
async def get_model_config(
    _user: Annotated[dict, Depends(require_agent_admin)],
) -> ModelConfigOut:
    """Return current model configuration and all provider profiles."""
    from openharness.config import load_settings

    settings = load_settings()
    active_name, _ = settings.resolve_profile()
    all_profiles = settings.merged_profiles()

    profile_outs = []
    for name in all_profiles:
        try:
            profile_outs.append(_build_profile_out(settings, name, is_active=(name == active_name)))
        except Exception as exc:
            logger.warning("Failed to build profile %s: %s", name, exc)

    return ModelConfigOut(
        active_profile=active_name,
        model=settings.model,
        provider=settings.provider,
        api_format=settings.api_format,
        base_url=settings.base_url,
        max_tokens=settings.max_tokens,
        context_window_tokens=settings.context_window_tokens,
        auto_compact_threshold_tokens=settings.auto_compact_threshold_tokens,
        profiles=profile_outs,
        vision=_build_vision_out(settings),
    )


@router.put("/config/vision", response_model=ModelConfigOut)
async def update_vision_config(
    body: UpdateVisionConfigIn,
    _user: Annotated[dict, Depends(require_agent_admin)],
) -> ModelConfigOut:
    """Update vision model settings used by image_to_text."""
    from openharness.config import load_settings, save_settings
    from openharness.config.settings import VisionModelConfig

    settings = load_settings()
    current = settings.vision
    model = current.model if body.model is None else body.model.strip()
    base_url = current.base_url if body.base_url is None else body.base_url.strip()
    if body.clear_api_key:
        api_key = ""
    elif body.api_key is None:
        api_key = current.api_key
    else:
        api_key = body.api_key.strip()

    if (model and not api_key) or (api_key and not model):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="vision.model and vision.api_key must be configured together",
        )

    updated = settings.model_copy(
        update={
            "vision": VisionModelConfig(
                model=model,
                api_key=api_key,
                base_url=base_url,
            )
        }
    )
    save_settings(updated)
    return await get_model_config(_user)


@router.put("/config/active-profile", response_model=ModelConfigOut)
async def switch_active_profile(
    body: SwitchProfileIn,
    _user: Annotated[dict, Depends(require_agent_admin)],
) -> ModelConfigOut:
    """Switch the active provider profile and persist the change."""
    from openharness.config import load_settings, save_settings

    settings = load_settings()
    profiles = settings.merged_profiles()
    if body.profile not in profiles:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile '{body.profile}' not found",
        )

    updated = settings.model_copy(update={"active_profile": body.profile})
    updated = updated.materialize_active_profile()
    save_settings(updated)
    return await get_model_config(_user)


@router.patch("/config/model", response_model=ModelConfigOut)
async def set_model(
    body: SetModelIn,
    _user: Annotated[dict, Depends(require_agent_admin)],
) -> ModelConfigOut:
    """Update the model for a profile (defaults to active profile)."""
    from openharness.config import load_settings, save_settings

    settings = load_settings()
    profile_name = body.profile or settings.active_profile
    profiles = settings.merged_profiles()
    if profile_name not in profiles:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile '{profile_name}' not found",
        )

    profile = profiles[profile_name]
    profiles[profile_name] = profile.model_copy(update={"last_model": body.model})
    updated = settings.model_copy(update={"profiles": profiles})
    updated = updated.materialize_active_profile()
    save_settings(updated)
    return await get_model_config(_user)


@router.get("/profiles", response_model=list[ProviderProfileOut])
async def list_profiles(
    _user: Annotated[dict, Depends(require_agent_admin)],
) -> list[ProviderProfileOut]:
    """List all provider profiles with their auth status."""
    from openharness.config import load_settings

    settings = load_settings()
    active_name, _ = settings.resolve_profile()
    all_profiles = settings.merged_profiles()

    result = []
    for name in all_profiles:
        try:
            result.append(_build_profile_out(settings, name, is_active=(name == active_name)))
        except Exception as exc:
            logger.warning("Failed to build profile %s: %s", name, exc)
    return result


@router.post("/profiles", response_model=ProviderProfileOut, status_code=status.HTTP_201_CREATED)
async def create_profile(
    body: CreateProfileIn,
    _user: Annotated[dict, Depends(require_agent_admin)],
) -> ProviderProfileOut:
    """Create a custom provider profile."""
    from openharness.config import load_settings, save_settings
    from openharness.config.settings import ProviderProfile

    settings = load_settings()
    profiles = settings.merged_profiles()
    if body.name in profiles:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Profile '{body.name}' already exists",
        )

    new_profile = ProviderProfile(
        label=body.label,
        provider=body.provider,
        api_format=body.api_format,
        auth_source=body.auth_source,
        default_model=body.default_model,
        base_url=body.base_url,
        max_tokens=_positive_int_or_none(body.max_tokens),
        context_window_tokens=_positive_int_or_none(body.context_window_tokens),
        auto_compact_threshold_tokens=_positive_int_or_none(body.auto_compact_threshold_tokens),
        supports_vision=body.supports_vision,
        credential_slot=body.credential_slot or body.name,
        models=body.models,
    )
    profiles[body.name] = new_profile
    updated = settings.model_copy(update={"profiles": profiles})
    save_settings(updated)

    settings2 = load_settings()
    return _build_profile_out(settings2, body.name, is_active=False)


@router.put("/profiles/{name}", response_model=ProviderProfileOut)
async def update_profile(
    name: str,
    body: UpdateProfileIn,
    _user: Annotated[dict, Depends(require_agent_admin)],
) -> ProviderProfileOut:
    """Update label, model, or base_url of an existing profile."""
    from openharness.config import load_settings, save_settings

    settings = load_settings()
    profiles = settings.merged_profiles()
    if name not in profiles:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile '{name}' not found",
        )

    profile = profiles[name]
    patch: dict = {}
    if body.label is not None:
        patch["label"] = body.label
    if body.default_model is not None:
        patch["default_model"] = body.default_model
    if body.last_model is not None:
        patch["last_model"] = body.last_model
    if body.base_url is not None:
        patch["base_url"] = body.base_url
    if "max_tokens" in body.model_fields_set:
        patch["max_tokens"] = _positive_int_or_none(body.max_tokens)
    if "context_window_tokens" in body.model_fields_set:
        patch["context_window_tokens"] = _positive_int_or_none(body.context_window_tokens)
    if "auto_compact_threshold_tokens" in body.model_fields_set:
        patch["auto_compact_threshold_tokens"] = _positive_int_or_none(
            body.auto_compact_threshold_tokens
        )
    if "supports_vision" in body.model_fields_set:
        patch["supports_vision"] = body.supports_vision

    if body.models is not None:
        removed = {m.id for m in profile.models} - {m.id for m in body.models}
        from openharness.coordinator.agent_definitions import load_agents_dir
        from ohmo.gateway.routers.agents import _agents_dir

        if profile.resolved_model in removed or any(
            a.provider_profile == name and a.model in removed
            for a in load_agents_dir(_agents_dir())
        ):
            raise HTTPException(
                status_code=409, detail="模型仍被默认配置或智能体使用，请先更换绑定"
            )
        patch["models"] = body.models
    from openharness.config.settings import ProviderProfile

    try:
        profiles[name] = ProviderProfile.model_validate({**profile.model_dump(), **patch})
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="模型参数不合法，请检查重复 ID、上下文和输出上限"
        ) from exc
    updated = settings.model_copy(update={"profiles": profiles})
    if name == settings.active_profile:
        updated = updated.materialize_active_profile()
    save_settings(updated)

    settings2 = load_settings()
    active_name, _ = settings2.resolve_profile()
    return _build_profile_out(settings2, name, is_active=(name == active_name))


@router.delete("/profiles/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile(
    name: str,
    _user: Annotated[dict, Depends(require_agent_admin)],
):
    """Delete a custom (non-builtin) provider profile."""
    from openharness.config import load_settings, save_settings
    from openharness.config.settings import builtin_provider_profile_names

    if name in builtin_provider_profile_names():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete a built-in provider profile",
        )

    settings = load_settings()
    profiles = settings.merged_profiles()
    if name not in profiles:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile '{name}' not found",
        )

    from openharness.coordinator.agent_definitions import load_agents_dir
    from ohmo.gateway.routers.agents import _agents_dir

    if settings.active_profile == name or any(
        a.provider_profile == name for a in load_agents_dir(_agents_dir())
    ):
        raise HTTPException(status_code=409, detail="连接仍被默认配置或智能体使用，请先更换绑定")
    del profiles[name]
    # If we deleted the active profile, fall back to claude-api
    active = settings.active_profile
    if active == name:
        active = "claude-api"
    updated = settings.model_copy(update={"profiles": profiles, "active_profile": active})
    save_settings(updated)


@router.get("/profiles/{name}/auth-status", response_model=ApiKeyStatusOut)
async def get_profile_auth_status(
    name: str,
    _user: Annotated[dict, Depends(require_agent_admin)],
) -> ApiKeyStatusOut:
    """Get auth/API-key status for a specific profile."""
    from openharness.config import load_settings

    settings = load_settings()
    profiles = settings.merged_profiles()
    if name not in profiles:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile '{name}' not found",
        )

    profile = profiles[name]
    auth_st, auth_src = _resolve_auth_status(settings, name, profile)
    return ApiKeyStatusOut(
        profile=name,
        auth_source=profile.auth_source,
        status=auth_st,
        source=auth_src,
    )


@router.post("/profiles/{name}/api-key", response_model=ApiKeyStatusOut)
async def set_profile_api_key(
    name: str,
    body: SetApiKeyIn,
    _user: Annotated[dict, Depends(require_agent_admin)],
) -> ApiKeyStatusOut:
    """Save an API key for a profile. Stored in ~/.openharness/credentials.json."""
    from openharness.config import load_settings
    from openharness.config.settings import (
        auth_source_uses_api_key,
        auth_source_provider_name,
        credential_storage_provider_name,
    )
    from openharness.auth.storage import store_credential

    if not body.api_key or not body.api_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="api_key must not be empty",
        )

    settings = load_settings()
    profiles = settings.merged_profiles()
    if name not in profiles:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile '{name}' not found",
        )

    profile = profiles[name]
    if not auth_source_uses_api_key(profile.auth_source):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Profile '{name}' does not use API-key authentication (auth_source={profile.auth_source})",
        )

    storage_provider = credential_storage_provider_name(name, profile)
    store_credential(storage_provider, "api_key", body.api_key.strip(), use_keyring=False)

    auth_st, auth_src = _resolve_auth_status(settings, name, profile)
    return ApiKeyStatusOut(
        profile=name,
        auth_source=profile.auth_source,
        status=auth_st,
        source=auth_src,
    )


@router.delete("/profiles/{name}/api-key", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile_api_key(
    name: str,
    _user: Annotated[dict, Depends(require_agent_admin)],
):
    """Remove stored API key for a profile."""
    from openharness.config import load_settings
    from openharness.config.settings import (
        auth_source_uses_api_key,
        credential_storage_provider_name,
    )
    from openharness.auth.storage import clear_provider_credentials

    settings = load_settings()
    profiles = settings.merged_profiles()
    if name not in profiles:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile '{name}' not found",
        )

    profile = profiles[name]
    if not auth_source_uses_api_key(profile.auth_source):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Profile '{name}' does not use API-key authentication",
        )

    storage_provider = credential_storage_provider_name(name, profile)
    clear_provider_credentials(storage_provider)


def _connection_settings(name, model=None):
    from openharness.config import load_settings
    from openharness.config.model_catalog import resolve_agent_settings

    settings = load_settings()
    if name not in settings.merged_profiles():
        raise HTTPException(status_code=404, detail="模型连接不存在")
    return resolve_agent_settings(settings, default_profile=name, default_model=model)


@router.post("/profiles/{name}/test")
async def test_model_connection(
    name: str, body: TestModelIn, _user: Annotated[dict, Depends(require_agent_admin)]
):
    settings = _connection_settings(name, body.model)
    started = time.monotonic()
    client = None
    try:
        from openharness.ui.runtime import _resolve_api_client_from_settings
        from openharness.api.client import ApiMessageRequest, ApiMessageCompleteEvent
        from openharness.engine.messages import ConversationMessage, TextBlock

        client = _resolve_api_client_from_settings(settings)
        request = ApiMessageRequest(
            model=settings.model,
            max_tokens=256,
            messages=[ConversationMessage(role="user", content=[TextBlock(text="Reply only OK.")])],
        )
        async with asyncio.timeout(25):
            async for event in client.stream_message(request):
                if isinstance(event, ApiMessageCompleteEvent):
                    return {
                        "ok": True,
                        "latency_ms": round((time.monotonic() - started) * 1000),
                        "checked_at": time.time(),
                        "model": settings.model,
                    }
        raise ValueError("empty model response")
    except (Exception, SystemExit) as exc:
        code = getattr(exc, "status_code", None)
        message = (
            "凭据无效或无模型访问权限"
            if code in (401, 403)
            else "调用频率或额度受限"
            if code == 429
            else "连接超时，请检查接口地址和网络"
            if isinstance(exc, (TimeoutError, httpx.TimeoutException))
            else "连接测试失败，请检查凭据、模型名称和接口地址"
        )
        return {
            "ok": False,
            "error": message,
            "checked_at": time.time(),
            "latency_ms": round((time.monotonic() - started) * 1000),
        }
    finally:
        underlying = getattr(client, "_client", None)
        if underlying and hasattr(underlying, "close"):
            await underlying.close()


@router.post("/profiles/{name}/discover")
async def discover_models(name: str, _user: Annotated[dict, Depends(require_agent_admin)]):
    settings = _connection_settings(name)
    if settings.api_format != "openai":
        raise HTTPException(status_code=400, detail="该协议暂不支持列举模型，请手动添加")
    try:
        from openharness.api.openai_client import _normalize_openai_base_url

        base = _normalize_openai_base_url(settings.base_url) or "https://api.openai.com/v1"
        key = settings.resolve_auth().value
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            response = await client.get(
                base.rstrip("/") + "/models", headers={"Authorization": "Bearer " + key}
            )
            response.raise_for_status()
            data = response.json()
        models = sorted(
            {str(m["id"]) for m in data.get("data", []) if isinstance(m, dict) and m.get("id")}
        )
        return {"models": models[:1000]}
    except Exception as exc:
        raise HTTPException(status_code=502, detail="无法获取模型列表，仍可手动添加模型") from exc
