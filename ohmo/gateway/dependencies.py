"""FastAPI shared dependencies: JWT auth + singleton runtime state."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import httpx

from ohmo.gateway.config import resolve_mes_workspace_config

try:
    from jose import JWTError, jwt
except ImportError:  # pragma: no cover
    jwt = None  # type: ignore[assignment]
    JWTError = Exception  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JWT configuration (mirrors HSJM.OS.Service appsettings.json)
# ---------------------------------------------------------------------------

JWT_SECRET_KEY = "hsjm-os-service-secret-key-32chars!!"
JWT_ISSUER = "hsjm-os.com"
JWT_AUDIENCE = "hsjm-os.com"
JWT_ALGORITHM = "HS256"

_PROFILE_ENV_PREFIX = "OHMO_JWT_"
_ADMIN_VALUES_ENV = "OHMO_JWT_ADMIN_VALUES"

_bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    """Stable identity derived from either an OS or MES JWT."""

    source: str
    subject: str
    account: str | None = None
    name: str | None = None
    is_admin: bool = False


@dataclass(frozen=True)
class AuthContext:
    """Validated request auth plus the original bearer token for tool forwarding."""

    user: dict[str, Any]
    raw_token: str
    principal: Principal | None = None


@dataclass(frozen=True)
class _JwtProfile:
    source: str
    secret_key: str
    issuer: str
    audience: str
    algorithm: str = JWT_ALGORITHM


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value.strip() if isinstance(value, str) and value.strip() else default


def _jwt_profiles() -> tuple[_JwtProfile, ...]:
    """Return enabled trust profiles.

    The OS profile deliberately retains the old hard-coded values as defaults.
    MES is opt-in and normally comes from the persistent workspace config.
    Environment variables remain optional overrides for existing deployments.
    """

    os_profile = _JwtProfile(
        source="os",
        secret_key=_env(f"{_PROFILE_ENV_PREFIX}OS_SECRET_KEY", JWT_SECRET_KEY) or JWT_SECRET_KEY,
        issuer=_env(f"{_PROFILE_ENV_PREFIX}OS_ISSUER", JWT_ISSUER) or JWT_ISSUER,
        audience=_env(f"{_PROFILE_ENV_PREFIX}OS_AUDIENCE", JWT_AUDIENCE) or JWT_AUDIENCE,
        algorithm=_env(f"{_PROFILE_ENV_PREFIX}OS_ALGORITHM", JWT_ALGORITHM) or JWT_ALGORITHM,
    )
    profiles = [os_profile]
    mes_config = resolve_mes_workspace_config(getattr(_state, "workspace", None))
    if (
        mes_config.enabled
        and mes_config.secret_key
        and mes_config.issuer
        and mes_config.audience
    ):
        profiles.append(
            _JwtProfile(
                source="mes",
                secret_key=mes_config.secret_key,
                issuer=mes_config.issuer,
                audience=mes_config.audience,
                algorithm=mes_config.algorithm,
            )
        )
    elif mes_config.enabled:
        logger.warning(
            "MES JWT profile is incomplete and will be ignored; configure secret, issuer and audience"
        )
    return tuple(profiles)


def _first_claim(payload: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = payload.get(name)
        if value is not None and str(value).strip():
            return value
    return None


def _claim_is_admin(payload: dict[str, Any]) -> bool:
    # Both HSJM.OS.Service and HSJM.Factory.MES define SuperAdministrator as
    # 999 and put that numeric AdminType in CLAINM_SUPERADMIN.  Additional
    # values must be explicitly configured rather than inferred from role text.
    configured = _env(_ADMIN_VALUES_ENV, "999,true") or ""
    admin_values = {item.strip().lower() for item in configured.split(",") if item.strip()}
    candidates: list[Any] = [
        payload.get("CLAINM_SUPERADMIN"),
        payload.get("CLAINM_ADMINTYPE"),
        payload.get("is_admin"),
        payload.get("role"),
    ]
    roles = payload.get("roles")
    if isinstance(roles, (list, tuple, set)):
        candidates.extend(roles)
    return any(str(value).strip().lower() in admin_values for value in candidates if value is not None)


def principal_from_claims(payload: dict[str, Any], *, source: str) -> Principal:
    """Normalize product-specific claims into the ownership identity."""

    subject = _first_claim(
        payload,
        "CLAINM_USERID",
        "sub",
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/nameidentifier",
    )
    if subject is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization token has no user identity",
            headers={"WWW-Authenticate": "Bearer"},
        )
    account = _first_claim(
        payload,
        "CLAINM_ACCOUNT",
        "preferred_username",
        "unique_name",
    )
    name = _first_claim(
        payload,
        "CLAINM_NAME",
        "name",
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name",
    )
    return Principal(
        source=source,
        subject=str(subject),
        account=str(account) if account is not None else None,
        name=str(name) if name is not None else None,
        is_admin=_claim_is_admin(payload),
    )


def principal_from_user_claims(payload: dict[str, Any]) -> Principal:
    """Best-effort compatibility helper for routes still depending on claims."""

    source = str(payload.get("_ohmo_owner_source") or "os")
    return principal_from_claims(payload, source=source)


def _decode_bearer_with_principal(
    credentials: HTTPAuthorizationCredentials | None,
    *,
    allowed_sources: frozenset[str] | None = None,
) -> tuple[dict[str, Any], str, Principal]:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authorization token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = credentials.credentials
    if jwt is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="python-jose not installed",
        )
    last_error: Exception | None = None
    for profile in _jwt_profiles():
        if allowed_sources is not None and profile.source not in allowed_sources:
            continue
        try:
            payload = jwt.decode(
                token,
                profile.secret_key,
                algorithms=[profile.algorithm],
                issuer=profile.issuer,
                audience=profile.audience,
            )
            principal = principal_from_claims(payload, source=profile.source)
            # Internal metadata lets legacy claim-only dependencies participate
            # in owner checks without changing their public return type.
            payload = dict(payload)
            payload["_ohmo_owner_source"] = principal.source
            return payload, token, principal
        except JWTError as exc:
            last_error = exc
            logger.debug("JWT validation failed for %s profile: %s", profile.source, exc)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    ) from last_error


def _decode_bearer_credentials(
    credentials: HTTPAuthorizationCredentials | None,
) -> tuple[dict[str, Any], str]:
    """Validate the legacy OS token while preserving the two-item contract.

    Management routers depend on this helper through ``get_current_user``.
    Keeping it OS-only prevents a MES workspace token from implicitly gaining
    access to agent, skill, model, task and cron administration endpoints.
    """

    payload, token, _principal = _decode_bearer_with_principal(
        credentials,
        allowed_sources=frozenset({"os"}),
    )
    return payload, token


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> dict[str, Any]:
    """Validate Bearer JWT token and return decoded claims."""
    payload, _token = _decode_bearer_credentials(credentials)
    return payload


async def get_auth_context(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> AuthContext:
    """Validate Bearer JWT token and preserve the raw token for downstream tools."""
    payload, token, principal = _decode_bearer_with_principal(credentials)
    return AuthContext(user=payload, raw_token=token, principal=principal)


async def _authorize_workspace_access(auth: AuthContext) -> AuthContext:
    """Optionally enforce the MES permission used by the native AI workspace."""

    principal = auth.principal or principal_from_user_claims(auth.user)
    mes_config = resolve_mes_workspace_config(getattr(_state, "workspace", None))
    required_permission = mes_config.required_permission if mes_config.enabled else ""
    if principal.source != "mes" or not required_permission:
        return auth

    permission_url = mes_config.permission_endpoint
    if not permission_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MES permission validation is not configured",
        )

    try:
        async with httpx.AsyncClient(
            timeout=5.0,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.get(
                permission_url,
                headers={"Authorization": f"Bearer {auth.raw_token}"},
            )
        if response.status_code == status.HTTP_401_UNAUTHORIZED:
            raise HTTPException(status_code=401, detail="MES token is no longer valid")
        response.raise_for_status()
        payload = response.json()
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError):
        logger.exception("Failed to validate MES workspace permission")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MES permission service is unavailable",
        ) from None

    permissions: Any = payload
    if isinstance(payload, dict):
        permissions = payload.get("data")
        if permissions is None:
            permissions = payload.get("backResult")
        if permissions is None:
            permissions = payload.get("BackResult")
    if not isinstance(permissions, list) or required_permission not in {
        str(value) for value in permissions
    }:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="AI workspace access denied")
    return auth


async def get_workspace_auth_context(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> AuthContext:
    payload, token, principal = _decode_bearer_with_principal(credentials)
    return await _authorize_workspace_access(
        AuthContext(user=payload, raw_token=token, principal=principal)
    )


async def get_optional_workspace_auth_context(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> AuthContext | None:
    if credentials is None:
        return None
    return await get_workspace_auth_context(credentials)


async def get_optional_auth_context(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> AuthContext | None:
    """Return auth context when a Bearer token is present; allow anonymous requests."""
    if credentials is None:
        return None
    payload, token, principal = _decode_bearer_with_principal(credentials)
    return AuthContext(user=payload, raw_token=token, principal=principal)


# ---------------------------------------------------------------------------
# Optional auth (allows unauthenticated during development)
# ---------------------------------------------------------------------------

async def get_optional_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> dict[str, Any] | None:
    """Like get_current_user but returns None when no token is provided (dev mode)."""
    if credentials is None:
        return None
    try:
        return await get_current_user(credentials)
    except HTTPException:
        return None


# ---------------------------------------------------------------------------
# Singleton runtime holders (populated at startup in api.py)
# ---------------------------------------------------------------------------

class _RuntimeState:
    task_manager: Any = None          # BackgroundTaskManager
    mcp_manager: Any = None           # McpClientManager
    skill_registry: Any = None        # SkillRegistry
    disabled_skills: set[str] = set()
    # session storage
    workspace: str | None = None

_state = _RuntimeState()


def get_runtime() -> _RuntimeState:
    return _state
