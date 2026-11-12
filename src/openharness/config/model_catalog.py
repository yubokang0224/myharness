"""Model catalog and request-scoped resolution shared by HTTP and IM runtimes."""

from __future__ import annotations
import hashlib
import json


def profile_models(profile):
    from openharness.config.settings import ModelSpec

    models = [m.model_copy(deep=True) for m in profile.models]
    ids = {m.id for m in models}
    for name in [profile.resolved_model, *profile.allowed_models]:
        if name and name not in ids:
            models.append(
                ModelSpec(
                    id=name,
                    name=name,
                    context_window_tokens=profile.context_window_tokens,
                    max_tokens=profile.max_tokens,
                    auto_compact_threshold_tokens=profile.auto_compact_threshold_tokens,
                    supports_vision=profile.supports_vision,
                )
            )
            ids.add(name)
    return models


def apply_model_parameters(settings, profile, model):
    spec = next((m for m in profile.models if m.id == model), None)
    if spec is None:
        return settings
    updates = {
        k: getattr(spec, k)
        for k in ("context_window_tokens", "max_tokens", "auto_compact_threshold_tokens")
        if getattr(spec, k) is not None
    }
    updates["model_reasoning_mode"] = spec.reasoning_mode
    updates["model_reasoning_effort"] = spec.reasoning_effort
    # Vision is read by existing engine paths from the effective provider profile.
    profiles = settings.merged_profiles()
    profiles[settings.active_profile] = profile.model_copy(
        update={
            "supports_vision": spec.supports_vision
            if spec.supports_vision is not None
            else profile.supports_vision
        }
    )
    updates["profiles"] = profiles
    return settings.model_copy(update=updates)


def resolve_agent_settings(settings, agent=None, *, default_profile=None, default_model=None):
    selected = (
        getattr(agent, "provider_profile", None) or default_profile or settings.active_profile
    )
    if selected not in settings.merged_profiles():
        raise ValueError("智能体绑定的模型连接不存在，请联系管理员重新配置")
    # Project the selected connection BEFORE model overrides. Never copy credentials or
    # a previous provider's URL into the newly selected profile.
    result = settings.model_copy(update={"active_profile": selected}).materialize_active_profile()
    model = getattr(agent, "model", None)
    if not model or model == "inherit":
        model = default_model if not getattr(agent, "provider_profile", None) else None
    profiles = result.merged_profiles()
    if model:
        profiles[selected] = profiles[selected].model_copy(update={"last_model": model})
        result = result.model_copy(update={"profiles": profiles}).materialize_active_profile()
    if agent:
        result = result.merge_cli_overrides(
            system_prompt=agent.system_prompt,
            permission_mode=agent.permission_mode,
            max_turns=agent.max_turns,
        )
        if agent.effort:
            scoped = result.merged_profiles()
            profile = scoped[result.active_profile]
            scoped[result.active_profile] = profile.model_copy(
                update={
                    "models": [
                        m.model_copy(update={"reasoning_effort": str(agent.effort)})
                        if m.id == result.model
                        else m
                        for m in profile.models
                    ]
                }
            )
            result = result.model_copy(update={"profiles": scoped})
            result = result.model_copy(
                update={"effort": str(agent.effort), "model_reasoning_effort": str(agent.effort)}
            )
    return result


def runtime_config_fingerprint(settings, agent=None):
    try:
        credential = hashlib.sha256(settings.resolve_auth().value.encode()).hexdigest()
    except (Exception, SystemExit):
        credential = "unavailable"
    payload = {
        "credential_revision": credential,
        "settings": settings.model_dump(mode="json"),
        "agent": agent.model_dump(mode="json") if agent else None,
    }
    # Stored only in memory. Do not log raw settings or credentials.
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
