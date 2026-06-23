from __future__ import annotations

from openharness.tools.internal_api_request_tool import _headers_with_channel_context
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
    monkeypatch.setenv("OHMO_INTERNAL_API_BASE_URL", "http://39.106.250.202:2415")
    monkeypatch.setenv(
        "OHMO_INTERNAL_API_ALLOWLIST",
        "http://39.106.250.202:2415,http://localhost:2415",
    )

    settings = load_settings(config_path)

    assert settings.internal_api.base_url == "http://39.106.250.202:2415"
    assert settings.internal_api.allowlist == [
        "http://39.106.250.202:2415",
        "http://localhost:2415",
    ]


def test_apply_internal_api_auth_for_allowlisted_relative_url():
    settings = Settings(
        internal_api={
            "base_url": "http://39.106.250.202:2415",
            "allowlist": ["http://39.106.250.202:2415"],
        }
    )

    url, headers, attached = apply_internal_api_auth(
        "/api/demo",
        {},
        metadata={"hsjm_auth": {"token": "abc"}},
        settings=settings,
    )

    assert url == "http://39.106.250.202:2415/api/demo"
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
