"""Pydantic schemas for model/provider configuration API."""

from __future__ import annotations

from typing import Any
from pydantic import BaseModel


class ProviderProfileOut(BaseModel):
    """Provider profile summary returned by the API."""

    name: str
    label: str
    provider: str
    api_format: str
    auth_source: str
    default_model: str
    last_model: str | None = None
    base_url: str | None = None
    credential_slot: str | None = None
    is_builtin: bool = True
    # resolved model actually in use
    resolved_model: str = ""
    # auth status: "configured" | "missing" | ...
    auth_status: str = "unknown"
    # whether this profile is currently active
    is_active: bool = False


class ModelConfigOut(BaseModel):
    """Current model configuration summary."""

    active_profile: str
    model: str
    provider: str
    api_format: str
    base_url: str | None
    profiles: list[ProviderProfileOut]


class SwitchProfileIn(BaseModel):
    """Request body to switch the active provider profile."""

    profile: str


class SetModelIn(BaseModel):
    """Request body to set the model for the active profile."""

    model: str
    profile: str | None = None  # if None, applies to active profile


class CreateProfileIn(BaseModel):
    """Request body to create a custom provider profile."""

    name: str
    label: str
    provider: str = "openai"
    api_format: str = "openai"
    auth_source: str = "openai_api_key"
    default_model: str
    base_url: str | None = None
    credential_slot: str | None = None


class UpdateProfileIn(BaseModel):
    """Request body to update an existing profile."""

    label: str | None = None
    default_model: str | None = None
    last_model: str | None = None
    base_url: str | None = None


class SetApiKeyIn(BaseModel):
    """Request body to save an API key for a provider/profile."""

    api_key: str


class ApiKeyStatusOut(BaseModel):
    """API key status for a profile."""

    profile: str
    auth_source: str
    status: str  # "configured" | "missing"
    source: str = ""  # "env" | "file" | "missing"
