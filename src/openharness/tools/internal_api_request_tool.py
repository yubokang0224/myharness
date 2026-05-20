"""HTTP tool that forwards the HSJM user token to configured internal APIs."""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urljoin

import httpx
from pydantic import BaseModel, ConfigDict, Field

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from openharness.utils.internal_api_auth import apply_internal_api_auth, resolve_internal_api_url
from openharness.utils.network_guard import NetworkGuardError, validate_http_url

MAX_REDIRECTS = 5


class InternalApiRequestInput(BaseModel):
    """Arguments for an internal or external HTTP API request."""

    model_config = ConfigDict(populate_by_name=True)

    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = Field(default="GET")
    url: str = Field(description="Absolute URL or internal API path such as /api/items")
    params: dict[str, str] | None = None
    headers: dict[str, str] | None = None
    json_body: Any = Field(default=None, alias="json")
    timeout: float = Field(default=30.0, ge=1.0, le=120.0)
    max_chars: int = Field(default=12000, ge=500, le=50000)


class InternalApiRequestTool(BaseTool):
    """Call an HTTP API, adding the current HSJM token only for allowlisted origins."""

    name = "internal_api_request"
    description = (
        "Call an HTTP API. Relative paths use the configured internal_api.base_url. "
        "Allowlisted internal origins automatically receive the current user's Bearer token; "
        "other URLs are requested without that token."
    )
    input_model = InternalApiRequestInput

    async def execute(
        self,
        arguments: InternalApiRequestInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        url = resolve_internal_api_url(arguments.url)
        try:
            validate_http_url(url)
        except NetworkGuardError as exc:
            return ToolResult(output=f"internal_api_request failed: {exc}", is_error=True)

        try:
            response, auth_attached = await _request_with_auth_per_redirect(
                method=arguments.method,
                url=url,
                params=arguments.params,
                headers=arguments.headers,
                json_body=arguments.json_body,
                timeout=arguments.timeout,
                metadata=context.metadata,
            )
        except (httpx.HTTPError, NetworkGuardError) as exc:
            return ToolResult(output=f"internal_api_request failed: {exc}", is_error=True)

        content_type = response.headers.get("content-type", "")
        body = response.text
        if len(body) > arguments.max_chars:
            body = body[: arguments.max_chars].rstrip() + "\n...[truncated]"

        return ToolResult(
            output=(
                f"URL: {response.url}\n"
                f"Status: {response.status_code}\n"
                f"Content-Type: {content_type or '(unknown)'}\n"
                f"Internal auth attached: {str(auth_attached).lower()}\n\n"
                f"{body}"
            )
        )

    def is_read_only(self, arguments: BaseModel) -> bool:
        method = getattr(arguments, "method", "GET")
        return str(method).upper() == "GET"


async def _request_with_auth_per_redirect(
    *,
    method: str,
    url: str,
    params: dict[str, str] | None,
    headers: dict[str, str] | None,
    json_body: Any,
    timeout: float,
    metadata: dict[str, Any],
) -> tuple[httpx.Response, bool]:
    current_url = url
    current_params = params
    auth_attached_any = False
    async with httpx.AsyncClient(follow_redirects=False, timeout=timeout, trust_env=False) as client:
        for redirect_count in range(MAX_REDIRECTS + 1):
            validate_http_url(current_url)
            request_url, request_headers, attached = apply_internal_api_auth(
                current_url,
                headers,
                metadata=metadata,
            )
            auth_attached_any = auth_attached_any or attached
            response = await client.request(
                method,
                request_url,
                params=current_params,
                headers=request_headers,
                json=json_body,
            )
            if not response.has_redirect_location:
                return response, auth_attached_any
            location = response.headers.get("location")
            if not location:
                return response, auth_attached_any
            if redirect_count >= MAX_REDIRECTS:
                raise NetworkGuardError(f"too many redirects (>{MAX_REDIRECTS})")
            current_url = urljoin(str(response.url), location)
            current_params = None

    raise NetworkGuardError("request failed before receiving a response")
