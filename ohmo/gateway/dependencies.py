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
    """Validated HSJM bearer auth for one gateway request."""

    claims: dict[str, Any]
    raw_token: str


def _decode_bearer_token(token: str) -> dict[str, Any]:
    if jwt is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="python-jose not installed",
        )
    try:
        return jwt.decode(
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


async def get_auth_context(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> AuthContext:
    """Validate Bearer JWT token and keep both claims and raw token."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authorization token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = credentials.credentials
    return AuthContext(claims=_decode_bearer_token(token), raw_token=token)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> dict[str, Any]:
    """Validate Bearer JWT token and return decoded claims."""
    context = await get_auth_context(credentials)
    return context.claims


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
        return _decode_bearer_token(credentials.credentials)
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
