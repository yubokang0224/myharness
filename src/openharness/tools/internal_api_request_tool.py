"""HTTP tool that forwards the HSJM user token to configured internal APIs."""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin

import httpx
from pydantic import BaseModel, ConfigDict, Field

from openharness.config.settings import load_settings
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from openharness.utils.internal_api_auth import (
    apply_internal_api_auth,
    resolve_hsjm_user_token,
    resolve_internal_api_url,
)
from openharness.utils.network_guard import NetworkGuardError, validate_http_url

MAX_REDIRECTS = 5


class InternalApiRequestInput(BaseModel):
    """Arguments for an internal or external HTTP API request."""

    model_config = ConfigDict(populate_by_name=True)

    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = Field(default="GET")
    url: str = Field(description="Absolute URL or internal API path such as /api/items")
    params: dict[str, str | int | float | bool] | None = None
    headers: dict[str, str] | None = None
    json_body: Any = Field(default=None, alias="json")
    body: Any = Field(default=None, description="Compatibility alias for JSON request bodies.")
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
            json_body_input = _coerce_json_body(arguments.json_body, arguments.body)
            method = _normalize_method(arguments.method, url, json_body_input)
            json_body = _json_body_with_channel_context(
                url,
                json_body_input,
                metadata=context.metadata,
            )
            await _upload_local_attachments_in_place(json_body, url, context.metadata)
            headers = _headers_with_channel_context(
                arguments.headers,
                metadata=context.metadata,
            )
            response, auth_attached = await _request_with_auth_per_redirect(
                method=method,
                url=url,
                params=arguments.params,
                headers=headers,
                json_body=json_body,
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


def _headers_with_channel_context(
    headers: dict[str, str] | None,
    *,
    metadata: dict[str, Any] | None,
) -> dict[str, str]:
    next_headers = dict(headers or {})
    if any(key.lower() == "x-ohmo-source-channel" for key in next_headers):
        return next_headers
    if not isinstance(metadata, dict):
        return next_headers
    channel_context = metadata.get("channel_context")
    if not isinstance(channel_context, dict):
        return next_headers
    channel = channel_context.get("channel")
    if isinstance(channel, str) and channel.strip():
        next_headers["X-OHMO-Source-Channel"] = channel.strip()
    return next_headers


def _coerce_json_body(json_body: Any, body: Any) -> Any:
    candidate = json_body if json_body is not None else body
    if isinstance(candidate, str):
        text = candidate.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return candidate
    return candidate


def _normalize_method(method: str, url: str, json_body: Any) -> str:
    normalized = str(method or "GET").upper()
    if (
        normalized == "GET"
        and json_body is not None
        and (_is_production_issue_write_url(url) or _is_board_memo_write_url(url) or _is_kb_write_url(url))
    ):
        return "POST"
    return normalized


def _json_body_with_channel_context(
    url: str,
    json_body: Any,
    *,
    metadata: dict[str, Any] | None,
) -> Any:
    if not (_is_production_issue_url(url) or _is_board_memo_url(url) or _is_kb_url(url)) or not isinstance(json_body, dict):
        return json_body
    if not isinstance(metadata, dict):
        return json_body
    channel_context = metadata.get("channel_context")
    if not isinstance(channel_context, dict):
        return json_body

    channel = _metadata_text(channel_context.get("channel"))
    if not channel:
        return json_body

    next_body = dict(json_body)
    _set_if_missing(next_body, "sourceChannel", channel)
    _set_if_missing(next_body, "sourceChatId", _metadata_text(channel_context.get("chat_id")))
    _set_if_missing(next_body, "sourceSenderId", _metadata_text(channel_context.get("source_sender_id")))
    _set_if_missing(next_body, "sourceSenderName", _metadata_text(channel_context.get("sender_name")))
    _set_if_missing(next_body, "sourceMessageId", _metadata_text(channel_context.get("message_id")))
    _set_if_missing(next_body, "sourceConversationId", _metadata_text(channel_context.get("conversation_id")))
    _set_if_missing(next_body, "sourceSessionKey", _metadata_text(channel_context.get("session_key")))
    _set_if_missing(next_body, "reporterName", _metadata_text(channel_context.get("sender_name")))
    _set_production_issue_idempotency_key(next_body, url, channel_context)

    body_attachments = next_body.get("attachments")
    needs_attachments = not body_attachments
    if _is_board_memo_insert_url(url) and not _has_usable_attachment(body_attachments):
        # 模型可能带了只剩 fileName 的空壳附件（拿不到本地路径时），一并视为缺失
        needs_attachments = True
    if needs_attachments:
        attachment_paths = channel_context.get("attachment_paths")
        if _is_board_memo_insert_url(url) and not _paths_present(attachment_paths):
            # 确认消息本身不带附件：回退到本会话最近一次带图消息（白板首图）
            attachment_paths = channel_context.get("last_attachment_paths")
        if isinstance(attachment_paths, list):
            attachments = [
                {
                    "fileName": Path(str(path)).name,
                    "sourceLocalPath": str(path),
                    "sourceType": channel,
                }
                for path in attachment_paths
                if str(path).strip()
            ]
            if attachments:
                next_body["attachments"] = attachments

    _set_board_memo_insert_idempotency_key(next_body, channel_context, url)

    return next_body


_IMAGE_UPLOAD_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


async def _upload_local_attachments_in_place(
    json_body: Any,
    url: str,
    metadata: dict[str, Any] | None,
) -> None:
    """Upload attachment files that exist on this machine, filling fileUrl in place.

    附件文件在工具所在机器上，而 2416 中转可能部署在另一台机器、读不到本机路径。
    因此凡本机存在的 sourceLocalPath 先直传 Common/Upload* 换成 fileUrl 再转发；
    带 fileUrl 的附件下游中转会原样放行。上传失败时保留原路径，交由中转兜底。
    """
    if not _is_board_memo_insert_url(url) or not isinstance(json_body, dict):
        return
    attachments = json_body.get("attachments")
    if not isinstance(attachments, list):
        return

    pending = [
        entry
        for entry in attachments
        if isinstance(entry, dict)
        and not str(entry.get("fileUrl") or "").strip()
        and str(entry.get("sourceLocalPath") or "").strip()
        and Path(str(entry.get("sourceLocalPath")).strip()).is_file()
    ]
    if not pending:
        return

    token = resolve_hsjm_user_token(metadata)
    if not token:
        try:
            token = str(load_settings().internal_api.dingtalk_token or "").strip()
        except Exception:
            token = ""
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
    if not token:
        return

    async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
        for entry in pending:
            path = Path(str(entry.get("sourceLocalPath")).strip())
            try:
                content = path.read_bytes()
            except OSError:
                continue
            extension = path.suffix.lower()
            action = "UploadImage" if extension in _IMAGE_UPLOAD_EXTENSIONS else "UploadFile"
            mime_type, _ = mimetypes.guess_type(str(path))
            upload_url = resolve_internal_api_url(f"/Common/{action}")
            try:
                response = await client.post(
                    upload_url,
                    headers={"Authorization": f"Bearer {token}"},
                    files={"file": (path.name, content, mime_type or "application/octet-stream")},
                )
            except httpx.HTTPError:
                continue
            if response.status_code >= 400:
                continue
            try:
                result = response.json()
            except ValueError:
                continue
            is_success = result.get("isSuccess")
            if is_success is None:
                is_success = result.get("IsSuccess")
            file_url = result.get("backResult") or result.get("BackResult")
            if not is_success or not file_url:
                continue
            entry["fileUrl"] = str(file_url)
            entry["fileName"] = path.name
            entry["fileSize"] = len(content)
            if mime_type and not str(entry.get("mimeType") or "").strip():
                entry["mimeType"] = mime_type


def _paths_present(paths: Any) -> bool:
    return isinstance(paths, list) and any(str(path).strip() for path in paths)


def _has_usable_attachment(attachments: Any) -> bool:
    if not isinstance(attachments, list):
        return False
    for entry in attachments:
        if isinstance(entry, dict) and (
            str(entry.get("fileUrl") or "").strip()
            or str(entry.get("sourceLocalPath") or "").strip()
        ):
            return True
    return False


def _is_board_memo_insert_url(url: str) -> bool:
    return url.lower().rstrip("/").endswith("/boardmemo/insert")


def _set_board_memo_insert_idempotency_key(
    body: dict[str, Any],
    channel_context: dict[str, Any],
    url: str,
) -> None:
    """Fill the insert idempotency key from the photo message when the model omits it."""
    if not _is_board_memo_insert_url(url):
        return
    if body.get("idempotencyKey") or body.get("idempotency_key"):
        return
    message_id = _metadata_text(channel_context.get("last_attachment_message_id")) or _metadata_text(
        channel_context.get("message_id")
    )
    if message_id:
        body["idempotencyKey"] = f"board-memo:{message_id}:insert"


def _is_production_issue_url(url: str) -> bool:
    return "/productionissue/" in url.lower()


def _is_production_issue_write_url(url: str) -> bool:
    normalized = url.lower().rstrip("/")
    return any(
        normalized.endswith(path)
        for path in (
            "/productionissue/insert",
            "/productionissue/appendprocess",
            "/productionissue/updatestatus",
        )
    )


def _set_production_issue_idempotency_key(
    body: dict[str, Any],
    url: str,
    channel_context: dict[str, Any],
) -> None:
    if body.get("idempotencyKey") or body.get("idempotency_key"):
        return

    normalized = url.lower().rstrip("/")
    if normalized.endswith("/productionissue/insert"):
        action = "insert"
    elif normalized.endswith(("/productionissue/appendprocess", "/productionissue/updatestatus")):
        action = "process"
    else:
        return

    seed = (
        _metadata_text(channel_context.get("message_id"))
        or _metadata_text(body.get("sourceMessageId"))
        or _metadata_text(channel_context.get("timestamp"))
        or _metadata_text(channel_context.get("session_key"))
    )
    if not seed:
        return

    body["idempotencyKey"] = f"production-site-issue:{seed}:{action}"


def _is_kb_url(url: str) -> bool:
    return "/kb/" in url.lower()


def _is_kb_write_url(url: str) -> bool:
    return url.lower().rstrip("/").endswith("/kb/ingest")


def _is_board_memo_url(url: str) -> bool:
    return "/boardmemo/" in url.lower()


def _is_board_memo_write_url(url: str) -> bool:
    normalized = url.lower().rstrip("/")
    return any(
        normalized.endswith(path)
        for path in (
            "/boardmemo/insert",
            "/boardmemo/appenditem",
            "/boardmemo/updateitem",
        )
    )


def _metadata_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _set_if_missing(body: dict[str, Any], key: str, value: str) -> None:
    if not value:
        return
    if key in body and body[key]:
        return
    body[key] = value
