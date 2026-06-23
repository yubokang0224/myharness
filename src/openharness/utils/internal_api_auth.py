"""Helpers for forwarding the current HSJM user token to internal APIs."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import urljoin, urlparse

from openharness.config.settings import Settings, load_settings

USER_BEARER_TOKEN_ENV = "HSJM_USER_BEARER_TOKEN"

_DEFAULT_PORTS = {
    "http": 80,
    "https": 443,
}


def make_hsjm_auth_metadata(token: str | None) -> dict[str, str]:
    """Build runtime metadata for the current HSJM user token."""
    token = (token or "").strip()
    return {"token": token} if token else {}


def resolve_hsjm_user_token(
    metadata: Mapping[str, Any] | None = None,
) -> str:
    """Return the current HSJM user token from tool metadata or task env."""
    if metadata:
        raw_auth = metadata.get("hsjm_auth")
        if isinstance(raw_auth, Mapping):
            token = raw_auth.get("token")
            if isinstance(token, str) and token.strip():
                return _normalize_bearer_token(token)
    return _normalize_bearer_token(os.environ.get(USER_BEARER_TOKEN_ENV, ""))


def resolve_internal_api_url(url: str, settings: Settings | None = None) -> str:
    """Resolve relative internal API paths against the configured base URL."""
    raw = url.strip()
    if not raw:
        return raw
    parsed = urlparse(raw)
    if parsed.scheme:
        return raw

    resolved_settings = settings or load_settings()
    base_url = resolved_settings.internal_api.base_url.strip()
    if not base_url:
        return raw
    return urljoin(base_url.rstrip("/") + "/", raw.lstrip("/"))


def is_internal_api_url(url: str, settings: Settings | None = None) -> bool:
    """Return whether a URL origin is configured for token forwarding."""
    resolved_settings = settings or load_settings()
    target_origin = normalized_origin(url)
    if target_origin is None:
        return False
    allowlist = {
        origin
        for item in resolved_settings.internal_api.allowlist
        for origin in [normalized_origin(item)]
        if origin is not None
    }
    return target_origin in allowlist


def apply_internal_api_auth(
    url: str,
    headers: Mapping[str, str] | None = None,
    *,
    metadata: Mapping[str, Any] | None = None,
    settings: Settings | None = None,
) -> tuple[str, dict[str, str], bool]:
    """Resolve URL and add Authorization when it targets an allowed internal API."""
    resolved_settings = settings or load_settings()
    resolved_url = resolve_internal_api_url(url, resolved_settings)
    next_headers = dict(headers or {})
    if _has_authorization_header(next_headers):
        return resolved_url, next_headers, False
    if not is_internal_api_url(resolved_url, resolved_settings):
        return resolved_url, next_headers, False
    token = resolve_hsjm_user_token(metadata)
    if not token:
        return resolved_url, next_headers, False
    next_headers["Authorization"] = f"Bearer {token}"
    return resolved_url, next_headers, True


def _normalize_bearer_token(value: str) -> str:
    token = (value or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def normalized_origin(url: str) -> str | None:
    """Normalize a URL or origin string to scheme://host:port."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    port = parsed.port or _DEFAULT_PORTS[parsed.scheme]
    host = parsed.hostname.lower()
    return f"{parsed.scheme.lower()}://{host}:{port}"


def _has_authorization_header(headers: Mapping[str, str]) -> bool:
    return any(key.lower() == "authorization" for key in headers)
