from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

from openharness.tools.internal_api_request_tool import (
    InternalApiRequestInput,
    InternalApiRequestTool,
    _coerce_json_body,
    _headers_with_channel_context,
    _is_allowed_agent_gateway_target,
    _is_allowed_board_memo_target,
    _is_allowed_async_board_target,
    _json_body_with_channel_context,
    _normalize_method,
    _params_with_channel_context,
    _request_with_auth_per_redirect,
)
from openharness.tools.base import ToolExecutionContext
from openharness.config.settings import Settings, load_settings
from openharness.utils.internal_api_auth import (
    apply_internal_api_auth,
    normalized_origin,
    resolve_agent_api_base_url,
    resolve_internal_api_url,
)
from openharness.utils.network_guard import NetworkGuardError
import httpx
import pytest


def test_internal_api_env_overrides_config(tmp_path, monkeypatch):
    config_path = tmp_path / "settings.json"
    config_path.write_text(
        """
{
  "internal_api": {
    "base_url": "http://from-file.example",
    "allowlist": ["http://from-file.example"]
  }
}
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("OHMO_INTERNAL_API_BASE_URL", "http://192.168.6.123:2415")
    monkeypatch.setenv("OHMO_AGENT_API_BASE_URL", "http://192.168.6.123:3416")
    monkeypatch.setenv("OHMO_INTERNAL_API_DINGTALK_TOKEN", "service-token")
    monkeypatch.setenv(
        "OHMO_INTERNAL_API_ALLOWLIST",
        "http://192.168.6.123:2415,http://localhost:2415",
    )

    settings = load_settings(config_path)

    assert settings.internal_api.base_url == "http://192.168.6.123:2415"
    assert settings.internal_api.agent_api_base_url == "http://192.168.6.123:3416"
    assert settings.internal_api.dingtalk_token == "service-token"
    assert settings.internal_api.allowlist == [
        "http://192.168.6.123:2415",
        "http://localhost:2415",
    ]


def test_apply_internal_api_auth_for_allowlisted_relative_url():
    settings = Settings(
        internal_api={
            "base_url": "http://192.168.6.123:2415",
            "allowlist": ["http://192.168.6.123:2415"],
        }
    )

    url, headers, attached = apply_internal_api_auth(
        "/api/demo",
        {},
        metadata={"hsjm_auth": {"token": "abc"}},
        settings=settings,
    )

    assert url == "http://192.168.6.123:2415/api/demo"
    assert headers["Authorization"] == "Bearer abc"
    assert attached is True


def test_apply_internal_api_auth_for_allowlisted_agent_absolute_url():
    settings = Settings(
        internal_api={
            "base_url": "http://192.168.6.123:2416",
            "allowlist": ["http://192.168.6.123:2416"],
        }
    )

    url, headers, attached = apply_internal_api_auth(
        "http://192.168.6.123:2416/agent/api/v1/ProductionIssue/Insert",
        {},
        metadata={"hsjm_auth": {"token": "web-token"}},
        settings=settings,
    )

    assert url == "http://192.168.6.123:2416/agent/api/v1/ProductionIssue/Insert"
    assert headers["Authorization"] == "Bearer web-token"
    assert attached is True


def test_apply_internal_api_auth_for_configured_relative_agent_url():
    settings = Settings(
        internal_api={
            "base_url": "http://api.internal:2415",
            "agent_api_base_url": "https://gateway.internal:3443/agent/api/v1",
            "allowlist": [],
        }
    )

    url, headers, attached = apply_internal_api_auth(
        "/agent/api/v1/BoardMemo/GetProfiles",
        {},
        metadata={"hsjm_auth": {"token": "web-token"}},
        settings=settings,
    )

    assert url == "https://gateway.internal:3443/agent/api/v1/BoardMemo/GetProfiles"
    assert headers["Authorization"] == "Bearer web-token"
    assert attached is True


def test_internal_api_request_headers_include_channel_context():
    headers = _headers_with_channel_context(
        {
            "X-OHMO-Source-Channel": "web",
            "X-OHMO-Source-Sender-Name": "张三",
        },
        metadata={
            "channel_context": {
                "channel": "dingtalk",
                "sender_name": "俞晨",
            }
        },
    )

    assert headers["X-OHMO-Source-Channel"] == "dingtalk"
    assert unquote(headers["X-OHMO-Source-Sender-Name"]) == "俞晨"


















def test_internal_api_request_coerces_json_string_body():
    body = _coerce_json_body('{"title":"测试","sourceChannel":"dingtalk"}', None)

    assert body == {"title": "测试", "sourceChannel": "dingtalk"}


def test_internal_api_request_accepts_body_alias_for_json():
    body = _coerce_json_body(None, '{"title":"测试"}')

    assert body == {"title": "测试"}


def test_internal_api_request_defaults_production_issue_write_to_post_with_body():
    method = _normalize_method(
        "GET",
        "http://192.168.6.123:2416/agent/api/v1/ProductionIssue/Insert",
        {"title": "测试"},
    )

    assert method == "POST"


def test_async_board_recognition_forces_draft_key_and_persisted_attachments():
    body = _json_body_with_channel_context(
        "http://192.168.6.146:2416/agent/api/v1/BoardMemo/Insert?attempt=1",
        {
            "contentMd": "识别结果",
            "status": "confirmed",
            "businessDomain": "evil-domain",
            "sourceChannel": "evil-channel",
            "sourceSenderId": "evil-sender",
            "idempotencyKey": "model-invented-key",
            "attachments": [{"sourceLocalPath": "/tmp/duplicate.jpg"}],
        },
        metadata={
            "channel_context": {
                "channel": "dingtalk",
                "chat_id": "staff-1",
                "source_sender_id": "staff-1",
                "message_id": "msg-1",
                "sender_name": "张三",
                "conversation_id": "conv-1",
                "session_key": "board-recognition:101",
                "async_board_recognition": True,
                "board_batch_id": "101",
                "board_business_domain": "project",
                "board_idempotency_key": "board-image-recognition:101:insert",
                "attachment_urls": [
                    {
                        "fileUrl": "/WebUp_Files/images/one.jpg",
                        "fileName": "one.jpg",
                        "contentType": "image/jpeg",
                        "sizeBytes": 123,
                    },
                    {
                        "file_url": "/WebUp_Files/images/two.png",
                        "file_name": "two.png",
                    },
                ],
            }
        },
    )

    assert body["status"] == "draft"
    assert body["businessDomain"] == "project"
    assert body["idempotencyKey"] == "board-image-recognition:101:insert"
    assert body["sourceChannel"] == "dingtalk"
    assert body["sourceSenderId"] == "staff-1"
    assert body["sourceSenderName"] == "张三"
    assert body["sourceMessageId"] == "msg-1"
    assert body["sourceConversationId"] == "conv-1"
    assert body["sourceSessionKey"] == "board-recognition:101"
    assert body["attachments"] == [
        {
            "fileName": "one.jpg",
            "fileUrl": "/WebUp_Files/images/one.jpg",
            "sourceType": "dingtalk",
            "mimeType": "image/jpeg",
            "fileSize": 123,
        },
        {
            "fileName": "two.png",
            "fileUrl": "/WebUp_Files/images/two.png",
            "sourceType": "dingtalk",
        },
    ]


def test_relative_agent_route_uses_explicit_agent_base_url():
    settings = Settings(
        internal_api={
            "base_url": "http://api.example:2415",
            "agent_api_base_url": "https://gateway.example:3416/agent/api/v1",
            "allowlist": [],
        }
    )

    assert resolve_internal_api_url(
        "/agent/api/v1/BoardMemo/GetProfiles?businessDomain=project",
        settings,
    ) == "https://gateway.example:3416/agent/api/v1/BoardMemo/GetProfiles?businessDomain=project"


def test_agent_base_url_must_be_configured_explicitly():
    settings = Settings(
        internal_api={
            "base_url": "http://192.168.6.123:2415",
            "allowlist": [],
        }
    )

    assert resolve_agent_api_base_url(settings) == ""
    assert resolve_internal_api_url(
        "/agent/api/v1/BoardMemo/Insert",
        settings,
    ) == "/agent/api/v1/BoardMemo/Insert"


def test_async_board_recognition_only_allows_configured_board_memo_targets(monkeypatch):
    monkeypatch.setattr(
        "openharness.tools.internal_api_request_tool.load_settings",
        lambda: Settings(
            internal_api={
                "base_url": "http://192.168.6.146:2415",
                "agent_api_base_url": "http://192.168.6.146:3416",
                "allowlist": ["http://192.168.6.146:2415"],
            }
        ),
    )

    assert _is_allowed_async_board_target(
        "http://192.168.6.146:3416/agent/api/v1/BoardMemo/GetProfiles?businessDomain=project"
    )
    assert _is_allowed_async_board_target(
        "http://192.168.6.146:3416/agent/api/v1/BoardMemo/Insert?attempt=1"
    )
    assert not _is_allowed_async_board_target(
        "https://evil.example/agent/api/v1/BoardMemo/Insert"
    )
    assert not _is_allowed_async_board_target(
        "http://192.168.6.146:3416/agent/api/v1/BoardMemo/Delete"
    )
    assert not _is_allowed_async_board_target(
        "http://192.168.6.146:2415/BoardMemo/Insert"
    )


def test_all_board_memo_calls_require_exact_configured_gateway(monkeypatch):
    monkeypatch.setattr(
        "openharness.tools.internal_api_request_tool.load_settings",
        lambda: Settings(
            internal_api={
                "base_url": "http://api.internal:2415",
                "agent_api_base_url": "https://gateway.internal:3443/proxy/agent/api/v1",
                "allowlist": [],
            }
        ),
    )

    assert _is_allowed_board_memo_target(
        "https://gateway.internal:3443/proxy/agent/api/v1/BoardMemo/AppendItem"
    )
    assert _is_allowed_board_memo_target(
        "https://gateway.internal:3443/proxy/agent/api/v1/BoardMemo/GetSummaryListPaged"
    )
    assert not _is_allowed_board_memo_target(
        "http://gateway.internal:3443/proxy/agent/api/v1/BoardMemo/AppendItem"
    )
    assert not _is_allowed_board_memo_target(
        "https://gateway.internal:3444/proxy/agent/api/v1/BoardMemo/AppendItem"
    )
    assert not _is_allowed_board_memo_target(
        "https://gateway.internal:3443/agent/api/v1/BoardMemo/AppendItem"
    )
    assert not _is_allowed_board_memo_target(
        "https://gateway.internal:3443/proxy/agent/api/v1/BoardMemo/Unknown"
    )
    assert _is_allowed_board_memo_target(
        "https://gateway.internal:3443/proxy/agent/api/v1/%42oardMemo/Insert"
    )
    assert _is_allowed_board_memo_target(
        "https://gateway.internal:3443/proxy/agent/api/v1/BoardMemo%2FInsert"
    )
    assert not _is_allowed_board_memo_target(
        "https://evil.internal/proxy/agent/api/v1/%42oardMemo/Insert"
    )


@pytest.mark.asyncio
async def test_internal_api_tool_rejects_unknown_board_memo_action_before_network(monkeypatch):
    monkeypatch.setattr(
        "openharness.tools.internal_api_request_tool.load_settings",
        lambda: Settings(
            internal_api={
                "base_url": "http://api.internal:2415",
                "agent_api_base_url": "https://gateway.internal:3443/proxy/agent/api/v1",
                "allowlist": [],
            }
        ),
    )

    result = await InternalApiRequestTool().execute(
        InternalApiRequestInput(
            method="GET",
            url="https://gateway.internal:3443/proxy/agent/api/v1/BoardMemo/Unknown",
        ),
        ToolExecutionContext(cwd=Path.cwd()),
    )

    assert result.is_error is True
    assert "supported action" in result.output


def test_all_agent_routes_require_exact_configured_gateway(monkeypatch):
    monkeypatch.setattr(
        "openharness.tools.internal_api_request_tool.load_settings",
        lambda: Settings(
            internal_api={
                "base_url": "http://api.internal:2415",
                "agent_api_base_url": "https://gateway.internal:3443/proxy/agent/api/v1",
                "allowlist": [],
            }
        ),
    )

    assert _is_allowed_agent_gateway_target(
        "https://gateway.internal:3443/proxy/agent/api/v1/Health"
    )
    assert not _is_allowed_agent_gateway_target(
        "https://evil.internal/proxy/agent/api/v1/Health"
    )
    assert not _is_allowed_agent_gateway_target(
        "https://gateway.internal:3443/agent/api/v1/Health"
    )


@pytest.mark.parametrize(
    "agent_base_url",
    [
        "https://gateway.internal:3443/agent/api/v1?target=evil",
        "https://gateway.internal:3443/agent/api/v1#fragment",
        "https://user:password@gateway.internal:3443/agent/api/v1",
        "https://gateway.internal:99999/agent/api/v1",
        "https://gateway.internal:3443/agent/../api/v1",
    ],
)
def test_invalid_agent_base_url_fails_closed(monkeypatch, agent_base_url):
    monkeypatch.setattr(
        "openharness.tools.internal_api_request_tool.load_settings",
        lambda: Settings(
            internal_api={
                "base_url": "http://api.internal:2415",
                "agent_api_base_url": agent_base_url,
                "allowlist": [],
            }
        ),
    )

    assert not _is_allowed_agent_gateway_target(
        "https://gateway.internal:3443/agent/api/v1/BoardMemo/GetProfiles"
    )


def test_agent_relative_url_cannot_escape_configured_origin():
    settings = Settings(
        internal_api={
            "base_url": "http://api.internal:2415",
            "agent_api_base_url": "https://gateway.internal:3443/agent/api/v1",
            "allowlist": [],
        }
    )

    resolved = resolve_internal_api_url(
        "/agent/api/v1/https://evil.internal/BoardMemo/Insert",
        settings,
    )

    assert resolved.startswith("https://gateway.internal:3443/agent/api/v1/")
    assert "evil.internal" in resolved


@pytest.mark.asyncio
async def test_redirect_from_non_agent_url_to_wrong_board_memo_origin_is_rejected(monkeypatch):
    settings = Settings(
        internal_api={
            "base_url": "http://192.168.6.146:2415",
            "agent_api_base_url": "http://192.168.6.146:2416/agent/api/v1",
            "allowlist": [],
        }
    )
    monkeypatch.setattr(
        "openharness.tools.internal_api_request_tool.load_settings",
        lambda: settings,
    )
    monkeypatch.setattr(
        "openharness.tools.internal_api_request_tool.apply_internal_api_auth",
        lambda url, headers, metadata=None: (url, dict(headers or {}), False),
    )
    calls: list[str] = []

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, _method, url, **_kwargs):
            calls.append(url)
            return httpx.Response(
                302,
                headers={
                    "location": "http://192.168.6.147:2416/agent/api/v1/%42oardMemo/Insert"
                },
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(
        "openharness.tools.internal_api_request_tool.httpx.AsyncClient",
        FakeAsyncClient,
    )

    with pytest.raises(NetworkGuardError, match="agent API request left"):
        await _request_with_auth_per_redirect(
            method="GET",
            url="https://example.com/start",
            params=None,
            headers={},
            json_body=None,
            timeout=5,
            metadata={},
        )

    assert calls == ["https://example.com/start"]


def test_apply_internal_api_auth_downgrades_external_url_without_token():
    settings = Settings(
        internal_api={
            "base_url": "http://39.106.250.202:2415",
            "allowlist": ["http://39.106.250.202:2415"],
        }
    )

    url, headers, attached = apply_internal_api_auth(
        "https://example.com/api/demo",
        {},
        metadata={"hsjm_auth": {"token": "abc"}},
        settings=settings,
    )

    assert url == "https://example.com/api/demo"
    assert "Authorization" not in headers
    assert attached is False


def test_apply_internal_api_auth_does_not_override_explicit_authorization():
    settings = Settings(
        internal_api={
            "base_url": "http://39.106.250.202:2415",
            "allowlist": ["http://39.106.250.202:2415"],
        }
    )

    _url, headers, attached = apply_internal_api_auth(
        "http://39.106.250.202:2415/api/demo",
        {"Authorization": "Bearer explicit"},
        metadata={"hsjm_auth": {"token": "abc"}},
        settings=settings,
    )

    assert headers["Authorization"] == "Bearer explicit"
    assert attached is False


def test_normalized_origin_includes_default_port():
    assert normalized_origin("http://example.com/path") == "http://example.com:80"
    assert normalized_origin("https://example.com/path") == "https://example.com:443"
