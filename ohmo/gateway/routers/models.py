"""Router for model/provider configuration management."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status

from ohmo.gateway.dependencies import get_current_user
from ohmo.gateway.schemas.models import (
    ApiKeyStatusOut,
    CreateProfileIn,
    ModelConfigOut,
    ProviderProfileOut,
    SetApiKeyIn,
    SetModelIn,
    SwitchProfileIn,
    UpdateProfileIn,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/models", tags=["models"])


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
        # subscription / oauth â€?simplified check
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


def _build_profile_out(
    settings, profile_name: str, is_active: bool = False
) -> ProviderProfileOut:
    """Build a ProviderProfileOut from settings + profile_name."""
    from openharness.config.settings import builtin_provider_profile_names

    profiles = settings.merged_profiles()
    profile = profiles.get(profile_name)
    if profile is None:
        raise KeyError(profile_name)

    auth_st, auth_src = _resolve_auth_status(settings, profile_name, profile)
    resolved = profile.resolved_model

    return ProviderProfileOut(
        name=profile_name,
        label=profile.label,
        provider=profile.provider,
        api_format=profile.api_format,
        auth_source=profile.auth_source,
        default_model=profile.default_model,
        last_model=profile.last_model,
        base_url=profile.base_url,
        credential_slot=profile.credential_slot,
        is_builtin=profile_name in builtin_provider_profile_names(),
        resolved_model=resolved,
        auth_status=auth_st,
        is_active=is_active,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/config", response_model=ModelConfigOut)
async def get_model_config(
    _user: Annotated[dict, Depends(get_current_user)],
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
        profiles=profile_outs,
    )


@router.put("/config/active-profile", response_model=ModelConfigOut)
async def switch_active_profile(
    body: SwitchProfileIn,
    _user: Annotated[dict, Depends(get_current_user)],
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
    _user: Annotated[dict, Depends(get_current_user)],
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
    updated = settings.model_copy(update={"profiles": profiles, "active_profile": profile_name})
    updated = updated.materialize_active_profile()
    save_settings(updated)
    return await get_model_config(_user)


@router.get("/profiles", response_model=list[ProviderProfileOut])
async def list_profiles(
    _user: Annotated[dict, Depends(get_current_user)],
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
    _user: Annotated[dict, Depends(get_current_user)],
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
        credential_slot=body.credential_slot,
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
    _user: Annotated[dict, Depends(get_current_user)],
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

    profiles[name] = profile.model_copy(update=patch)
    updated = settings.model_copy(update={"profiles": profiles})
    save_settings(updated)

    settings2 = load_settings()
    active_name, _ = settings2.resolve_profile()
    return _build_profile_out(settings2, name, is_active=(name == active_name))


@router.delete("/profiles/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile(
    name: str,
    _user: Annotated[dict, Depends(get_current_user)],
) -> None:
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
    _user: Annotated[dict, Depends(get_current_user)],
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
    _user: Annotated[dict, Depends(get_current_user)],
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
    _user: Annotated[dict, Depends(get_current_user)],
) -> None:
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
