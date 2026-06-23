"""Agent-side proxy routes for ProductionIssue APIs."""

from __future__ import annotations

import os
from typing import Annotated
from urllib.parse import urljoin

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from openharness.config.settings import load_settings
from ohmo.gateway.dependencies import _decode_bearer_credentials

router = APIRouter(prefix="/ProductionIssue", tags=["production-issue"])

_bearer_scheme = HTTPBearer(auto_error=False)
_DINGTALK_INTERNAL_API_TOKEN_ENV = "OHMO_DINGTALK_INTERNAL_API_TOKEN"
_DINGTALK_INTERNAL_API_DEFAULT_TOKEN = "123"
_PRODUCTION_ISSUE_UPSTREAM_BASE_URL_ENV = "OHMO_PRODUCTION_ISSUE_UPSTREAM_BASE_URL"


async def _resolve_forward_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> str:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authorization token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials.strip()
    if token == _dingtalk_internal_api_token():
        return token

    _decode_bearer_credentials(credentials)
    return token


@router.post("/Insert")
async def insert(
    request: Request,
    token: Annotated[str, Depends(_resolve_forward_token)],
) -> Response:
    return await _forward(request, "POST", "/ProductionIssue/Insert", token)


@router.post("/AppendProcess")
async def append_process(
    request: Request,
    token: Annotated[str, Depends(_resolve_forward_token)],
) -> Response:
    return await _forward(request, "POST", "/ProductionIssue/AppendProcess", token)


@router.post("/UpdateStatus")
async def update_status(
    request: Request,
    token: Annotated[str, Depends(_resolve_forward_token)],
) -> Response:
    return await _forward(request, "POST", "/ProductionIssue/UpdateStatus", token)


@router.get("/GetListPaged")
async def get_list_paged(
    request: Request,
    token: Annotated[str, Depends(_resolve_forward_token)],
) -> Response:
    return await _forward(request, "GET", "/ProductionIssue/GetListPaged", token)


@router.get("/Get")
async def get_detail(
    request: Request,
    token: Annotated[str, Depends(_resolve_forward_token)],
) -> Response:
    return await _forward(request, "GET", "/ProductionIssue/Get", token)


async def _forward(request: Request, method: str, path: str, token: str) -> Response:
    base_url = _production_issue_upstream_base_url()
    target_url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    body = await request.body()
    headers = {"Authorization": f"Bearer {token}"}
    content_type = request.headers.get("content-type")
    if content_type:
        headers["Content-Type"] = content_type

    async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
        upstream = await client.request(
            method,
            target_url,
            params=dict(request.query_params),
            content=body if body else None,
            headers=headers,
        )

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )


def _production_issue_upstream_base_url() -> str:
    base_url = os.environ.get(_PRODUCTION_ISSUE_UPSTREAM_BASE_URL_ENV, "").strip()
    if not base_url:
        base_url = load_settings().internal_api.base_url.strip()
    if not base_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "ProductionIssue upstream is not configured. "
                f"Set {_PRODUCTION_ISSUE_UPSTREAM_BASE_URL_ENV} or internal_api.base_url."
            ),
        )
    return base_url


def _dingtalk_internal_api_token() -> str:
    raw = os.environ.get(_DINGTALK_INTERNAL_API_TOKEN_ENV, _DINGTALK_INTERNAL_API_DEFAULT_TOKEN)
    token = raw.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token
