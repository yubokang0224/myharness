"""Config IO for ohmo gateway."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any

from openharness.auth.storage import load_credential, store_credential
from openharness.config.schema import Config
from openharness.utils.fs import atomic_write_text

from ohmo.gateway.models import GatewayConfig, MesWorkspaceConfig
from ohmo.workspace import get_gateway_config_path, get_workspace_root


_MES_CREDENTIAL_KEY = "jwt_secret"
_MES_ENV_NAMES = (
    "OHMO_JWT_MES_SECRET_KEY",
    "OHMO_JWT_MES_ISSUER",
    "OHMO_JWT_MES_AUDIENCE",
    "OHMO_JWT_MES_ALGORITHM",
    "OHMO_MES_REQUIRED_PERMISSION",
    "OHMO_MES_PERMISSION_URL",
    "OHMO_MES_API_BASE_URL",
)


@dataclass(frozen=True)
class ResolvedMesWorkspaceConfig:
    """Runtime MES settings after persistent config and env overrides merge."""

    enabled: bool
    secret_key: str
    issuer: str
    audience: str
    algorithm: str
    required_permission: str
    api_base_url: str
    permission_url: str
    allow_anonymous_api: bool

    @property
    def permission_endpoint(self) -> str:
        if self.permission_url:
            return self.permission_url
        if self.api_base_url:
            return f"{self.api_base_url.rstrip('/')}/SysUser/GetPermissions"
        return ""


def load_gateway_config(workspace: str | Path | None = None) -> GatewayConfig:
    """Load ``.ohmo/gateway.json``."""
    path = get_gateway_config_path(workspace)
    if path.exists():
        return GatewayConfig.model_validate_json(path.read_text(encoding="utf-8"))
    return GatewayConfig()


def save_gateway_config(config: GatewayConfig, workspace: str | Path | None = None) -> Path:
    """Persist ``.ohmo/gateway.json`` atomically with owner-only POSIX mode."""
    path = get_gateway_config_path(workspace)
    atomic_write_text(path, config.model_dump_json(indent=2) + "\n", mode=0o600)
    return path


def _env_text(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _env_bool(name: str) -> bool | None:
    value = _env_text(name)
    if value is None:
        return None
    normalized = value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _mes_credential_provider(workspace: str | Path | None) -> str:
    root = str(get_workspace_root(workspace)).casefold().encode("utf-8")
    digest = hashlib.sha256(root).hexdigest()[:16]
    return f"ohmo_mes_{digest}"


def resolve_mes_workspace_config(
    workspace: str | Path | None = None,
) -> ResolvedMesWorkspaceConfig:
    """Resolve MES configuration.

    Existing ``OHMO_*`` variables remain supported as optional deployment
    overrides.  Normal local operation reads non-secret values from
    ``gateway.json`` and the JWT secret from the credential store.
    """

    persisted = load_gateway_config(workspace).mes
    mes_env_present = any(_env_text(name) is not None for name in _MES_ENV_NAMES)
    mes_configured = bool(persisted.enabled or mes_env_present)
    enabled_override = _env_bool("OHMO_MES_ENABLED")
    enabled = (
        enabled_override
        if enabled_override is not None
        else mes_configured
    )

    secret_key = _env_text("OHMO_JWT_MES_SECRET_KEY") or ""
    if enabled and not secret_key:
        secret_key = load_credential(
            _mes_credential_provider(workspace),
            _MES_CREDENTIAL_KEY,
        ) or ""

    allow_anonymous_override = _env_bool("OHMO_ALLOW_ANONYMOUS_API")
    if allow_anonymous_override is not None:
        allow_anonymous = allow_anonymous_override
    elif mes_configured:
        # Disabling MES authentication must never reopen the legacy anonymous
        # API on a process that has already been configured for MES.
        allow_anonymous = bool(persisted.allow_anonymous_api)
    else:
        # Preserve the historical OS-only behavior until MES is enabled.
        allow_anonymous = True

    return ResolvedMesWorkspaceConfig(
        enabled=enabled,
        secret_key=secret_key,
        issuer=_env_text("OHMO_JWT_MES_ISSUER") or persisted.issuer.strip(),
        audience=_env_text("OHMO_JWT_MES_AUDIENCE") or persisted.audience.strip(),
        algorithm=(
            _env_text("OHMO_JWT_MES_ALGORITHM")
            or persisted.algorithm.strip()
            or "HS256"
        ),
        required_permission=(
            _env_text("OHMO_MES_REQUIRED_PERMISSION")
            or persisted.required_permission.strip()
        ),
        api_base_url=(
            _env_text("OHMO_MES_API_BASE_URL") or persisted.api_base_url.strip()
        ).rstrip("/"),
        permission_url=(
            _env_text("OHMO_MES_PERMISSION_URL") or persisted.permission_url.strip()
        ),
        allow_anonymous_api=allow_anonymous,
    )


def validate_mes_workspace_config(
    workspace: str | Path | None = None,
) -> ResolvedMesWorkspaceConfig:
    """Fail startup when an explicitly enabled MES profile is incomplete."""

    config = resolve_mes_workspace_config(workspace)
    if not config.enabled:
        return config

    missing: list[str] = []
    if not config.secret_key:
        missing.append("JWT secret")
    if not config.issuer:
        missing.append("JWT issuer")
    if not config.audience:
        missing.append("JWT audience")
    if config.required_permission and not config.permission_endpoint:
        missing.append("MES permission endpoint")
    if missing:
        raise ValueError(f"MES workspace configuration is incomplete: {', '.join(missing)}")
    return config


def _strip_json_comments(raw: str) -> str:
    """Remove JavaScript-style comments while preserving quoted URLs."""

    result: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(raw):
        char = raw[index]
        next_char = raw[index + 1] if index + 1 < len(raw) else ""
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            result.append(char)
            index += 1
            continue
        if char == "/" and next_char == "/":
            index += 2
            while index < len(raw) and raw[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and next_char == "*":
            index += 2
            while index + 1 < len(raw) and raw[index : index + 2] != "*/":
                if raw[index] in "\r\n":
                    result.append(raw[index])
                index += 1
            index = min(index + 2, len(raw))
            continue
        result.append(char)
        index += 1
    return "".join(result)


def _read_mes_jwt_settings(appsettings_path: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
    path = Path(appsettings_path).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"MES appsettings file does not exist: {path}")
    try:
        document = json.loads(_strip_json_comments(path.read_text(encoding="utf-8-sig")))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read MES appsettings file: {path}") from exc
    if not isinstance(document, dict):
        raise ValueError("MES appsettings root must be an object")
    web_config = document.get("WebConfig")
    jwt_config = web_config.get("JWTConfig") if isinstance(web_config, dict) else None
    if not isinstance(jwt_config, dict):
        jwt_config = document.get("JWTConfig")
    if not isinstance(jwt_config, dict):
        raise ValueError("MES appsettings does not contain WebConfig.JWTConfig")
    return document, jwt_config


def _infer_api_base_url(document: dict[str, Any]) -> str:
    listen_url = document.get("urls") or document.get("Urls") or ""
    if not isinstance(listen_url, str):
        return ""
    first_url = listen_url.split(";", 1)[0].strip()
    return re.sub(r"(?<=://)(?:\*|0\.0\.0\.0|\[::\])(?=:)", "localhost", first_url)


def configure_mes_workspace_from_appsettings(
    appsettings_path: str | Path,
    *,
    workspace: str | Path | None = None,
    api_base_url: str | None = None,
    required_permission: str = "report.ai",
    allow_anonymous_api: bool = False,
) -> Path:
    """Import MES JWT trust settings once and persist the Agent configuration."""

    document, jwt_config = _read_mes_jwt_settings(appsettings_path)
    secret = str(jwt_config.get("SecurityKey") or "").strip()
    issuer = str(jwt_config.get("Issuer") or "").strip()
    audience = str(jwt_config.get("Audience") or "").strip()
    algorithm = str(jwt_config.get("Algorithm") or "HS256").strip() or "HS256"
    if not secret or not issuer or not audience:
        raise ValueError("MES JWT configuration must include SecurityKey, Issuer and Audience")

    resolved_api_base = (api_base_url or _infer_api_base_url(document)).strip().rstrip("/")
    permission = required_permission.strip()
    if permission and not resolved_api_base:
        raise ValueError("MES API base URL is required when permission checking is enabled")

    root = get_workspace_root(workspace)
    current = load_gateway_config(root)
    mes = MesWorkspaceConfig(
        enabled=True,
        issuer=issuer,
        audience=audience,
        algorithm=algorithm,
        required_permission=permission,
        api_base_url=resolved_api_base,
        permission_url="",
        allow_anonymous_api=allow_anonymous_api,
    )
    store_credential(
        _mes_credential_provider(root),
        _MES_CREDENTIAL_KEY,
        secret,
    )
    save_gateway_config(current.model_copy(update={"mes": mes}), root)
    validate_mes_workspace_config(root)
    return get_gateway_config_path(root)


def build_channel_manager_config(config: GatewayConfig) -> Config:
    """Project gateway settings into the channel compatibility models."""
    root = Config()
    root.channels.send_progress = config.send_progress
    root.channels.send_tool_hints = config.send_tool_hints
    for name in config.enabled_channels:
        if not hasattr(root.channels, name):
            continue
        base_config = getattr(root.channels, name)
        channel_payload = base_config.model_dump()
        channel_payload.update({"enabled": True, **config.channel_configs.get(name, {})})
        channel_config = type(base_config).model_validate(channel_payload)
        setattr(root.channels, name, channel_config)
    return root
