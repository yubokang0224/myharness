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


class MesWorkspaceConfig(BaseModel):
    """Persistent MES workspace integration settings.

    The JWT signing secret is intentionally stored in the Xiaojin AI
    credential store instead of this JSON-serializable model.
    """

    enabled: bool = False
    issuer: str = ""
    audience: str = ""
    algorithm: str = "HS256"
    required_permission: str = "report.ai"
    api_base_url: str = ""
    permission_url: str = ""
    allow_anonymous_api: bool = False


class BoardRecognitionConfig(BaseModel):
    """Background whiteboard image recognition settings."""

    enabled: bool = False
    poll_seconds: float = 30
    lease_seconds: int = 600
    worker_id: str = ""
    request_timeout_seconds: float = 60


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
    mes: MesWorkspaceConfig = Field(default_factory=MesWorkspaceConfig)
    board_recognition: BoardRecognitionConfig = Field(default_factory=BoardRecognitionConfig)


class GatewayState(BaseModel):
    """Runtime gateway status snapshot."""

    running: bool = False
    pid: int | None = None
    active_sessions: int = 0
    provider_profile: str = "codex"
    enabled_channels: list[str] = Field(default_factory=list)
    last_error: str | None = None

