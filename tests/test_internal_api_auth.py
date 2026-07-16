from __future__ import annotations

from openharness.tools.internal_api_request_tool import (
    _coerce_json_body,
    _headers_with_channel_context,
    _json_body_with_channel_context,
    _normalize_method,
)
from openharness.config.settings import Settings, load_settings
from openharness.utils.internal_api_auth import apply_internal_api_auth, normalized_origin


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
    monkeypatch.setenv(
        "OHMO_INTERNAL_API_ALLOWLIST",
        "http://192.168.6.123:2415,http://localhost:2415",
    )

    settings = load_settings(config_path)

    assert settings.internal_api.base_url == "http://3192.168.6.123:2415"
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


def test_internal_api_request_headers_include_channel_context():
    headers = _headers_with_channel_context(
        {},
        metadata={"channel_context": {"channel": "dingtalk"}},
    )

    assert headers["X-OHMO-Source-Channel"] == "dingtalk"


def test_internal_api_request_body_includes_production_issue_channel_context():
    body = _json_body_with_channel_context(
        "http://192.168.6.123:2416/agent/api/v1/ProductionIssue/Insert",
        {"title": "合同号识别错误"},
        metadata={
            "channel_context": {
                "channel": "dingtalk",
                "chat_id": "chat-1",
                "sender_name": "俞晨",
                "message_id": "msg-1",
                "conversation_id": "conv-1",
                "session_key": "dingtalk:dingtalk-bot:生产助手:chat-1:sender-1",
                "attachment_paths": ["/tmp/receipt.png"],
            }
        },
    )

    assert body["sourceChannel"] == "dingtalk"
    assert body["sourceChatId"] == "chat-1"
    assert body["sourceSenderName"] == "俞晨"
    assert body["sourceMessageId"] == "msg-1"
    assert body["sourceConversationId"] == "conv-1"
    assert body["idempotencyKey"] == "production-site-issue:msg-1:insert"
    assert body["reporterName"] == "俞晨"
    assert "sourceSenderId" not in body
    assert body["attachments"] == [
        {
            "fileName": "receipt.png",
            "sourceLocalPath": "/tmp/receipt.png",
            "sourceType": "dingtalk",
        }
    ]


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
