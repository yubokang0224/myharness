# -*- coding: utf-8 -*-
"""通道配置与扫码绑定相关的请求/响应数据模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DingTalkBotItemResponse(BaseModel):
    """单个钉钉机器人通道配置响应（密钥已脱敏）。"""

    name: str
    enabled: bool = True
    client_id: str = ""
    corp_id: str = ""
    client_secret_masked: str = ""
    has_secret: bool = False
    robot_code: str = ""
    default_agent: str = ""
    allow_from: list[str] = Field(default_factory=lambda: ["*"])
    defer_board_images: bool = False
    board_business_domain: str = ""
    board_batch_quiet_seconds: int = 20


class DingTalkBotUpsertRequest(BaseModel):
    """新增或更新钉钉机器人通道配置请求。"""

    name: str = Field(..., min_length=1, max_length=64)
    enabled: bool = True
    client_id: str = Field(..., min_length=1, max_length=256)
    corp_id: str = ""
    client_secret: str = ""
    robot_code: str = ""
    default_agent: str = ""
    allow_from: list[str] = Field(default_factory=lambda: ["*"])
    defer_board_images: bool = False
    board_business_domain: str = ""
    board_batch_quiet_seconds: int = Field(default=20, ge=5, le=300)


class DingTalkProbeRequest(BaseModel):
    """探测钉钉机器人凭证连通性请求。"""

    client_id: str = ""
    client_secret: str = ""
    bot_name: str = ""


class ChannelProbeResponse(BaseModel):
    """通道连通性测试结果。"""

    ok: bool
    latency_ms: int = 0
    checked_at: float
    error: str | None = None


class DingTalkQrGenerateRequest(BaseModel):
    """初始化钉钉扫码一键注册请求。"""

    bot_name: str = ""
    default_agent: str = ""
    defer_board_images: bool = False
    board_business_domain: str = ""


class DingTalkQrGenerateResponse(BaseModel):
    """钉钉扫码二维码生成结果。"""

    registration_id: str
    qrcode_url: str
    user_code: str
    expires_in: int = 600
    interval: int = 5


class DingTalkQrPollRequest(BaseModel):
    """轮询钉钉扫码注册结果请求。"""

    registration_id: str
    bot_name: str = ""
    default_agent: str = ""
    defer_board_images: bool = False
    board_business_domain: str = ""


class DingTalkQrPollResponse(BaseModel):
    """钉钉扫码轮询状态响应。"""

    status: str  # "waiting" | "success" | "expired" | "failed"
    message: str | None = None
    bot: DingTalkBotItemResponse | None = None


class FeishuChannelResponse(BaseModel):
    """飞书机器人通道配置响应（密钥已脱敏）。"""

    name: str = "default"
    enabled: bool = False
    app_id: str = ""
    app_secret_masked: str = ""
    has_secret: bool = False
    encrypt_key: str = ""
    verification_token: str = ""
    default_agent: str = ""
    allow_from: list[str] = Field(default_factory=lambda: ["*"])


class FeishuChannelUpdateRequest(BaseModel):
    """新增或更新飞书通道配置请求。"""

    name: str = "default"
    enabled: bool = False
    app_id: str = ""
    app_secret: str = ""
    encrypt_key: str = ""
    verification_token: str = ""
    default_agent: str = ""
    allow_from: list[str] = Field(default_factory=lambda: ["*"])


class FeishuProbeRequest(BaseModel):
    """探测飞书机器人凭证连通性请求。"""

    bot_name: str = ""
    app_id: str = ""
    app_secret: str = ""


class FeishuQrGenerateRequest(BaseModel):
    """初始化飞书扫码一键注册请求。"""

    bot_name: str = ""
    default_agent: str = ""
    allow_from: list[str] = Field(default_factory=lambda: ["*"])


class FeishuQrGenerateResponse(BaseModel):
    """飞书扫码二维码生成结果。"""

    registration_id: str
    qrcode_url: str
    user_code: str
    expires_in: int = 600
    interval: int = 5


class FeishuQrPollRequest(BaseModel):
    """轮询飞书扫码注册结果请求。"""

    registration_id: str
    bot_name: str = ""
    default_agent: str = ""
    allow_from: list[str] = Field(default_factory=lambda: ["*"])


class FeishuQrPollResponse(BaseModel):
    """飞书扫码轮询状态响应。"""

    status: str  # "waiting" | "success" | "expired" | "failed"
    message: str | None = None
    feishu: FeishuChannelResponse | None = None


class WeixinChannelResponse(BaseModel):
    """个人微信（iLink）通道配置响应（Token 已脱敏）。"""

    name: str = "weixin-bot"
    enabled: bool = False
    account_id: str = ""
    token_masked: str = ""
    has_token: bool = False
    base_url: str = "https://ilinkai.weixin.qq.com"
    user_id: str = ""
    default_agent: str = ""
    allow_from: list[str] = Field(default_factory=lambda: ["*"])


class WeixinChannelUpdateRequest(BaseModel):
    """新增或更新个人微信通道配置请求。"""

    name: str = "weixin-bot"
    enabled: bool = False
    account_id: str = ""
    token: str = ""
    base_url: str = "https://ilinkai.weixin.qq.com"
    user_id: str = ""
    default_agent: str = ""
    allow_from: list[str] = Field(default_factory=lambda: ["*"])


class WeixinProbeRequest(BaseModel):
    """探测个人微信 iLink 凭证连通性请求。"""

    bot_name: str = ""
    token: str = ""
    base_url: str = "https://ilinkai.weixin.qq.com"


class WeixinQrGenerateRequest(BaseModel):
    """初始化个人微信扫码绑定请求。"""

    bot_name: str = ""
    default_agent: str = ""
    allow_from: list[str] = Field(default_factory=lambda: ["*"])


class WeixinQrGenerateResponse(BaseModel):
    """个人微信扫码二维码生成结果。"""

    registration_id: str
    qrcode_url: str
    qrcode_token: str = ""
    expires_in: int = 300
    interval: int = 2


class WeixinQrPollRequest(BaseModel):
    """轮询个人微信扫码结果请求。"""

    registration_id: str
    bot_name: str = ""
    default_agent: str = ""
    allow_from: list[str] = Field(default_factory=lambda: ["*"])


class WeixinQrPollResponse(BaseModel):
    """个人微信扫码轮询状态响应。"""

    status: str  # "waiting" | "scanned" | "success" | "expired" | "failed"
    message: str | None = None
    weixin: WeixinChannelResponse | None = None


class WecomChannelResponse(BaseModel):
    """企业微信智能机器人通道配置响应（密钥已脱敏）。"""

    name: str = "wecom-bot"
    enabled: bool = False
    bot_id: str = ""
    secret_masked: str = ""
    has_secret: bool = False
    ws_url: str = "wss://openws.work.weixin.qq.com"
    default_agent: str = ""
    allow_from: list[str] = Field(default_factory=lambda: ["*"])


class WecomChannelUpdateRequest(BaseModel):
    """新增或更新企业微信智能机器人通道配置请求。"""

    name: str = "wecom-bot"
    enabled: bool = False
    bot_id: str = ""
    secret: str = ""
    ws_url: str = "wss://openws.work.weixin.qq.com"
    default_agent: str = ""
    allow_from: list[str] = Field(default_factory=lambda: ["*"])


class WecomProbeRequest(BaseModel):
    """探测企业微信智能机器人凭证连通性请求。"""

    bot_name: str = ""
    bot_id: str = ""
    secret: str = ""
    ws_url: str = "wss://openws.work.weixin.qq.com"


class WecomQrGenerateRequest(BaseModel):
    """初始化企业微信扫码一键创建机器人请求。"""

    bot_name: str = ""
    default_agent: str = ""
    allow_from: list[str] = Field(default_factory=lambda: ["*"])


class WecomQrGenerateResponse(BaseModel):
    """企业微信扫码二维码生成结果。"""

    registration_id: str
    qrcode_url: str
    scode: str = ""
    expires_in: int = 300
    interval: int = 2


class WecomQrPollRequest(BaseModel):
    """轮询企业微信扫码创建结果请求。"""

    registration_id: str
    bot_name: str = ""
    default_agent: str = ""
    allow_from: list[str] = Field(default_factory=lambda: ["*"])


class WecomQrPollResponse(BaseModel):
    """企业微信扫码轮询状态响应。"""

    status: str  # "waiting" | "success" | "expired" | "failed"
    message: str | None = None
    wecom: WecomChannelResponse | None = None


class ChannelsOverviewResponse(BaseModel):
    """工作台通道配置总览响应。"""

    enabled_channels: list[str] = Field(default_factory=list)
    send_progress: bool = True
    send_tool_hints: bool = True
    dingtalk_bots: list[DingTalkBotItemResponse] = Field(default_factory=list)
    feishu_bots: list[FeishuChannelResponse] = Field(default_factory=list)
    feishu: FeishuChannelResponse = Field(default_factory=FeishuChannelResponse)
    weixin_bots: list[WeixinChannelResponse] = Field(default_factory=list)
    weixin: WeixinChannelResponse = Field(default_factory=WeixinChannelResponse)
    wecom_bots: list[WecomChannelResponse] = Field(default_factory=list)
    wecom: WecomChannelResponse = Field(default_factory=WecomChannelResponse)
