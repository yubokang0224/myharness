"""Existing knowledge-base attachment proxy helpers.

Extracted unchanged when ProductionIssue moved to the authenticated MES client.
This module is not used by the production issue workflow.
"""
from __future__ import annotations
import json
import logging
import mimetypes
from pathlib import Path
from urllib.parse import urljoin
import httpx
from fastapi import Request
from fastapi.security import HTTPAuthorizationCredentials
from openharness.channels.impl.base import resolve_channel_media_dir
from ohmo.gateway.dependencies import _decode_bearer_credentials
logger = logging.getLogger(__name__)

_SOURCE_CHANNEL_HEADER = "x-ohmo-source-channel"


_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


_FILE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".csv",
    ".txt",
    ".ppt",
    ".pptx",
    ".zip",
    ".rar",
    ".7z",
}


def _resolve_forward_token(
    credentials: HTTPAuthorizationCredentials | None,
    dingtalk_token: str,
    source_channel: str,
) -> str:
    if source_channel == "dingtalk":
        token = dingtalk_token.strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if token:
            return token

    _user, token = _decode_bearer_credentials(credentials)
    return token


def _source_channel(request: Request, body: bytes) -> str:
    header_channel = (request.headers.get(_SOURCE_CHANNEL_HEADER) or "").strip().lower()
    if header_channel:
        return header_channel
    query_channel = (request.query_params.get("sourceChannel") or request.query_params.get("source_channel") or "").strip().lower()
    if query_channel:
        return query_channel
    body_channel = _source_channel_from_json_body(body)
    return body_channel.strip().lower()


async def _upload_attachments_recursively(
    value: object,
    *,
    upstream_base_url: str,
    authorization: str,
    source_channel: str,
    client: httpx.AsyncClient,
) -> bool:
    changed = False
    if isinstance(value, dict):
        attachments = value.get("attachments")
        if isinstance(attachments, list):
            for attachment in attachments:
                if isinstance(attachment, dict):
                    uploaded = await _upload_attachment_if_needed(
                        attachment,
                        upstream_base_url=upstream_base_url,
                        authorization=authorization,
                        source_channel=source_channel,
                        client=client,
                    )
                    changed = changed or uploaded
        for key, child in value.items():
            if key == "attachments":
                continue
            nested_changed = await _upload_attachments_recursively(
                child,
                upstream_base_url=upstream_base_url,
                authorization=authorization,
                source_channel=source_channel,
                client=client,
            )
            changed = changed or nested_changed
    elif isinstance(value, list):
        for child in value:
            nested_changed = await _upload_attachments_recursively(
                child,
                upstream_base_url=upstream_base_url,
                authorization=authorization,
                source_channel=source_channel,
                client=client,
            )
            changed = changed or nested_changed
    return changed


async def _upload_attachment_if_needed(
    attachment: dict[str, object],
    *,
    upstream_base_url: str,
    authorization: str,
    source_channel: str,
    client: httpx.AsyncClient,
) -> bool:
    if _text(attachment.get("fileUrl")):
        return False

    source_local_path = _text(attachment.get("sourceLocalPath"))
    if not source_local_path:
        return False

    path = _resolve_allowed_attachment_path(source_local_path, source_channel)
    if path is None:
        logger.warning("ProductionIssue attachment path is not allowed or unavailable: %s", source_local_path)
        return False

    try:
        content = path.read_bytes()
    except OSError as exc:
        logger.warning("ProductionIssue attachment read failed path=%s err=%s", path, exc)
        return False

    upload_info = _detect_upload_type(path, content)
    if upload_info is None:
        logger.warning("ProductionIssue attachment is not a supported file path=%s", path)
        return False
    mime_type, extension, upload_action = upload_info

    upload_name = _filename_with_extension(path.name, extension)
    upload_url = urljoin(upstream_base_url.rstrip("/") + "/", f"Common/{upload_action}")
    response = await client.post(
        upload_url,
        headers={"Authorization": authorization},
        files={"file": (upload_name, content, mime_type)},
    )
    if response.status_code >= 400:
        logger.warning("ProductionIssue attachment upload failed status=%s path=%s", response.status_code, path)
        return False

    try:
        result = response.json()
    except ValueError:
        logger.warning("ProductionIssue attachment upload returned non-json path=%s body=%s", path, response.text[:300])
        return False

    is_success = result.get("isSuccess")
    if is_success is None:
        is_success = result.get("IsSuccess")
    file_url = result.get("backResult") or result.get("BackResult")
    if not is_success or not file_url:
        logger.warning("ProductionIssue attachment upload unsuccessful path=%s body=%s", path, str(result)[:500])
        return False

    attachment["fileUrl"] = str(file_url)
    attachment["fileName"] = upload_name
    attachment["fileSize"] = int(len(content))
    attachment["mimeType"] = _text(attachment.get("mimeType")) or mime_type
    attachment["sourceType"] = _text(attachment.get("sourceType")) or source_channel
    return True


def _resolve_allowed_attachment_path(source_local_path: str, source_channel: str) -> Path | None:
    try:
        path = Path(source_local_path).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    if not path.is_file():
        return None

    roots = _allowed_attachment_roots(source_channel)
    if not any(_is_relative_to(path, root) for root in roots):
        return None
    return path


def _allowed_attachment_roots(source_channel: str) -> list[Path]:
    channel = (source_channel or "dingtalk").strip() or "dingtalk"
    channel_dir = resolve_channel_media_dir(channel).resolve()
    roots = [channel_dir, channel_dir.parent]
    return list(dict.fromkeys(roots))


def _detect_upload_type(path: Path, content: bytes) -> tuple[str, str, str] | None:
    sniffed = _sniff_image_type(content)
    if sniffed:
        mime_type, extension = sniffed
        return mime_type, extension, "UploadImage"

    extension = _normalized_image_extension(path.suffix) or path.suffix.lower()
    if extension not in _FILE_EXTENSIONS:
        return None

    mime_type, _ = mimetypes.guess_type(str(path))
    if not mime_type:
        mime_type = "application/octet-stream"

    if mime_type.startswith("image/") and extension in _IMAGE_EXTENSIONS:
        return mime_type, extension, "UploadImage"

    return mime_type, extension, "UploadFile"


def _sniff_image_type(content: bytes) -> tuple[str, str] | None:
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if content.startswith(b"BM"):
        return "image/bmp", ".bmp"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None


def _normalized_image_extension(extension: str | None) -> str | None:
    if not extension:
        return None
    lowered = extension.lower()
    if lowered in {".jpe", ".jpeg"}:
        return ".jpg"
    return lowered


def _filename_with_extension(filename: str, extension: str) -> str:
    path = Path(filename)
    stem = path.stem or "image"
    current_ext = _normalized_image_extension(path.suffix)
    if current_ext == extension:
        return path.name
    return f"{stem}{extension}"


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _source_channel_from_json_body(body: bytes) -> str:
    if not body:
        return ""
    try:
        payload = json.loads(body)
    except (TypeError, ValueError, UnicodeDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    value = payload.get("sourceChannel") or payload.get("source_channel")
    return value if isinstance(value, str) else ""
