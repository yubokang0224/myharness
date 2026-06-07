"""FastAPI shared dependencies: JWT auth + singleton runtime state."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

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

_bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AuthContext:
    """Validated request auth plus the original bearer token for tool forwarding."""

    user: dict[str, Any]
    raw_token: str


def _decode_bearer_credentials(
    credentials: HTTPAuthorizationCredentials | None,
) -> tuple[dict[str, Any], str]:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authorization token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = credentials.credentials
    try:
        if jwt is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="python-jose not installed",
            )
        payload = jwt.decode(
            token,
            JWT_SECRET_KEY,
            algorithms=[JWT_ALGORITHM],
            issuer=JWT_ISSUER,
            audience=JWT_AUDIENCE,
        )
    except JWTError as exc:
        logger.debug("JWT validation failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
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
    payload, token = _decode_bearer_credentials(credentials)
    return AuthContext(user=payload, raw_token=token)


async def get_optional_auth_context(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> AuthContext | None:
    """Return auth context when a Bearer token is present; allow anonymous requests."""
    if credentials is None:
        return None
    payload, token = _decode_bearer_credentials(credentials)
    return AuthContext(user=payload, raw_token=token)


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
