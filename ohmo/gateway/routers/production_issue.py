"""Production issue proxy routes for agent-side skill calls."""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urljoin

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from openharness.config import load_settings
from ohmo.gateway.dependencies import _decode_bearer_credentials

router = APIRouter(prefix="/ProductionIssue", tags=["ProductionIssue"])

_bearer_scheme = HTTPBearer(auto_error=False)
_SOURCE_CHANNEL_HEADER = "x-ohmo-source-channel"
_HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
@router.post("/Insert")
async def insert_production_issue(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> Response:
    return await _forward_production_issue("Insert", request, credentials)


@router.post("/AppendProcess")
async def append_production_issue_process(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> Response:
    return await _forward_production_issue("AppendProcess", request, credentials)


@router.post("/UpdateStatus")
async def update_production_issue_status(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> Response:
    return await _forward_production_issue("UpdateStatus", request, credentials)


@router.get("/GetListPaged")
async def get_production_issue_list(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> Response:
    return await _forward_production_issue("GetListPaged", request, credentials)


@router.get("/Get")
async def get_production_issue(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> Response:
    return await _forward_production_issue("Get", request, credentials)


async def _forward_production_issue(
    action: str,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
) -> Response:
    """Forward ProductionIssue skill calls to the configured upstream API."""

    settings = load_settings()
    upstream_base_url = settings.internal_api.base_url.strip()
    if not upstream_base_url:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="internal_api.base_url is not configured")

    upstream_url = urljoin(upstream_base_url.rstrip("/") + "/", f"ProductionIssue/{action}")
    headers = _forward_headers(request)
    headers["Authorization"] = f"Bearer {_resolve_forward_token(request, credentials, settings.internal_api.dingtalk_token)}"

    body = await request.body()
    async with httpx.AsyncClient(follow_redirects=False, timeout=30.0, trust_env=False) as client:
        upstream_response = await client.request(
            request.method,
            upstream_url,
            params=dict(request.query_params),
            headers=headers,
            content=body,
        )

    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        media_type=upstream_response.headers.get("content-type"),
    )


def _resolve_forward_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
    dingtalk_token: str,
) -> str:
    source_channel = (request.headers.get(_SOURCE_CHANNEL_HEADER) or "").strip().lower()
    if source_channel == "dingtalk":
        token = dingtalk_token.strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if token:
            return token

    _user, token = _decode_bearer_credentials(credentials)
    return token


def _forward_headers(request: Request) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in request.headers.items():
        lower_key = key.lower()
        if lower_key in _HOP_BY_HOP_HEADERS or lower_key == "authorization" or lower_key == _SOURCE_CHANNEL_HEADER:
            continue
        headers[key] = value
    return headers
