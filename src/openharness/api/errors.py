"""API error types for OpenHarness."""

from __future__ import annotations

import asyncio
import json
from typing import Any


class OpenHarnessApiError(RuntimeError):
    """Base class for upstream API failures."""


class AuthenticationFailure(OpenHarnessApiError):
    """Raised when the upstream service rejects the provided credentials."""


class RateLimitFailure(OpenHarnessApiError):
    """Raised when the upstream service rejects the request due to rate limits."""


class RequestFailure(OpenHarnessApiError):
    """Raised for generic request or transport failures."""


_TIMEOUT_CLASS_NAMES = {
    "TimeoutError",
    "TimeoutException",
    "ReadTimeout",
    "WriteTimeout",
    "ConnectTimeout",
    "PoolTimeout",
    "APITimeoutError",
}


def describe_exception(exc: BaseException, *, fallback: str = "upstream API request failed") -> str:
    """Return a user-facing description for exceptions that may stringify empty."""
    return _describe_exception(exc, fallback=fallback, seen=set())


def _describe_exception(
    exc: BaseException,
    *,
    fallback: str,
    seen: set[int],
) -> str:
    if id(exc) in seen:
        return fallback
    seen.add(id(exc))

    parts: list[str] = []
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    response_status = getattr(response, "status_code", None)
    if status is None and response_status is not None:
        status = response_status

    raw = str(exc).strip()
    if raw:
        parts.append(raw)

    payload_message = _extract_payload_message(getattr(exc, "body", None))
    if not payload_message and response is not None:
        payload_message = _extract_payload_message(getattr(response, "text", None))
    if payload_message:
        parts.append(payload_message)

    if status is not None:
        status_text = f"HTTP {status}"
        if not any(status_text in part for part in parts):
            parts.insert(0, status_text)

    for nested in (getattr(exc, "__cause__", None), getattr(exc, "__context__", None)):
        if isinstance(nested, BaseException):
            nested_message = _describe_exception(nested, fallback="", seen=seen).strip()
            if nested_message and nested_message not in parts:
                parts.append(nested_message)
                break

    message = "; ".join(_dedupe(parts)).strip()
    if _looks_like_timeout(exc):
        timeout_message = (
            "Request timed out while waiting for the model provider to stream a response. "
            "Long HTML or file-generation tasks may need more time; increase "
            "OPENHARNESS_TIMEOUT if this repeats."
        )
        if message:
            return _truncate(f"{timeout_message} ({message})")
        return timeout_message

    if message:
        return _truncate(message)

    class_name = exc.__class__.__name__
    if class_name and class_name != "Exception":
        return f"{fallback} ({class_name})"
    return fallback


def _extract_payload_message(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", "replace")
    if isinstance(payload, dict):
        return _message_from_mapping(payload)
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return ""
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(parsed, dict):
            return _message_from_mapping(parsed) or text
        return text
    return ""


def _message_from_mapping(payload: dict[str, Any]) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("code")
        if isinstance(message, str) and message.strip():
            return message.strip()
    elif isinstance(error, str) and error.strip():
        return error.strip()

    for key in ("message", "detail", "code"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _looks_like_timeout(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return True
    name = exc.__class__.__name__
    if name in _TIMEOUT_CLASS_NAMES or "Timeout" in name:
        return True
    for nested in (getattr(exc, "__cause__", None), getattr(exc, "__context__", None)):
        if isinstance(nested, BaseException) and _looks_like_timeout(nested):
            return True
    return False


def _dedupe(parts: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for part in parts:
        normalized = " ".join(part.split())
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _truncate(message: str, limit: int = 2000) -> str:
    if len(message) <= limit:
        return message
    return message[: limit - 3].rstrip() + "..."
