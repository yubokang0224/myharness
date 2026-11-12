# -*- coding: utf-8 -*-
"""通道连接管理与钉钉扫码一键注册路由。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import re
import secrets
import threading
import time
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, status

from ohmo.gateway.config import load_gateway_config, save_gateway_config
from ohmo.gateway.dependencies import _RuntimeState, get_runtime, require_agent_admin
from ohmo.gateway.schemas.channels import (
    ChannelProbeResponse,
    ChannelsOverviewResponse,
    DingTalkBotItemResponse,
    DingTalkBotUpsertRequest,
    DingTalkProbeRequest,
    DingTalkQrGenerateRequest,
    DingTalkQrGenerateResponse,
    DingTalkQrPollRequest,
    DingTalkQrPollResponse,
    FeishuChannelResponse,
    FeishuChannelUpdateRequest,
    FeishuProbeRequest,
    FeishuQrGenerateRequest,
    FeishuQrGenerateResponse,
    FeishuQrPollRequest,
    FeishuQrPollResponse,
    WecomChannelResponse,
    WecomChannelUpdateRequest,
    WecomProbeRequest,
    WecomQrGenerateRequest,
    WecomQrGenerateResponse,
    WecomQrPollRequest,
    WecomQrPollResponse,
    WeixinChannelResponse,
    WeixinChannelUpdateRequest,
    WeixinProbeRequest,
    WeixinQrGenerateRequest,
    WeixinQrGenerateResponse,
    WeixinQrPollRequest,
    WeixinQrPollResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/channels", tags=["channels"])

_DINGTALK_OAPI_BASE = "https://oapi.dingtalk.com"
_DINGTALK_API_TOKEN_URL = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
_DINGTALK_MAX_EXPIRES_IN = 2 * 60 * 60
_DINGTALK_MAX_SESSIONS = 128
_FEISHU_ACCOUNTS_BASE = "https://accounts.feishu.cn"
_LARK_ACCOUNTS_BASE = "https://accounts.larksuite.com"
_FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
_WEIXIN_ILINK_BASE = "https://ilinkai.weixin.qq.com"
_WECOM_QR_BASE = "https://work.weixin.qq.com"
_WECOM_DEFAULT_WS_URL = "wss://openws.work.weixin.qq.com"
_BOT_NAME_INVALID_RE = re.compile(r'[:<>:"/\\|?*\x00-\x1f]')


@dataclass
class _DingTalkRegistrationSession:
    registration_id: str
    device_code: str
    user_code: str
    qrcode_url: str
    expires_at: float
    interval: int
    bot_name: str = ""
    default_agent: str = ""
    defer_board_images: bool = False
    board_business_domain: str = ""
    completed_bot: DingTalkBotItemResponse | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class _FeishuRegistrationSession:
    registration_id: str
    device_code: str
    user_code: str
    qrcode_url: str
    expires_at: float
    interval: int
    base_url: str = _FEISHU_ACCOUNTS_BASE
    bot_name: str = ""
    default_agent: str = ""
    allow_from: list[str] = field(default_factory=lambda: ["*"])
    completed_feishu: FeishuChannelResponse | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class _WeixinRegistrationSession:
    registration_id: str
    qrcode_token: str
    qrcode_url: str
    expires_at: float
    interval: int = 2
    bot_name: str = ""
    default_agent: str = ""
    allow_from: list[str] = field(default_factory=lambda: ["*"])
    last_status: str = "waiting"
    poll_task: asyncio.Task[dict[str, Any]] | None = None
    completed_weixin: WeixinChannelResponse | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class _WecomRegistrationSession:
    registration_id: str
    scode: str
    qrcode_url: str
    expires_at: float
    interval: int = 2
    bot_name: str = ""
    default_agent: str = ""
    allow_from: list[str] = field(default_factory=lambda: ["*"])
    completed_wecom: WecomChannelResponse | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_dingtalk_qr_sessions: dict[str, _DingTalkRegistrationSession] = {}
_feishu_qr_sessions: dict[str, _FeishuRegistrationSession] = {}
_weixin_qr_sessions: dict[str, _WeixinRegistrationSession] = {}
_wecom_qr_sessions: dict[str, _WecomRegistrationSession] = {}


def _mask_secret(raw: str) -> str:
    val = (raw or "").strip()
    if not val:
        return ""
    if len(val) <= 8:
        return val[:2] + "****"
    return f"{val[:4]}****{val[-4:]}"


def _validate_bot_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned or cleaned in {".", ".."} or _BOT_NAME_INVALID_RE.search(cleaned):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="机器人名称不能为空，且不能包含冒号或路径特殊字符",
        )
    return cleaned


def _normalize_dingtalk_bots_from_config(dingtalk_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """从 gateway.json 的 channel_configs.dingtalk 中提取归一化后的机器人字典列表。"""
    raw_bots = dingtalk_cfg.get("bots")
    bots: list[dict[str, Any]] = []
    if isinstance(raw_bots, list) and raw_bots:
        for item in raw_bots:
            if isinstance(item, dict) and str(item.get("name") or "").strip():
                bots.append(dict(item))
        return bots

    # 兼容旧版单机器人顶层配置
    legacy_client_id = str(dingtalk_cfg.get("client_id") or "").strip()
    legacy_secret = str(dingtalk_cfg.get("client_secret") or "").strip()
    if legacy_client_id or legacy_secret:
        bots.append(
            {
                "name": str(dingtalk_cfg.get("name") or "default").strip() or "default",
                "enabled": bool(dingtalk_cfg.get("enabled", True)),
                "client_id": legacy_client_id,
                "corp_id": str(dingtalk_cfg.get("corp_id") or "").strip(),
                "client_secret": legacy_secret,
                "robot_code": str(dingtalk_cfg.get("robot_code") or "").strip(),
                "default_agent": str(dingtalk_cfg.get("default_agent") or "").strip(),
                "allow_from": list(dingtalk_cfg.get("allow_from") or ["*"]),
                "defer_board_images": bool(dingtalk_cfg.get("defer_board_images", False)),
                "board_business_domain": str(
                    dingtalk_cfg.get("board_business_domain") or ""
                ).strip(),
                "board_batch_quiet_seconds": int(
                    dingtalk_cfg.get("board_batch_quiet_seconds") or 20
                ),
            }
        )
    return bots


def _bot_dict_to_response(bot: dict[str, Any]) -> DingTalkBotItemResponse:
    secret = str(bot.get("client_secret") or "").strip()
    allow_from = bot.get("allow_from")
    if not isinstance(allow_from, list) or not allow_from:
        allow_from = ["*"]
    quiet_seconds = int(bot.get("board_batch_quiet_seconds") or 20)
    quiet_seconds = max(5, min(quiet_seconds, 300))
    return DingTalkBotItemResponse(
        name=str(bot.get("name") or "default").strip(),
        enabled=bool(bot.get("enabled", True)),
        client_id=str(bot.get("client_id") or "").strip(),
        corp_id=str(bot.get("corp_id") or "").strip(),
        client_secret_masked=_mask_secret(secret),
        has_secret=bool(secret),
        robot_code=str(bot.get("robot_code") or "").strip(),
        default_agent=str(bot.get("default_agent") or "").strip(),
        allow_from=[str(x).strip() for x in allow_from if str(x).strip()] or ["*"],
        defer_board_images=bool(bot.get("defer_board_images", False)),
        board_business_domain=str(bot.get("board_business_domain") or "").strip(),
        board_batch_quiet_seconds=quiet_seconds,
    )


def _normalize_feishu_bots_from_config(feishu_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """从 gateway.json 的 channel_configs.feishu 中提取归一化后的飞书机器人列表。"""
    raw_bots = feishu_cfg.get("bots")
    bots: list[dict[str, Any]] = []
    if isinstance(raw_bots, list) and raw_bots:
        for item in raw_bots:
            if isinstance(item, dict) and (
                str(item.get("name") or "").strip()
                or str(item.get("app_id") or "").strip()
            ):
                bots.append(dict(item))
        return bots

    legacy_app_id = str(feishu_cfg.get("app_id") or "").strip()
    legacy_secret = str(feishu_cfg.get("app_secret") or "").strip()
    if legacy_app_id or legacy_secret:
        bots.append(
            {
                "name": str(feishu_cfg.get("name") or "feishu-bot").strip() or "feishu-bot",
                "enabled": bool(feishu_cfg.get("enabled", True)),
                "app_id": legacy_app_id,
                "app_secret": legacy_secret,
                "encrypt_key": str(feishu_cfg.get("encrypt_key") or "").strip(),
                "verification_token": str(
                    feishu_cfg.get("verification_token") or ""
                ).strip(),
                "default_agent": str(feishu_cfg.get("default_agent") or "").strip(),
                "allow_from": list(feishu_cfg.get("allow_from") or ["*"]),
            }
        )
    return bots


def _feishu_dict_to_response(
    feishu_cfg: dict[str, Any], *, channel_enabled: bool
) -> FeishuChannelResponse:
    secret = str(feishu_cfg.get("app_secret") or "").strip()
    allow_from = feishu_cfg.get("allow_from")
    if not isinstance(allow_from, list) or not allow_from:
        allow_from = ["*"]
    return FeishuChannelResponse(
        name=str(feishu_cfg.get("name") or "feishu-bot").strip() or "feishu-bot",
        enabled=bool(feishu_cfg.get("enabled", channel_enabled)),
        app_id=str(feishu_cfg.get("app_id") or "").strip(),
        app_secret_masked=_mask_secret(secret),
        has_secret=bool(secret),
        encrypt_key=str(feishu_cfg.get("encrypt_key") or "").strip(),
        verification_token=str(feishu_cfg.get("verification_token") or "").strip(),
        default_agent=str(feishu_cfg.get("default_agent") or "").strip(),
        allow_from=[str(x).strip() for x in allow_from if str(x).strip()] or ["*"],
    )


def _save_feishu_bots_to_workspace(
    workspace: str | None,
    bots: list[dict[str, Any]],
) -> ChannelsOverviewResponse:
    config = load_gateway_config(workspace)
    channel_configs = dict(config.channel_configs or {})
    feishu_cfg = dict(channel_configs.get("feishu") or {})
    feishu_cfg["bots"] = bots

    has_enabled_bot = any(bool(b.get("enabled", True)) and str(b.get("app_id") or "").strip() for b in bots)
    feishu_cfg["enabled"] = has_enabled_bot

    # 同步将第一个（或第一个启用的）飞书机器人镜像到顶层字段，确保向后兼容
    primary = next((b for b in bots if bool(b.get("enabled", True))), bots[0] if bots else None)
    if primary:
        feishu_cfg["name"] = str(primary.get("name") or "feishu-bot").strip()
        feishu_cfg["app_id"] = str(primary.get("app_id") or "").strip()
        feishu_cfg["app_secret"] = str(primary.get("app_secret") or "").strip()
        feishu_cfg["encrypt_key"] = str(primary.get("encrypt_key") or "").strip()
        feishu_cfg["verification_token"] = str(primary.get("verification_token") or "").strip()
        feishu_cfg["default_agent"] = str(primary.get("default_agent") or "").strip()
        feishu_cfg["allow_from"] = list(primary.get("allow_from") or ["*"])
    else:
        for k in ("app_id", "app_secret", "encrypt_key", "verification_token", "default_agent"):
            feishu_cfg[k] = ""

    channel_configs["feishu"] = feishu_cfg
    enabled_channels = list(config.enabled_channels or [])
    if has_enabled_bot and "feishu" not in enabled_channels:
        enabled_channels.append("feishu")
    elif not has_enabled_bot and "feishu" in enabled_channels:
        enabled_channels.remove("feishu")

    updated = config.model_copy(
        update={
            "enabled_channels": enabled_channels,
            "channel_configs": channel_configs,
        }
    )
    save_gateway_config(updated, workspace)
    _trigger_gateway_channels_reload(workspace)
    return _build_overview_response(updated)


def _trigger_gateway_channels_reload(workspace: str | None) -> None:
    """在真实工作区保存通道配置后，后台异步热重启网关消息通道进程以立即生效。"""
    if workspace is not None:
        # 显式传入临时目录（如单元测试）时不启动后台子进程
        return

    def _reload_worker() -> None:
        try:
            from ohmo.gateway.service import start_gateway_process, stop_gateway_process

            stop_gateway_process(workspace=workspace)
            cfg = load_gateway_config(workspace)
            if cfg.enabled_channels:
                pid = start_gateway_process(workspace=workspace)
                logger.info("Gateway channels auto-reloaded (pid=%s)", pid)
        except Exception:
            logger.debug("Auto-reload gateway channels skipped", exc_info=True)

    threading.Thread(target=_reload_worker, daemon=True).start()


def _save_dingtalk_bots_to_workspace(
    workspace: str | None,
    bots: list[dict[str, Any]],
) -> ChannelsOverviewResponse:
    config = load_gateway_config(workspace)
    channel_configs = dict(config.channel_configs or {})
    dingtalk_cfg = dict(channel_configs.get("dingtalk") or {})
    dingtalk_cfg["bots"] = bots
    # 清理旧版单机器人残留字段，统一由 bots 列表驱动
    for legacy_key in (
        "client_id",
        "client_secret",
        "robot_code",
        "corp_id",
        "default_agent",
        "defer_board_images",
        "board_business_domain",
        "board_batch_quiet_seconds",
    ):
        dingtalk_cfg.pop(legacy_key, None)

    has_enabled_bot = any(bool(b.get("enabled", True)) for b in bots)
    dingtalk_cfg["enabled"] = has_enabled_bot
    channel_configs["dingtalk"] = dingtalk_cfg

    enabled_channels = list(config.enabled_channels or [])
    if has_enabled_bot and "dingtalk" not in enabled_channels:
        enabled_channels.append("dingtalk")
    elif not has_enabled_bot and "dingtalk" in enabled_channels:
        enabled_channels.remove("dingtalk")

    # 如果任一机器人开启了白板照片异步识别，确保网关白板识别后台任务开关同步开启
    board_recognition = config.board_recognition
    if any(bool(b.get("defer_board_images", False)) for b in bots):
        board_recognition = board_recognition.model_copy(update={"enabled": True})

    updated = config.model_copy(
        update={
            "enabled_channels": enabled_channels,
            "channel_configs": channel_configs,
            "board_recognition": board_recognition,
        }
    )
    save_gateway_config(updated, workspace)
    _trigger_gateway_channels_reload(workspace)
    return _build_overview_response(updated)


def _normalize_weixin_bots_from_config(weixin_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """从 gateway.json 的 channel_configs.weixin 中提取归一化后的个人微信机器人列表。"""
    raw_bots = weixin_cfg.get("bots")
    bots: list[dict[str, Any]] = []
    if isinstance(raw_bots, list) and raw_bots:
        for item in raw_bots:
            if isinstance(item, dict) and (
                str(item.get("name") or "").strip()
                or str(item.get("account_id") or "").strip()
                or str(item.get("token") or "").strip()
            ):
                bots.append(dict(item))
        return bots

    legacy_token = str(weixin_cfg.get("token") or "").strip()
    legacy_account = str(weixin_cfg.get("account_id") or weixin_cfg.get("bot_uin") or "").strip()
    if legacy_token or legacy_account:
        bots.append(
            {
                "name": str(weixin_cfg.get("name") or "weixin-bot").strip() or "weixin-bot",
                "enabled": bool(weixin_cfg.get("enabled", True)),
                "account_id": legacy_account or "weixin",
                "token": legacy_token,
                "base_url": str(weixin_cfg.get("base_url") or _WEIXIN_ILINK_BASE).strip() or _WEIXIN_ILINK_BASE,
                "user_id": str(weixin_cfg.get("user_id") or "").strip(),
                "default_agent": str(weixin_cfg.get("default_agent") or "").strip(),
                "allow_from": list(weixin_cfg.get("allow_from") or ["*"]),
            }
        )
    return bots


def _weixin_dict_to_response(
    weixin_cfg: dict[str, Any], *, channel_enabled: bool
) -> WeixinChannelResponse:
    token = str(weixin_cfg.get("token") or "").strip()
    allow_from = weixin_cfg.get("allow_from")
    if not isinstance(allow_from, list) or not allow_from:
        allow_from = ["*"]
    return WeixinChannelResponse(
        name=str(weixin_cfg.get("name") or "weixin-bot").strip() or "weixin-bot",
        enabled=bool(weixin_cfg.get("enabled", channel_enabled)),
        account_id=str(weixin_cfg.get("account_id") or weixin_cfg.get("bot_uin") or "").strip(),
        token_masked=_mask_secret(token),
        has_token=bool(token),
        base_url=str(weixin_cfg.get("base_url") or _WEIXIN_ILINK_BASE).strip() or _WEIXIN_ILINK_BASE,
        user_id=str(weixin_cfg.get("user_id") or "").strip(),
        default_agent=str(weixin_cfg.get("default_agent") or "").strip(),
        allow_from=[str(x).strip() for x in allow_from if str(x).strip()] or ["*"],
    )


def _save_weixin_bots_to_workspace(
    workspace: str | None,
    bots: list[dict[str, Any]],
) -> ChannelsOverviewResponse:
    config = load_gateway_config(workspace)
    channel_configs = dict(config.channel_configs or {})
    weixin_cfg = dict(channel_configs.get("weixin") or {})
    weixin_cfg["bots"] = bots

    has_enabled_bot = any(bool(b.get("enabled", True)) and str(b.get("token") or "").strip() for b in bots)
    weixin_cfg["enabled"] = has_enabled_bot

    primary = next((b for b in bots if bool(b.get("enabled", True))), bots[0] if bots else None)
    if primary:
        weixin_cfg["name"] = str(primary.get("name") or "weixin-bot").strip()
        weixin_cfg["account_id"] = str(primary.get("account_id") or "weixin").strip()
        weixin_cfg["token"] = str(primary.get("token") or "").strip()
        weixin_cfg["base_url"] = str(primary.get("base_url") or _WEIXIN_ILINK_BASE).strip()
        weixin_cfg["user_id"] = str(primary.get("user_id") or "").strip()
        weixin_cfg["default_agent"] = str(primary.get("default_agent") or "").strip()
        weixin_cfg["allow_from"] = list(primary.get("allow_from") or ["*"])
    else:
        for k in ("account_id", "token", "user_id", "default_agent"):
            weixin_cfg[k] = ""

    channel_configs["weixin"] = weixin_cfg
    enabled_channels = list(config.enabled_channels or [])
    if has_enabled_bot and "weixin" not in enabled_channels:
        enabled_channels.append("weixin")
    elif not has_enabled_bot and "weixin" in enabled_channels:
        enabled_channels.remove("weixin")

    updated = config.model_copy(
        update={
            "enabled_channels": enabled_channels,
            "channel_configs": channel_configs,
        }
    )
    save_gateway_config(updated, workspace)
    _trigger_gateway_channels_reload(workspace)
    return _build_overview_response(updated)


def _normalize_wecom_bots_from_config(wecom_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """从 gateway.json 的 channel_configs.wecom 中提取归一化后的企业微信机器人列表。"""
    raw_bots = wecom_cfg.get("bots")
    bots: list[dict[str, Any]] = []
    if isinstance(raw_bots, list) and raw_bots:
        for item in raw_bots:
            if isinstance(item, dict) and (
                str(item.get("name") or "").strip()
                or str(item.get("bot_id") or "").strip()
            ):
                bots.append(dict(item))
        return bots

    legacy_bot_id = str(wecom_cfg.get("bot_id") or "").strip()
    legacy_secret = str(wecom_cfg.get("secret") or "").strip()
    if legacy_bot_id or legacy_secret:
        bots.append(
            {
                "name": str(wecom_cfg.get("name") or "wecom-bot").strip() or "wecom-bot",
                "enabled": bool(wecom_cfg.get("enabled", True)),
                "bot_id": legacy_bot_id,
                "secret": legacy_secret,
                "ws_url": str(wecom_cfg.get("ws_url") or _WECOM_DEFAULT_WS_URL).strip() or _WECOM_DEFAULT_WS_URL,
                "default_agent": str(wecom_cfg.get("default_agent") or "").strip(),
                "allow_from": list(wecom_cfg.get("allow_from") or ["*"]),
            }
        )
    return bots


def _wecom_dict_to_response(
    wecom_cfg: dict[str, Any], *, channel_enabled: bool
) -> WecomChannelResponse:
    secret = str(wecom_cfg.get("secret") or "").strip()
    allow_from = wecom_cfg.get("allow_from")
    if not isinstance(allow_from, list) or not allow_from:
        allow_from = ["*"]
    return WecomChannelResponse(
        name=str(wecom_cfg.get("name") or "wecom-bot").strip() or "wecom-bot",
        enabled=bool(wecom_cfg.get("enabled", channel_enabled)),
        bot_id=str(wecom_cfg.get("bot_id") or "").strip(),
        secret_masked=_mask_secret(secret),
        has_secret=bool(secret),
        ws_url=str(wecom_cfg.get("ws_url") or _WECOM_DEFAULT_WS_URL).strip() or _WECOM_DEFAULT_WS_URL,
        default_agent=str(wecom_cfg.get("default_agent") or "").strip(),
        allow_from=[str(x).strip() for x in allow_from if str(x).strip()] or ["*"],
    )


def _save_wecom_bots_to_workspace(
    workspace: str | None,
    bots: list[dict[str, Any]],
) -> ChannelsOverviewResponse:
    config = load_gateway_config(workspace)
    channel_configs = dict(config.channel_configs or {})
    wecom_cfg = dict(channel_configs.get("wecom") or {})
    wecom_cfg["bots"] = bots

    has_enabled_bot = any(bool(b.get("enabled", True)) and str(b.get("bot_id") or "").strip() for b in bots)
    wecom_cfg["enabled"] = has_enabled_bot

    primary = next((b for b in bots if bool(b.get("enabled", True))), bots[0] if bots else None)
    if primary:
        wecom_cfg["name"] = str(primary.get("name") or "wecom-bot").strip()
        wecom_cfg["bot_id"] = str(primary.get("bot_id") or "").strip()
        wecom_cfg["secret"] = str(primary.get("secret") or "").strip()
        wecom_cfg["ws_url"] = str(primary.get("ws_url") or _WECOM_DEFAULT_WS_URL).strip()
        wecom_cfg["default_agent"] = str(primary.get("default_agent") or "").strip()
        wecom_cfg["allow_from"] = list(primary.get("allow_from") or ["*"])
    else:
        for k in ("bot_id", "secret", "default_agent"):
            wecom_cfg[k] = ""

    channel_configs["wecom"] = wecom_cfg
    enabled_channels = list(config.enabled_channels or [])
    if has_enabled_bot and "wecom" not in enabled_channels:
        enabled_channels.append("wecom")
    elif not has_enabled_bot and "wecom" in enabled_channels:
        enabled_channels.remove("wecom")

    updated = config.model_copy(
        update={
            "enabled_channels": enabled_channels,
            "channel_configs": channel_configs,
        }
    )
    save_gateway_config(updated, workspace)
    _trigger_gateway_channels_reload(workspace)
    return _build_overview_response(updated)


def _build_overview_response(config) -> ChannelsOverviewResponse:
    channel_configs = dict(config.channel_configs or {})
    dingtalk_cfg = dict(channel_configs.get("dingtalk") or {})
    feishu_cfg = dict(channel_configs.get("feishu") or {})
    weixin_cfg = dict(channel_configs.get("weixin") or {})
    wecom_cfg = dict(channel_configs.get("wecom") or {})
    bots = _normalize_dingtalk_bots_from_config(dingtalk_cfg)
    feishu_bots = _normalize_feishu_bots_from_config(feishu_cfg)
    weixin_bots = _normalize_weixin_bots_from_config(weixin_cfg)
    wecom_bots = _normalize_wecom_bots_from_config(wecom_cfg)
    enabled_set = set(config.enabled_channels or [])
    feishu_enabled = "feishu" in enabled_set
    weixin_enabled = "weixin" in enabled_set
    wecom_enabled = "wecom" in enabled_set
    feishu_bot_resps = [
        _feishu_dict_to_response(b, channel_enabled=feishu_enabled)
        for b in feishu_bots
    ]
    primary_feishu = (
        feishu_bot_resps[0]
        if feishu_bot_resps
        else _feishu_dict_to_response(feishu_cfg, channel_enabled=feishu_enabled)
    )
    weixin_bot_resps = [
        _weixin_dict_to_response(b, channel_enabled=weixin_enabled)
        for b in weixin_bots
    ]
    primary_weixin = (
        weixin_bot_resps[0]
        if weixin_bot_resps
        else _weixin_dict_to_response(weixin_cfg, channel_enabled=weixin_enabled)
    )
    wecom_bot_resps = [
        _wecom_dict_to_response(b, channel_enabled=wecom_enabled)
        for b in wecom_bots
    ]
    primary_wecom = (
        wecom_bot_resps[0]
        if wecom_bot_resps
        else _wecom_dict_to_response(wecom_cfg, channel_enabled=wecom_enabled)
    )
    return ChannelsOverviewResponse(
        enabled_channels=list(config.enabled_channels or []),
        send_progress=bool(config.send_progress),
        send_tool_hints=bool(config.send_tool_hints),
        dingtalk_bots=[_bot_dict_to_response(b) for b in bots],
        feishu_bots=feishu_bot_resps,
        feishu=primary_feishu,
        weixin_bots=weixin_bot_resps,
        weixin=primary_weixin,
        wecom_bots=wecom_bot_resps,
        wecom=primary_wecom,
    )


async def dingtalk_device_flow_generate() -> dict[str, Any]:
    """调用钉钉开放平台设备流接口初始化并获取扫码注册地址。"""
    async with httpx.AsyncClient(
        base_url=_DINGTALK_OAPI_BASE, timeout=15.0, trust_env=False
    ) as client:
        init_resp = await client.post("/app/registration/init", json={})
        init_resp.raise_for_status()
        init_data = init_resp.json()
        if not isinstance(init_data, dict) or init_data.get("errcode") != 0:
            msg = (
                init_data.get("errmsg")
                if isinstance(init_data, dict)
                else "无效响应"
            )
            raise RuntimeError(f"钉钉注册初始化失败：{msg}")
        nonce = str(init_data.get("nonce") or "").strip()
        if not nonce:
            raise RuntimeError("钉钉注册初始化未返回有效的 nonce")

        begin_resp = await client.post(
            "/app/registration/begin", json={"nonce": nonce}
        )
        begin_resp.raise_for_status()
        begin_data = begin_resp.json()
        if not isinstance(begin_data, dict) or begin_data.get("errcode") != 0:
            msg = (
                begin_data.get("errmsg")
                if isinstance(begin_data, dict)
                else "无效响应"
            )
            raise RuntimeError(f"钉钉扫码会话创建失败：{msg}")

    for key in ("device_code", "user_code", "verification_uri_complete"):
        if not str(begin_data.get(key) or "").strip():
            raise RuntimeError(f"钉钉扫码会话缺少必要字段：{key}")
    return begin_data


async def dingtalk_device_flow_poll(device_code: str) -> dict[str, Any]:
    """轮询钉钉开放平台设备流扫码授权结果。"""
    async with httpx.AsyncClient(
        base_url=_DINGTALK_OAPI_BASE, timeout=15.0, trust_env=False
    ) as client:
        resp = await client.post(
            "/app/registration/poll",
            json={"device_code": device_code},
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict) or data.get("errcode") != 0:
            msg = data.get("errmsg") if isinstance(data, dict) else "无效响应"
            raise RuntimeError(f"钉钉扫码状态查询失败：{msg}")
        return data


async def probe_dingtalk_credentials(
    client_id: str, client_secret: str
) -> ChannelProbeResponse:
    """通过换取钉钉 AccessToken 校验机器人 Client ID 与 Client Secret 是否有效。"""
    started = time.perf_counter()
    now = time.time()
    cid = (client_id or "").strip()
    secret = (client_secret or "").strip()
    if not cid or not secret:
        return ChannelProbeResponse(
            ok=False,
            latency_ms=0,
            checked_at=now,
            error="请提供完整的 Client ID 与 Client Secret",
        )
    try:
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            resp = await client.post(
                _DINGTALK_API_TOKEN_URL,
                json={"appKey": cid, "appSecret": secret},
            )
        latency_ms = max(1, int((time.perf_counter() - started) * 1000))
        data = resp.json() if resp.content else {}
        if resp.status_code == 200 and isinstance(data, dict) and data.get("accessToken"):
            return ChannelProbeResponse(
                ok=True,
                latency_ms=latency_ms,
                checked_at=now,
            )
        err_msg = (
            data.get("message")
            or data.get("errmsg")
            or f"HTTP {resp.status_code}"
            if isinstance(data, dict)
            else f"HTTP {resp.status_code}"
        )
        return ChannelProbeResponse(
            ok=False,
            latency_ms=latency_ms,
            checked_at=now,
            error=f"钉钉凭证校验未通过：{err_msg}",
        )
    except Exception as exc:
        latency_ms = max(1, int((time.perf_counter() - started) * 1000))
        return ChannelProbeResponse(
            ok=False,
            latency_ms=latency_ms,
            checked_at=now,
            error=f"连接钉钉接口失败：{exc}",
        )


def _prune_qr_sessions(now: float) -> None:
    expired = [k for k, s in _dingtalk_qr_sessions.items() if s.expires_at <= now]
    for k in expired:
        _dingtalk_qr_sessions.pop(k, None)
    if len(_dingtalk_qr_sessions) >= _DINGTALK_MAX_SESSIONS:
        oldest = min(
            _dingtalk_qr_sessions, key=lambda k: _dingtalk_qr_sessions[k].expires_at
        )
        _dingtalk_qr_sessions.pop(oldest, None)

    feishu_expired = [k for k, s in _feishu_qr_sessions.items() if s.expires_at <= now]
    for k in feishu_expired:
        _feishu_qr_sessions.pop(k, None)
    if len(_feishu_qr_sessions) >= _DINGTALK_MAX_SESSIONS:
        oldest_fs = min(
            _feishu_qr_sessions, key=lambda k: _feishu_qr_sessions[k].expires_at
        )
        _feishu_qr_sessions.pop(oldest_fs, None)


async def feishu_device_flow_generate() -> dict[str, Any]:
    """调用飞书开放平台设备流接口初始化并获取扫码注册地址。"""
    async with httpx.AsyncClient(
        base_url=_FEISHU_ACCOUNTS_BASE, timeout=15.0, trust_env=False
    ) as client:
        init_resp = await client.post(
            "/oauth/v1/app/registration",
            data={"action": "init"},
        )
        init_resp.raise_for_status()
        init_data = init_resp.json()
        if not isinstance(init_data, dict):
            raise RuntimeError("飞书注册初始化返回无效响应")
        supported = init_data.get("supported_auth_methods") or []
        if isinstance(supported, list) and supported and "client_secret" not in supported:
            raise RuntimeError("当前飞书环境不支持 client_secret 认证方式")
        nonce = str(init_data.get("nonce") or "").strip()

        begin_resp = await client.post(
            "/oauth/v1/app/registration",
            data={
                "action": "begin",
                "archetype": "PersonalAgent",
                "auth_method": "client_secret",
                "request_id": nonce,
            },
        )
        begin_resp.raise_for_status()
        begin_data = begin_resp.json()
        if not isinstance(begin_data, dict) or begin_data.get("error"):
            msg = (
                begin_data.get("error_description")
                or begin_data.get("error")
                if isinstance(begin_data, dict)
                else "无效响应"
            )
            raise RuntimeError(f"飞书扫码会话创建失败：{msg}")

    for key in ("device_code", "user_code", "verification_uri_complete"):
        if not str(begin_data.get(key) or "").strip():
            raise RuntimeError(f"飞书扫码会话缺少必要字段：{key}")
    return begin_data


async def feishu_device_flow_poll(
    device_code: str, base_url: str = _FEISHU_ACCOUNTS_BASE
) -> dict[str, Any]:
    """轮询飞书开放平台设备流扫码授权结果。"""
    async with httpx.AsyncClient(
        base_url=base_url, timeout=15.0, trust_env=False
    ) as client:
        resp = await client.post(
            "/oauth/v1/app/registration",
            data={
                "action": "poll",
                "device_code": device_code,
            },
        )
        data = resp.json() if resp.content else {}
        if not isinstance(data, dict):
            raise RuntimeError(f"飞书扫码状态查询失败：HTTP {resp.status_code}")
        return data


async def probe_feishu_credentials(
    app_id: str, app_secret: str
) -> ChannelProbeResponse:
    """通过换取飞书 tenant_access_token 校验机器人 App ID 与 App Secret 是否有效。"""
    started = time.perf_counter()
    now = time.time()
    aid = (app_id or "").strip()
    secret = (app_secret or "").strip()
    if not aid or not secret:
        return ChannelProbeResponse(
            ok=False,
            latency_ms=0,
            checked_at=now,
            error="请提供完整的飞书 App ID 与 App Secret",
        )
    try:
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            resp = await client.post(
                _FEISHU_TOKEN_URL,
                json={"app_id": aid, "app_secret": secret},
            )
        latency_ms = max(1, int((time.perf_counter() - started) * 1000))
        data = resp.json() if resp.content else {}
        if (
            resp.status_code == 200
            and isinstance(data, dict)
            and data.get("code") == 0
            and data.get("tenant_access_token")
        ):
            return ChannelProbeResponse(
                ok=True,
                latency_ms=latency_ms,
                checked_at=now,
            )
        err_msg = (
            data.get("msg")
            or data.get("message")
            or f"HTTP {resp.status_code}"
            if isinstance(data, dict)
            else f"HTTP {resp.status_code}"
        )
        return ChannelProbeResponse(
            ok=False,
            latency_ms=latency_ms,
            checked_at=now,
            error=f"飞书凭证校验未通过：{err_msg}",
        )
    except Exception as exc:
        latency_ms = max(1, int((time.perf_counter() - started) * 1000))
        return ChannelProbeResponse(
            ok=False,
            latency_ms=latency_ms,
            checked_at=now,
            error=f"连接飞书接口失败：{exc}",
        )


def _choose_auto_bot_name(
    existing_names: set[str], preferred_name: str, default_agent: str
) -> str:
    candidate = _BOT_NAME_INVALID_RE.sub("-", (preferred_name or "").strip()).strip("-")
    if not candidate and default_agent:
        candidate = _BOT_NAME_INVALID_RE.sub("-", default_agent.strip()).strip("-")
    if not candidate:
        candidate = "dingtalk-bot"
    if candidate not in existing_names:
        return candidate
    idx = 2
    while f"{candidate}-{idx}" in existing_names:
        idx += 1
    return f"{candidate}-{idx}"


@router.get("", response_model=ChannelsOverviewResponse)
async def get_channels_overview(
    _user: Annotated[dict, Depends(require_agent_admin)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
) -> ChannelsOverviewResponse:
    """获取当前工作区的通道连接配置总览。"""
    config = load_gateway_config(runtime.workspace)
    return _build_overview_response(config)


@router.post(
    "/dingtalk/bots",
    response_model=ChannelsOverviewResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_dingtalk_bot(
    body: DingTalkBotUpsertRequest,
    _user: Annotated[dict, Depends(require_agent_admin)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
) -> ChannelsOverviewResponse:
    """新增一个钉钉机器人配置。"""
    bot_name = _validate_bot_name(body.name)
    secret = body.client_secret.strip()
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="新增钉钉机器人必须填写 Client Secret",
        )
    config = load_gateway_config(runtime.workspace)
    dingtalk_cfg = dict((config.channel_configs or {}).get("dingtalk") or {})
    bots = _normalize_dingtalk_bots_from_config(dingtalk_cfg)
    if any(str(b.get("name") or "").strip() == bot_name for b in bots):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"钉钉机器人「{bot_name}」已存在",
        )
    bots.append(
        {
            "name": bot_name,
            "enabled": body.enabled,
            "client_id": body.client_id.strip(),
            "corp_id": body.corp_id.strip(),
            "client_secret": secret,
            "robot_code": body.robot_code.strip() or body.client_id.strip(),
            "default_agent": body.default_agent.strip(),
            "allow_from": [x.strip() for x in body.allow_from if x.strip()] or ["*"],
            "defer_board_images": body.defer_board_images,
            "board_business_domain": body.board_business_domain.strip(),
            "board_batch_quiet_seconds": body.board_batch_quiet_seconds,
        }
    )
    return _save_dingtalk_bots_to_workspace(runtime.workspace, bots)


@router.put("/dingtalk/bots/{bot_name}", response_model=ChannelsOverviewResponse)
async def update_dingtalk_bot(
    bot_name: str,
    body: DingTalkBotUpsertRequest,
    _user: Annotated[dict, Depends(require_agent_admin)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
) -> ChannelsOverviewResponse:
    """更新指定钉钉机器人配置（若未传新密钥则保留原密钥）。"""
    target_name = _validate_bot_name(body.name)
    config = load_gateway_config(runtime.workspace)
    dingtalk_cfg = dict((config.channel_configs or {}).get("dingtalk") or {})
    bots = _normalize_dingtalk_bots_from_config(dingtalk_cfg)

    match_idx = next(
        (i for i, b in enumerate(bots) if str(b.get("name") or "").strip() == bot_name),
        None,
    )
    if match_idx is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"未找到钉钉机器人「{bot_name}」",
        )
    if target_name != bot_name and any(
        str(b.get("name") or "").strip() == target_name for b in bots
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"钉钉机器人名称「{target_name}」已被占用",
        )

    existing = bots[match_idx]
    secret = body.client_secret.strip() or str(existing.get("client_secret") or "").strip()
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="请填写 Client Secret",
        )
    bots[match_idx] = {
        "name": target_name,
        "enabled": body.enabled,
        "client_id": body.client_id.strip(),
        "corp_id": body.corp_id.strip(),
        "client_secret": secret,
        "robot_code": body.robot_code.strip() or body.client_id.strip(),
        "default_agent": body.default_agent.strip(),
        "allow_from": [x.strip() for x in body.allow_from if x.strip()] or ["*"],
        "defer_board_images": body.defer_board_images,
        "board_business_domain": body.board_business_domain.strip(),
        "board_batch_quiet_seconds": body.board_batch_quiet_seconds,
    }
    return _save_dingtalk_bots_to_workspace(runtime.workspace, bots)


@router.delete("/dingtalk/bots/{bot_name}", response_model=ChannelsOverviewResponse)
async def delete_dingtalk_bot(
    bot_name: str,
    _user: Annotated[dict, Depends(require_agent_admin)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
) -> ChannelsOverviewResponse:
    """删除指定钉钉机器人配置。"""
    config = load_gateway_config(runtime.workspace)
    dingtalk_cfg = dict((config.channel_configs or {}).get("dingtalk") or {})
    bots = _normalize_dingtalk_bots_from_config(dingtalk_cfg)
    remaining = [b for b in bots if str(b.get("name") or "").strip() != bot_name]
    if len(remaining) == len(bots):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"未找到钉钉机器人「{bot_name}」",
        )
    return _save_dingtalk_bots_to_workspace(runtime.workspace, remaining)


@router.post("/dingtalk/bots/{bot_name}/test", response_model=ChannelProbeResponse)
async def test_dingtalk_bot_connection(
    bot_name: str,
    _user: Annotated[dict, Depends(require_agent_admin)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
) -> ChannelProbeResponse:
    """测试已保存钉钉机器人的凭证连通性。"""
    config = load_gateway_config(runtime.workspace)
    dingtalk_cfg = dict((config.channel_configs or {}).get("dingtalk") or {})
    bots = _normalize_dingtalk_bots_from_config(dingtalk_cfg)
    bot = next(
        (b for b in bots if str(b.get("name") or "").strip() == bot_name),
        None,
    )
    if bot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"未找到钉钉机器人「{bot_name}」",
        )
    return await probe_dingtalk_credentials(
        str(bot.get("client_id") or ""),
        str(bot.get("client_secret") or ""),
    )


@router.post("/dingtalk/probe", response_model=ChannelProbeResponse)
async def probe_dingtalk_draft(
    body: DingTalkProbeRequest,
    _user: Annotated[dict, Depends(require_agent_admin)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
) -> ChannelProbeResponse:
    """测试正在编辑的钉钉机器人凭证（若未传密钥则回退读取已保存的机器人密钥）。"""
    secret = body.client_secret.strip()
    if not secret and body.bot_name:
        config = load_gateway_config(runtime.workspace)
        dingtalk_cfg = dict((config.channel_configs or {}).get("dingtalk") or {})
        bots = _normalize_dingtalk_bots_from_config(dingtalk_cfg)
        existing = next(
            (b for b in bots if str(b.get("name") or "").strip() == body.bot_name.strip()),
            None,
        )
        if existing:
            secret = str(existing.get("client_secret") or "").strip()
    return await probe_dingtalk_credentials(body.client_id.strip(), secret)


@router.post("/dingtalk/qrcode/generate", response_model=DingTalkQrGenerateResponse)
async def generate_dingtalk_qrcode(
    body: DingTalkQrGenerateRequest,
    _user: Annotated[dict, Depends(require_agent_admin)],
) -> DingTalkQrGenerateResponse:
    """启动钉钉设备流扫码注册，返回二维码地址与设备验证码。"""
    try:
        started = await dingtalk_device_flow_generate()
    except Exception as exc:
        logger.exception("Failed to start DingTalk QR registration")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"启动钉钉扫码注册失败：{exc}",
        ) from exc

    expires_in = max(
        60, min(int(started.get("expires_in") or 600), _DINGTALK_MAX_EXPIRES_IN)
    )
    interval = max(2, min(int(started.get("interval") or 5), 30))
    registration_id = secrets.token_urlsafe(24)
    now = time.monotonic()
    _prune_qr_sessions(now)
    _dingtalk_qr_sessions[registration_id] = _DingTalkRegistrationSession(
        registration_id=registration_id,
        device_code=str(started["device_code"]),
        user_code=str(started["user_code"]),
        qrcode_url=str(started["verification_uri_complete"]),
        expires_at=now + expires_in,
        interval=interval,
        bot_name=body.bot_name.strip(),
        default_agent=body.default_agent.strip(),
        defer_board_images=body.defer_board_images,
        board_business_domain=body.board_business_domain.strip(),
    )
    return DingTalkQrGenerateResponse(
        registration_id=registration_id,
        qrcode_url=str(started["verification_uri_complete"]),
        user_code=str(started["user_code"]),
        expires_in=expires_in,
        interval=interval,
    )


@router.post("/dingtalk/qrcode/poll", response_model=DingTalkQrPollResponse)
async def poll_dingtalk_qrcode(
    body: DingTalkQrPollRequest,
    _user: Annotated[dict, Depends(require_agent_admin)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
) -> DingTalkQrPollResponse:
    """轮询钉钉扫码授权状态；扫码成功后自动创建并启用钉钉机器人配置。"""
    reg_id = (body.registration_id or "").strip()
    session = _dingtalk_qr_sessions.get(reg_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="扫码会话不存在或已过期，请重新生成二维码",
        )

    async with session.lock:
        if session.completed_bot is not None:
            return DingTalkQrPollResponse(
                status="success",
                bot=session.completed_bot,
            )
        if time.monotonic() >= session.expires_at:
            _dingtalk_qr_sessions.pop(reg_id, None)
            return DingTalkQrPollResponse(
                status="expired",
                message="二维码已过期，请点击重新生成",
            )

        try:
            result = await dingtalk_device_flow_poll(session.device_code)
        except Exception as exc:
            logger.exception("Failed to poll DingTalk QR registration")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"查询钉钉扫码状态失败：{exc}",
            ) from exc

        poll_status = str(result.get("status") or "").strip().upper()
        if poll_status == "WAITING":
            return DingTalkQrPollResponse(status="waiting")
        if poll_status == "EXPIRED":
            _dingtalk_qr_sessions.pop(reg_id, None)
            return DingTalkQrPollResponse(
                status="expired",
                message="二维码已过期，请点击重新生成",
            )
        if poll_status == "FAIL":
            _dingtalk_qr_sessions.pop(reg_id, None)
            reason = str(result.get("fail_reason") or "钉钉端拒绝或取消了授权")
            return DingTalkQrPollResponse(status="failed", message=reason)
        if poll_status != "SUCCESS":
            return DingTalkQrPollResponse(
                status="failed",
                message=f"未知的钉钉授权状态：{poll_status}",
            )

        client_id = str(result.get("client_id") or "").strip()
        client_secret = str(result.get("client_secret") or "").strip()
        if not client_id or not client_secret:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="钉钉扫码成功但未返回完整的机器人凭证",
            )

        preferred_name = body.bot_name.strip() or session.bot_name
        default_agent = body.default_agent.strip() or session.default_agent
        defer_board = (
            body.defer_board_images
            if body.defer_board_images is not False
            else session.defer_board_images
        )
        board_domain = (
            body.board_business_domain.strip() or session.board_business_domain
        )

        config = load_gateway_config(runtime.workspace)
        dingtalk_cfg = dict((config.channel_configs or {}).get("dingtalk") or {})
        bots = _normalize_dingtalk_bots_from_config(dingtalk_cfg)
        existing_names = {str(b.get("name") or "").strip() for b in bots}
        bot_name = _choose_auto_bot_name(existing_names, preferred_name, default_agent)

        new_bot = {
            "name": bot_name,
            "enabled": True,
            "client_id": client_id,
            "corp_id": str(result.get("corp_id") or "").strip(),
            "client_secret": client_secret,
            "robot_code": client_id,
            "default_agent": default_agent,
            "allow_from": ["*"],
            "defer_board_images": defer_board,
            "board_business_domain": board_domain,
            "board_batch_quiet_seconds": 20,
        }
        bots.append(new_bot)
        _save_dingtalk_bots_to_workspace(runtime.workspace, bots)

        bot_resp = _bot_dict_to_response(new_bot)
        session.completed_bot = bot_resp
        session.device_code = ""
        return DingTalkQrPollResponse(status="success", bot=bot_resp)


@router.put("/feishu", response_model=ChannelsOverviewResponse)
async def update_feishu_channel(
    body: FeishuChannelUpdateRequest,
    _user: Annotated[dict, Depends(require_agent_admin)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
) -> ChannelsOverviewResponse:
    """更新飞书通道配置（兼容单飞书应用及多飞书机器人）。"""
    config = load_gateway_config(runtime.workspace)
    feishu_cfg = dict((config.channel_configs or {}).get("feishu") or {})
    bots = _normalize_feishu_bots_from_config(feishu_cfg)
    target_name = (body.name or "").strip() or (
        str(bots[0].get("name") or "feishu-bot").strip() if bots else "feishu-bot"
    )
    match_idx = next(
        (i for i, b in enumerate(bots) if str(b.get("name") or "").strip() == target_name),
        0 if bots else None,
    )
    existing = bots[match_idx] if match_idx is not None else feishu_cfg
    secret = body.app_secret.strip() or str(existing.get("app_secret") or "").strip()
    if body.enabled and (not body.app_id.strip() or not secret):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="启用飞书通道需填写 App ID 与 App Secret",
        )
    bot_entry = {
        "name": target_name,
        "enabled": body.enabled,
        "app_id": body.app_id.strip(),
        "app_secret": secret,
        "encrypt_key": body.encrypt_key.strip(),
        "verification_token": body.verification_token.strip(),
        "default_agent": body.default_agent.strip(),
        "allow_from": [x.strip() for x in body.allow_from if x.strip()] or ["*"],
    }
    if match_idx is not None:
        bots[match_idx] = bot_entry
    elif body.app_id.strip() or secret:
        bots.append(bot_entry)
    return _save_feishu_bots_to_workspace(runtime.workspace, bots)


@router.post(
    "/feishu/bots",
    response_model=ChannelsOverviewResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_feishu_bot(
    body: FeishuChannelUpdateRequest,
    _user: Annotated[dict, Depends(require_agent_admin)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
) -> ChannelsOverviewResponse:
    """新增一个飞书机器人配置并可绑定到指定智能体。"""
    bot_name = _validate_bot_name(body.name or "feishu-bot")
    secret = body.app_secret.strip()
    if not body.app_id.strip() or not secret:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="新增飞书机器人必须填写 App ID 与 App Secret",
        )
    config = load_gateway_config(runtime.workspace)
    feishu_cfg = dict((config.channel_configs or {}).get("feishu") or {})
    bots = _normalize_feishu_bots_from_config(feishu_cfg)
    if any(str(b.get("name") or "").strip() == bot_name for b in bots):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"飞书机器人「{bot_name}」已存在",
        )
    bots.append(
        {
            "name": bot_name,
            "enabled": body.enabled,
            "app_id": body.app_id.strip(),
            "app_secret": secret,
            "encrypt_key": body.encrypt_key.strip(),
            "verification_token": body.verification_token.strip(),
            "default_agent": body.default_agent.strip(),
            "allow_from": [x.strip() for x in body.allow_from if x.strip()] or ["*"],
        }
    )
    return _save_feishu_bots_to_workspace(runtime.workspace, bots)


@router.put("/feishu/bots/{bot_name}", response_model=ChannelsOverviewResponse)
async def update_feishu_bot(
    bot_name: str,
    body: FeishuChannelUpdateRequest,
    _user: Annotated[dict, Depends(require_agent_admin)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
) -> ChannelsOverviewResponse:
    """更新指定飞书机器人配置（包括默认绑定智能体）。"""
    target_name = _validate_bot_name(body.name or bot_name)
    config = load_gateway_config(runtime.workspace)
    feishu_cfg = dict((config.channel_configs or {}).get("feishu") or {})
    bots = _normalize_feishu_bots_from_config(feishu_cfg)
    match_idx = next(
        (i for i, b in enumerate(bots) if str(b.get("name") or "").strip() == bot_name),
        None,
    )
    if match_idx is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"未找到飞书机器人「{bot_name}」",
        )
    if target_name != bot_name and any(
        str(b.get("name") or "").strip() == target_name for b in bots
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"飞书机器人名称「{target_name}」已被占用",
        )
    existing = bots[match_idx]
    secret = body.app_secret.strip() or str(existing.get("app_secret") or "").strip()
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="请填写飞书 App Secret",
        )
    bots[match_idx] = {
        "name": target_name,
        "enabled": body.enabled,
        "app_id": body.app_id.strip(),
        "app_secret": secret,
        "encrypt_key": body.encrypt_key.strip(),
        "verification_token": body.verification_token.strip(),
        "default_agent": body.default_agent.strip(),
        "allow_from": [x.strip() for x in body.allow_from if x.strip()] or ["*"],
    }
    return _save_feishu_bots_to_workspace(runtime.workspace, bots)


@router.delete("/feishu/bots/{bot_name}", response_model=ChannelsOverviewResponse)
async def delete_feishu_bot(
    bot_name: str,
    _user: Annotated[dict, Depends(require_agent_admin)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
) -> ChannelsOverviewResponse:
    """删除指定飞书机器人配置。"""
    config = load_gateway_config(runtime.workspace)
    feishu_cfg = dict((config.channel_configs or {}).get("feishu") or {})
    bots = _normalize_feishu_bots_from_config(feishu_cfg)
    remaining = [b for b in bots if str(b.get("name") or "").strip() != bot_name]
    if len(remaining) == len(bots):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"未找到飞书机器人「{bot_name}」",
        )
    return _save_feishu_bots_to_workspace(runtime.workspace, remaining)


@router.post("/feishu/probe", response_model=ChannelProbeResponse)
async def probe_feishu_draft(
    body: FeishuProbeRequest,
    _user: Annotated[dict, Depends(require_agent_admin)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
) -> ChannelProbeResponse:
    """测试飞书机器人凭证（若未传密钥则回退读取已保存的飞书密钥）。"""
    secret = body.app_secret.strip()
    if not secret:
        config = load_gateway_config(runtime.workspace)
        feishu_cfg = dict((config.channel_configs or {}).get("feishu") or {})
        bots = _normalize_feishu_bots_from_config(feishu_cfg)
        if body.bot_name:
            matched = next(
                (
                    b
                    for b in bots
                    if str(b.get("name") or "").strip() == body.bot_name.strip()
                ),
                None,
            )
            if matched:
                secret = str(matched.get("app_secret") or "").strip()
        if not secret and body.app_id.strip():
            matched_by_id = next(
                (
                    b
                    for b in bots
                    if str(b.get("app_id") or "").strip() == body.app_id.strip()
                ),
                None,
            )
            if matched_by_id:
                secret = str(matched_by_id.get("app_secret") or "").strip()
        if not secret:
            secret = str(feishu_cfg.get("app_secret") or "").strip()
    return await probe_feishu_credentials(body.app_id.strip(), secret)


@router.post("/feishu/qrcode/generate", response_model=FeishuQrGenerateResponse)
async def generate_feishu_qrcode(
    body: FeishuQrGenerateRequest,
    _user: Annotated[dict, Depends(require_agent_admin)],
) -> FeishuQrGenerateResponse:
    """启动飞书设备流扫码注册，返回二维码地址与设备验证码。"""
    try:
        started = await feishu_device_flow_generate()
    except Exception as exc:
        logger.exception("Failed to start Feishu QR registration")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"启动飞书扫码注册失败：{exc}",
        ) from exc

    expires_in = max(
        60, min(int(started.get("expires_in") or 600), _DINGTALK_MAX_EXPIRES_IN)
    )
    interval = max(2, min(int(started.get("interval") or 5), 30))
    registration_id = secrets.token_urlsafe(24)
    now = time.monotonic()
    _prune_qr_sessions(now)
    allow_from = [str(x).strip() for x in (body.allow_from or []) if str(x).strip()] or ["*"]
    _feishu_qr_sessions[registration_id] = _FeishuRegistrationSession(
        registration_id=registration_id,
        device_code=str(started["device_code"]),
        user_code=str(started["user_code"]),
        qrcode_url=str(started["verification_uri_complete"]),
        expires_at=now + expires_in,
        interval=interval,
        base_url=_FEISHU_ACCOUNTS_BASE,
        bot_name=body.bot_name.strip(),
        default_agent=body.default_agent.strip(),
        allow_from=allow_from,
    )
    return FeishuQrGenerateResponse(
        registration_id=registration_id,
        qrcode_url=str(started["verification_uri_complete"]),
        user_code=str(started["user_code"]),
        expires_in=expires_in,
        interval=interval,
    )


@router.post("/feishu/qrcode/poll", response_model=FeishuQrPollResponse)
async def poll_feishu_qrcode(
    body: FeishuQrPollRequest,
    _user: Annotated[dict, Depends(require_agent_admin)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
) -> FeishuQrPollResponse:
    """轮询飞书扫码授权状态；扫码成功后自动将 App ID 与 App Secret 写入并绑定指定智能体。"""
    reg_id = (body.registration_id or "").strip()
    session = _feishu_qr_sessions.get(reg_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="飞书扫码会话不存在或已过期，请重新生成二维码",
        )

    async with session.lock:
        if session.completed_feishu is not None:
            return FeishuQrPollResponse(
                status="success",
                feishu=session.completed_feishu,
            )
        if time.monotonic() >= session.expires_at:
            _feishu_qr_sessions.pop(reg_id, None)
            return FeishuQrPollResponse(
                status="expired",
                message="二维码已过期，请点击重新生成",
            )

        try:
            result = await feishu_device_flow_poll(
                session.device_code, session.base_url
            )
        except Exception as exc:
            logger.exception("Failed to poll Feishu QR registration")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"查询飞书扫码状态失败：{exc}",
            ) from exc

        # 兼容国际版 Lark 租户自动切换域名
        user_info = result.get("user_info")
        if (
            isinstance(user_info, dict)
            and user_info.get("tenant_brand") == "lark"
            and session.base_url != _LARK_ACCOUNTS_BASE
        ):
            session.base_url = _LARK_ACCOUNTS_BASE

        client_id = str(result.get("client_id") or "").strip()
        client_secret = str(result.get("client_secret") or "").strip()
        if client_id and client_secret:
            preferred_name = body.bot_name.strip() or session.bot_name
            default_agent = body.default_agent.strip() or session.default_agent

            config = load_gateway_config(runtime.workspace)
            feishu_cfg = dict((config.channel_configs or {}).get("feishu") or {})
            bots = _normalize_feishu_bots_from_config(feishu_cfg)
            existing_names = {str(b.get("name") or "").strip() for b in bots}
            bot_name = _choose_auto_bot_name(
                existing_names,
                preferred_name or (f"feishu-{default_agent}" if default_agent else "feishu-bot"),
                default_agent,
            )
            allow_from = (
                [str(x).strip() for x in (body.allow_from or []) if str(x).strip()]
                or session.allow_from
                or list(feishu_cfg.get("allow_from") or ["*"])
            )
            new_feishu_bot = {
                "name": bot_name,
                "enabled": True,
                "app_id": client_id,
                "app_secret": client_secret,
                "encrypt_key": "",
                "verification_token": "",
                "default_agent": default_agent,
                "allow_from": allow_from,
            }
            # 如果已有同名或同 app_id 机器人则更新，否则追加
            existing_idx = next(
                (
                    i
                    for i, b in enumerate(bots)
                    if str(b.get("app_id") or "").strip() == client_id
                    or (preferred_name and str(b.get("name") or "").strip() == preferred_name)
                ),
                None,
            )
            if existing_idx is not None:
                new_feishu_bot["name"] = str(bots[existing_idx].get("name") or bot_name).strip()
                bots[existing_idx] = new_feishu_bot
            else:
                bots.append(new_feishu_bot)

            _save_feishu_bots_to_workspace(runtime.workspace, bots)
            feishu_resp = _feishu_dict_to_response(
                new_feishu_bot, channel_enabled=True
            )
            session.completed_feishu = feishu_resp
            session.device_code = ""
            return FeishuQrPollResponse(status="success", feishu=feishu_resp)

        error_code = str(result.get("error") or "").strip()
        if error_code in ("authorization_pending", "slow_down"):
            if error_code == "slow_down":
                session.interval = min(session.interval + 5, 30)
            return FeishuQrPollResponse(status="waiting")
        if error_code == "expired_token":
            _feishu_qr_sessions.pop(reg_id, None)
            return FeishuQrPollResponse(
                status="expired",
                message="二维码已过期，请点击重新生成",
            )
        if error_code == "access_denied":
            _feishu_qr_sessions.pop(reg_id, None)
            return FeishuQrPollResponse(
                status="failed",
                message="飞书端拒绝或取消了扫码授权",
            )

        err_desc = (
            str(result.get("error_description") or error_code or "未知的飞书授权状态")
        ).strip()
        _feishu_qr_sessions.pop(reg_id, None)
        return FeishuQrPollResponse(status="failed", message=err_desc)


# ─── 个人微信（WeChat iLink）通道配置与扫码绑定 ─────────────────────────────


async def weixin_ilink_qr_generate() -> dict[str, Any]:
    """调用微信 iLink 接口生成个人微信扫码绑定二维码。"""
    async with httpx.AsyncClient(
        base_url=_WEIXIN_ILINK_BASE, timeout=15.0, trust_env=False
    ) as client:
        resp = await client.get(
            "/ilink/bot/get_bot_qrcode",
            params={"bot_type": "3"},
            headers={"iLink-App-ClientVersion": "1"},
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise RuntimeError("微信 iLink 二维码接口返回格式无效")
        qrcode = str(data.get("qrcode") or "").strip()
        qrcode_url = str(data.get("qrcode_img_content") or "").strip()
        if not qrcode or not qrcode_url:
            raise RuntimeError(f"微信 iLink 未返回有效的二维码数据：{data}")
        return {"qrcode": qrcode, "qrcode_url": qrcode_url}


async def weixin_ilink_qr_poll(qrcode_token: str) -> dict[str, Any]:
    """长轮询微信 iLink 扫码确认状态（若超时未扫则返回 status=wait）。"""
    try:
        async with httpx.AsyncClient(
            base_url=_WEIXIN_ILINK_BASE, timeout=38.0, trust_env=False
        ) as client:
            resp = await client.get(
                "/ilink/bot/get_qrcode_status",
                params={"qrcode": qrcode_token},
                headers={"iLink-App-ClientVersion": "1"},
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else {"status": "wait"}
    except (httpx.TimeoutException, TimeoutError):
        return {"status": "wait"}


async def probe_weixin_credentials(
    token: str, base_url: str = _WEIXIN_ILINK_BASE
) -> ChannelProbeResponse:
    """通过发起 1 秒短轮询 getupdates 校验个人微信 iLink Token 是否有效。"""
    from openharness.channels.impl.weixin import _wechat_uin

    started = time.perf_counter()
    now = time.time()
    cleaned_token = (token or "").strip()
    target_base = (base_url or _WEIXIN_ILINK_BASE).strip().rstrip("/") or _WEIXIN_ILINK_BASE
    if not cleaned_token:
        return ChannelProbeResponse(
            ok=False,
            latency_ms=0,
            checked_at=now,
            error="请先扫码或填写个人微信 iLink Token",
        )
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Authorization": f"Bearer {cleaned_token}",
        "X-WECHAT-UIN": _wechat_uin(),
        "iLink-App-Id": "bot",
        "iLink-App-ClientVersion": str((2 << 16) | (2 << 8) | 0),
    }
    payload = {
        "get_updates_buf": "",
        "longpolling_timeout_ms": 1000,
        "base_info": {"channel_version": "2.2.0"},
    }
    try:
        async with httpx.AsyncClient(timeout=4.0, trust_env=False) as client:
            resp = await client.post(
                f"{target_base}/ilink/bot/getupdates",
                json=payload,
                headers=headers,
            )
        latency_ms = max(1, int((time.perf_counter() - started) * 1000))
        data = resp.json() if resp.content else {}
        if resp.status_code == 200 and isinstance(data, dict):
            ret = data.get("ret", 0)
            errcode = data.get("errcode", 0)
            if ret in (None, 0) and errcode in (None, 0):
                return ChannelProbeResponse(ok=True, latency_ms=latency_ms, checked_at=now)
            return ChannelProbeResponse(
                ok=False,
                latency_ms=latency_ms,
                checked_at=now,
                error=f"微信 iLink 凭证无效或会话已过期（errcode={errcode or ret}）：{data.get('errmsg') or '请重新扫码绑定'}",
            )
        return ChannelProbeResponse(
            ok=False,
            latency_ms=latency_ms,
            checked_at=now,
            error=f"微信 iLink 接口返回 HTTP {resp.status_code}",
        )
    except (httpx.TimeoutException, TimeoutError):
        # 1 秒长轮询超时说明服务端已接受 Token 并挂起连接等待消息，凭证有效
        latency_ms = max(1, int((time.perf_counter() - started) * 1000))
        return ChannelProbeResponse(ok=True, latency_ms=latency_ms, checked_at=now)
    except Exception as exc:
        latency_ms = max(1, int((time.perf_counter() - started) * 1000))
        return ChannelProbeResponse(
            ok=False,
            latency_ms=latency_ms,
            checked_at=now,
            error=f"连接微信 iLink 接口失败：{exc}",
        )


@router.put("/weixin", response_model=ChannelsOverviewResponse)
async def update_weixin_channel(
    body: WeixinChannelUpdateRequest,
    _user: Annotated[dict, Depends(require_agent_admin)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
) -> ChannelsOverviewResponse:
    """更新个人微信通道配置（支持绑定智能体与更新 Token）。"""
    config = load_gateway_config(runtime.workspace)
    weixin_cfg = dict((config.channel_configs or {}).get("weixin") or {})
    bots = _normalize_weixin_bots_from_config(weixin_cfg)
    target_name = (body.name or "").strip() or (
        str(bots[0].get("name") or "weixin-bot").strip() if bots else "weixin-bot"
    )
    match_idx = next(
        (i for i, b in enumerate(bots) if str(b.get("name") or "").strip() == target_name),
        0 if bots else None,
    )
    existing = bots[match_idx] if match_idx is not None else weixin_cfg
    token = body.token.strip() or str(existing.get("token") or "").strip()
    if body.enabled and not token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="启用个人微信通道需先扫码绑定或填写 iLink Token",
        )
    account_id = (
        body.account_id.strip()
        or str(existing.get("account_id") or "").strip()
        or "weixin"
    )
    bot_entry = {
        "name": target_name,
        "enabled": body.enabled,
        "account_id": account_id,
        "token": token,
        "base_url": body.base_url.strip() or str(existing.get("base_url") or _WEIXIN_ILINK_BASE).strip() or _WEIXIN_ILINK_BASE,
        "user_id": body.user_id.strip() or str(existing.get("user_id") or "").strip(),
        "default_agent": body.default_agent.strip(),
        "allow_from": [x.strip() for x in body.allow_from if x.strip()] or ["*"],
    }
    if match_idx is not None:
        bots[match_idx] = bot_entry
    elif token or body.account_id.strip():
        bots.append(bot_entry)
    return _save_weixin_bots_to_workspace(runtime.workspace, bots)


@router.delete("/weixin/bots/{bot_name}", response_model=ChannelsOverviewResponse)
async def delete_weixin_bot(
    bot_name: str,
    _user: Annotated[dict, Depends(require_agent_admin)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
) -> ChannelsOverviewResponse:
    """删除指定个人微信机器人配置。"""
    config = load_gateway_config(runtime.workspace)
    weixin_cfg = dict((config.channel_configs or {}).get("weixin") or {})
    bots = _normalize_weixin_bots_from_config(weixin_cfg)
    remaining = [b for b in bots if str(b.get("name") or "").strip() != bot_name]
    if len(remaining) == len(bots):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"未找到个人微信机器人「{bot_name}」",
        )
    return _save_weixin_bots_to_workspace(runtime.workspace, remaining)


@router.post("/weixin/probe", response_model=ChannelProbeResponse)
async def probe_weixin_draft(
    body: WeixinProbeRequest,
    _user: Annotated[dict, Depends(require_agent_admin)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
) -> ChannelProbeResponse:
    """测试个人微信 iLink 凭证连通性。"""
    token = body.token.strip()
    base_url = body.base_url.strip() or _WEIXIN_ILINK_BASE
    if not token:
        config = load_gateway_config(runtime.workspace)
        weixin_cfg = dict((config.channel_configs or {}).get("weixin") or {})
        bots = _normalize_weixin_bots_from_config(weixin_cfg)
        if body.bot_name:
            matched = next(
                (b for b in bots if str(b.get("name") or "").strip() == body.bot_name.strip()),
                None,
            )
            if matched:
                token = str(matched.get("token") or "").strip()
                base_url = str(matched.get("base_url") or base_url).strip()
        if not token and bots:
            token = str(bots[0].get("token") or "").strip()
            base_url = str(bots[0].get("base_url") or base_url).strip()
    return await probe_weixin_credentials(token, base_url)


@router.post("/weixin/qrcode/generate", response_model=WeixinQrGenerateResponse)
async def generate_weixin_qrcode(
    body: WeixinQrGenerateRequest,
    _user: Annotated[dict, Depends(require_agent_admin)],
) -> WeixinQrGenerateResponse:
    """生成个人微信 iLink 扫码登录二维码。"""
    try:
        started = await weixin_ilink_qr_generate()
    except Exception as exc:
        logger.exception("Failed to generate Weixin QR code")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"生成个人微信二维码失败：{exc}",
        ) from exc

    registration_id = secrets.token_urlsafe(24)
    now = time.monotonic()
    _prune_qr_sessions(now)
    allow_from = [str(x).strip() for x in (body.allow_from or []) if str(x).strip()] or ["*"]
    _weixin_qr_sessions[registration_id] = _WeixinRegistrationSession(
        registration_id=registration_id,
        qrcode_token=str(started["qrcode"]),
        qrcode_url=str(started["qrcode_url"]),
        expires_at=now + 300,
        interval=2,
        bot_name=body.bot_name.strip(),
        default_agent=body.default_agent.strip(),
        allow_from=allow_from,
    )
    return WeixinQrGenerateResponse(
        registration_id=registration_id,
        qrcode_url=str(started["qrcode_url"]),
        qrcode_token=str(started["qrcode"]),
        expires_in=300,
        interval=2,
    )


@router.post("/weixin/qrcode/poll", response_model=WeixinQrPollResponse)
async def poll_weixin_qrcode(
    body: WeixinQrPollRequest,
    _user: Annotated[dict, Depends(require_agent_admin)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
) -> WeixinQrPollResponse:
    """轮询个人微信扫码状态；在服务端复用单个 iLink 长轮询连接并在扫码确认后自动入库绑定。"""
    reg_id = (body.registration_id or "").strip()
    session = _weixin_qr_sessions.get(reg_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="微信扫码会话不存在或已过期，请重新生成二维码",
        )

    async with session.lock:
        if session.completed_weixin is not None:
            return WeixinQrPollResponse(
                status="success",
                weixin=session.completed_weixin,
            )
        if time.monotonic() >= session.expires_at:
            if session.poll_task and not session.poll_task.done():
                session.poll_task.cancel()
            _weixin_qr_sessions.pop(reg_id, None)
            return WeixinQrPollResponse(
                status="expired",
                message="二维码已过期，请点击重新生成",
            )

        if session.poll_task is None:
            session.poll_task = asyncio.create_task(
                weixin_ilink_qr_poll(session.qrcode_token)
            )

        done, _ = await asyncio.wait([session.poll_task], timeout=2.0)
        if not done:
            # 微信服务端长轮询仍在挂起等待扫码，保持后台任务不中断，立即返回当前状态
            return WeixinQrPollResponse(
                status=session.last_status,
                message="已扫码，请在手机微信上点击确认登录" if session.last_status == "scanned" else None,
            )

        try:
            result = session.poll_task.result()
        except Exception as exc:
            session.poll_task = None
            logger.exception("Failed to poll Weixin QR status")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"查询微信扫码状态失败：{exc}",
            ) from exc
        session.poll_task = None

        raw_status = str(result.get("status") or "wait").strip().lower()
        if raw_status == "wait":
            return WeixinQrPollResponse(status=session.last_status)
        if raw_status in ("scaned", "scanned"):
            session.last_status = "scanned"
            return WeixinQrPollResponse(
                status="scanned",
                message="已扫码，请在手机微信上点击确认登录",
            )
        if raw_status == "expired":
            _weixin_qr_sessions.pop(reg_id, None)
            return WeixinQrPollResponse(
                status="expired",
                message="二维码已过期，请点击重新生成",
            )
        if raw_status != "confirmed":
            return WeixinQrPollResponse(status=session.last_status)

        bot_token = str(result.get("bot_token") or "").strip()
        raw_bot_id = str(result.get("ilink_bot_id") or "weixin").strip()
        account_id = raw_bot_id.replace("@", "-").replace(".", "-") or "weixin"
        base_url = str(result.get("baseurl") or _WEIXIN_ILINK_BASE).strip() or _WEIXIN_ILINK_BASE
        user_id = str(result.get("ilink_user_id") or "").strip()
        if not bot_token:
            return WeixinQrPollResponse(
                status="failed",
                message="微信扫码已确认，但未返回有效的 bot_token",
            )

        preferred_name = body.bot_name.strip() or session.bot_name
        default_agent = body.default_agent.strip() or session.default_agent
        config = load_gateway_config(runtime.workspace)
        weixin_cfg = dict((config.channel_configs or {}).get("weixin") or {})
        bots = _normalize_weixin_bots_from_config(weixin_cfg)
        existing_names = {str(b.get("name") or "").strip() for b in bots}
        bot_name = _choose_auto_bot_name(
            existing_names,
            preferred_name or (f"weixin-{default_agent}" if default_agent else "weixin-bot"),
            default_agent,
        )
        allow_from = (
            [str(x).strip() for x in (body.allow_from or []) if str(x).strip()]
            or session.allow_from
            or ["*"]
        )
        new_weixin_bot = {
            "name": bot_name,
            "enabled": True,
            "account_id": account_id,
            "token": bot_token,
            "base_url": base_url,
            "user_id": user_id,
            "default_agent": default_agent,
            "allow_from": allow_from,
        }
        existing_idx = next(
            (
                i
                for i, b in enumerate(bots)
                if str(b.get("account_id") or "").strip() == account_id
                or (preferred_name and str(b.get("name") or "").strip() == preferred_name)
            ),
            None,
        )
        if existing_idx is not None:
            new_weixin_bot["name"] = str(bots[existing_idx].get("name") or bot_name).strip()
            bots[existing_idx] = new_weixin_bot
        else:
            bots.append(new_weixin_bot)

        _save_weixin_bots_to_workspace(runtime.workspace, bots)
        weixin_resp = _weixin_dict_to_response(new_weixin_bot, channel_enabled=True)
        session.completed_weixin = weixin_resp
        return WeixinQrPollResponse(status="success", weixin=weixin_resp)


# ─── 企业微信（WeCom 智能机器人）通道配置与扫码绑定 ───────────────────────────


async def wecom_qr_generate() -> dict[str, Any]:
    """调用企业微信智能机器人接口生成扫码创建二维码。"""
    async with httpx.AsyncClient(
        base_url=_WECOM_QR_BASE, timeout=15.0, trust_env=False
    ) as client:
        resp = await client.get(
            "/ai/qc/generate",
            params={"source": "octop", "plat": "2"},
        )
        resp.raise_for_status()
        raw = resp.json()
        data = raw.get("data") if isinstance(raw, dict) and isinstance(raw.get("data"), dict) else raw
        if not isinstance(data, dict):
            raise RuntimeError("企业微信扫码接口返回格式无效")
        scode = str(data.get("scode") or "").strip()
        auth_url = str(data.get("auth_url") or "").strip()
        if not scode or not auth_url:
            raise RuntimeError(f"企业微信未返回有效的扫码凭证：{raw}")
        return {"scode": scode, "auth_url": auth_url}


async def wecom_qr_poll(scode: str) -> dict[str, Any]:
    """轮询企业微信智能机器人扫码结果。"""
    async with httpx.AsyncClient(
        base_url=_WECOM_QR_BASE, timeout=15.0, trust_env=False
    ) as client:
        resp = await client.get(
            "/ai/qc/query_result",
            params={"scode": scode},
        )
        resp.raise_for_status()
        raw = resp.json()
        data = raw.get("data") if isinstance(raw, dict) and isinstance(raw.get("data"), dict) else raw
        return data if isinstance(data, dict) else {"status": "waiting"}


@router.put("/wecom", response_model=ChannelsOverviewResponse)
async def update_wecom_channel(
    body: WecomChannelUpdateRequest,
    _user: Annotated[dict, Depends(require_agent_admin)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
) -> ChannelsOverviewResponse:
    """更新企业微信智能机器人通道配置。"""
    config = load_gateway_config(runtime.workspace)
    wecom_cfg = dict((config.channel_configs or {}).get("wecom") or {})
    bots = _normalize_wecom_bots_from_config(wecom_cfg)
    target_name = (body.name or "").strip() or (
        str(bots[0].get("name") or "wecom-bot").strip() if bots else "wecom-bot"
    )
    match_idx = next(
        (i for i, b in enumerate(bots) if str(b.get("name") or "").strip() == target_name),
        0 if bots else None,
    )
    existing = bots[match_idx] if match_idx is not None else wecom_cfg
    secret = body.secret.strip() or str(existing.get("secret") or "").strip()
    if body.enabled and (not body.bot_id.strip() or not secret):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="启用企业微信通道需填写 Bot ID 与 Secret",
        )
    bot_entry = {
        "name": target_name,
        "enabled": body.enabled,
        "bot_id": body.bot_id.strip(),
        "secret": secret,
        "ws_url": body.ws_url.strip() or _WECOM_DEFAULT_WS_URL,
        "default_agent": body.default_agent.strip(),
        "allow_from": [x.strip() for x in body.allow_from if x.strip()] or ["*"],
    }
    if match_idx is not None:
        bots[match_idx] = bot_entry
    elif body.bot_id.strip() or secret:
        bots.append(bot_entry)
    return _save_wecom_bots_to_workspace(runtime.workspace, bots)


@router.delete("/wecom/bots/{bot_name}", response_model=ChannelsOverviewResponse)
async def delete_wecom_bot(
    bot_name: str,
    _user: Annotated[dict, Depends(require_agent_admin)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
) -> ChannelsOverviewResponse:
    """删除指定企业微信机器人配置。"""
    config = load_gateway_config(runtime.workspace)
    wecom_cfg = dict((config.channel_configs or {}).get("wecom") or {})
    bots = _normalize_wecom_bots_from_config(wecom_cfg)
    remaining = [b for b in bots if str(b.get("name") or "").strip() != bot_name]
    if len(remaining) == len(bots):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"未找到企业微信机器人「{bot_name}」",
        )
    return _save_wecom_bots_to_workspace(runtime.workspace, remaining)


@router.post("/wecom/probe", response_model=ChannelProbeResponse)
async def probe_wecom_draft(
    body: WecomProbeRequest,
    _user: Annotated[dict, Depends(require_agent_admin)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
) -> ChannelProbeResponse:
    """测试企业微信智能机器人凭证连通性。"""
    from openharness.channels.impl.wecom import probe_wecom_credentials

    started = time.perf_counter()
    now = time.time()
    bot_id = body.bot_id.strip()
    secret = body.secret.strip()
    ws_url = body.ws_url.strip() or _WECOM_DEFAULT_WS_URL
    if not secret:
        config = load_gateway_config(runtime.workspace)
        wecom_cfg = dict((config.channel_configs or {}).get("wecom") or {})
        bots = _normalize_wecom_bots_from_config(wecom_cfg)
        if body.bot_name:
            matched = next(
                (b for b in bots if str(b.get("name") or "").strip() == body.bot_name.strip()),
                None,
            )
            if matched:
                secret = str(matched.get("secret") or "").strip()
                bot_id = bot_id or str(matched.get("bot_id") or "").strip()
        if not secret and bot_id:
            matched_by_id = next(
                (b for b in bots if str(b.get("bot_id") or "").strip() == bot_id),
                None,
            )
            if matched_by_id:
                secret = str(matched_by_id.get("secret") or "").strip()
        if not secret and bots:
            secret = str(bots[0].get("secret") or "").strip()
            bot_id = bot_id or str(bots[0].get("bot_id") or "").strip()

    ok, err = await probe_wecom_credentials(bot_id, secret, ws_url)
    latency_ms = max(1, int((time.perf_counter() - started) * 1000))
    return ChannelProbeResponse(ok=ok, latency_ms=latency_ms, checked_at=now, error=err)


@router.post("/wecom/qrcode/generate", response_model=WecomQrGenerateResponse)
async def generate_wecom_qrcode(
    body: WecomQrGenerateRequest,
    _user: Annotated[dict, Depends(require_agent_admin)],
) -> WecomQrGenerateResponse:
    """生成企业微信智能机器人扫码注册二维码。"""
    try:
        started = await wecom_qr_generate()
    except Exception as exc:
        logger.exception("Failed to generate WeCom QR code")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"生成企业微信二维码失败：{exc}",
        ) from exc

    registration_id = secrets.token_urlsafe(24)
    now = time.monotonic()
    _prune_qr_sessions(now)
    allow_from = [str(x).strip() for x in (body.allow_from or []) if str(x).strip()] or ["*"]
    _wecom_qr_sessions[registration_id] = _WecomRegistrationSession(
        registration_id=registration_id,
        scode=str(started["scode"]),
        qrcode_url=str(started["auth_url"]),
        expires_at=now + 300,
        interval=2,
        bot_name=body.bot_name.strip(),
        default_agent=body.default_agent.strip(),
        allow_from=allow_from,
    )
    return WecomQrGenerateResponse(
        registration_id=registration_id,
        qrcode_url=str(started["auth_url"]),
        scode=str(started["scode"]),
        expires_in=300,
        interval=2,
    )


@router.post("/wecom/qrcode/poll", response_model=WecomQrPollResponse)
async def poll_wecom_qrcode(
    body: WecomQrPollRequest,
    _user: Annotated[dict, Depends(require_agent_admin)],
    runtime: Annotated[_RuntimeState, Depends(get_runtime)],
) -> WecomQrPollResponse:
    """轮询企业微信智能机器人扫码状态；成功后自动保存 Bot ID 与 Secret 并绑定指定智能体。"""
    reg_id = (body.registration_id or "").strip()
    session = _wecom_qr_sessions.get(reg_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="企业微信扫码会话不存在或已过期，请重新生成二维码",
        )

    async with session.lock:
        if session.completed_wecom is not None:
            return WecomQrPollResponse(
                status="success",
                wecom=session.completed_wecom,
            )
        if time.monotonic() >= session.expires_at:
            _wecom_qr_sessions.pop(reg_id, None)
            return WecomQrPollResponse(
                status="expired",
                message="二维码已过期，请点击重新生成",
            )

        try:
            result = await wecom_qr_poll(session.scode)
        except Exception as exc:
            logger.exception("Failed to poll WeCom QR status")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"查询企业微信扫码状态失败：{exc}",
            ) from exc

        poll_status = str(result.get("status") or "waiting").strip().lower()
        if poll_status in ("waiting", "wait", "pending"):
            return WecomQrPollResponse(status="waiting")
        if poll_status == "expired":
            _wecom_qr_sessions.pop(reg_id, None)
            return WecomQrPollResponse(
                status="expired",
                message="二维码已过期，请点击重新生成",
            )
        if poll_status != "success":
            _wecom_qr_sessions.pop(reg_id, None)
            return WecomQrPollResponse(
                status="failed",
                message=str(result.get("errmsg") or result.get("reason") or f"企业微信扫码失败：{poll_status}"),
            )

        bot_info = result.get("bot_info") if isinstance(result.get("bot_info"), dict) else {}
        bot_id = str(bot_info.get("botid") or bot_info.get("bot_id") or result.get("bot_id") or "").strip()
        secret = str(bot_info.get("secret") or result.get("secret") or "").strip()
        if not bot_id or not secret:
            return WecomQrPollResponse(
                status="failed",
                message="企业微信扫码成功但未返回完整的 Bot ID 与 Secret",
            )

        preferred_name = body.bot_name.strip() or session.bot_name
        default_agent = body.default_agent.strip() or session.default_agent
        config = load_gateway_config(runtime.workspace)
        wecom_cfg = dict((config.channel_configs or {}).get("wecom") or {})
        bots = _normalize_wecom_bots_from_config(wecom_cfg)
        existing_names = {str(b.get("name") or "").strip() for b in bots}
        bot_name = _choose_auto_bot_name(
            existing_names,
            preferred_name or (f"wecom-{default_agent}" if default_agent else "wecom-bot"),
            default_agent,
        )
        allow_from = (
            [str(x).strip() for x in (body.allow_from or []) if str(x).strip()]
            or session.allow_from
            or ["*"]
        )
        new_wecom_bot = {
            "name": bot_name,
            "enabled": True,
            "bot_id": bot_id,
            "secret": secret,
            "ws_url": _WECOM_DEFAULT_WS_URL,
            "default_agent": default_agent,
            "allow_from": allow_from,
        }
        existing_idx = next(
            (
                i
                for i, b in enumerate(bots)
                if str(b.get("bot_id") or "").strip() == bot_id
                or (preferred_name and str(b.get("name") or "").strip() == preferred_name)
            ),
            None,
        )
        if existing_idx is not None:
            new_wecom_bot["name"] = str(bots[existing_idx].get("name") or bot_name).strip()
            bots[existing_idx] = new_wecom_bot
        else:
            bots.append(new_wecom_bot)

        _save_wecom_bots_to_workspace(runtime.workspace, bots)
        wecom_resp = _wecom_dict_to_response(new_wecom_bot, channel_enabled=True)
        session.completed_wecom = wecom_resp
        return WecomQrPollResponse(status="success", wecom=wecom_resp)
