"""Gateway models for ohmo."""

from __future__ import annotations

from pydantic import BaseModel, Field


class AlertingConfig(BaseModel):
    """Email alerting for operational failures (API outages, bot disconnects)."""

    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_ssl: bool = True
    smtp_use_tls: bool = False
    from_address: str = ""
    to_addresses: list[str] = Field(default_factory=list)
    cooldown_minutes: int = 30


class GatewayConfig(BaseModel):
    """Persistent gateway configuration."""

    provider_profile: str = "codex"
    enabled_channels: list[str] = Field(default_factory=list)
    session_routing: str = "chat-thread"
    send_progress: bool = True
    send_tool_hints: bool = True
    permission_mode: str = "default"
    sandbox_enabled: bool = False
    allow_remote_admin_commands: bool = False
    allowed_remote_admin_commands: list[str] = Field(default_factory=list)
    log_level: str = "INFO"
    channel_configs: dict[str, dict] = Field(default_factory=dict)
    alerting: AlertingConfig = Field(default_factory=AlertingConfig)


class GatewayState(BaseModel):
    """Runtime gateway status snapshot."""

    running: bool = False
    pid: int | None = None
    active_sessions: int = 0
    provider_profile: str = "codex"
    enabled_channels: list[str] = Field(default_factory=list)
    last_error: str | None = None

