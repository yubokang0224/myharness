# -*- coding: utf-8 -*-
"""个人微信（WeChat iLink Bot）通道实现：基于 HTTP 长轮询收发消息，无需公网回调地址。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import json
import logging
import random
import struct
import time
import uuid
from typing import Any
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from openharness.channels.bus.events import OutboundMessage
from openharness.channels.bus.queue import MessageBus
from openharness.channels.impl.base import BaseChannel, resolve_channel_media_dir
from openharness.config.schema import WeixinBotConfig, WeixinConfig

logger = logging.getLogger(__name__)

DEFAULT_WEIXIN_BASE_URL = "https://ilinkai.weixin.qq.com"
CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"

_EP_GET_UPDATES = "ilink/bot/getupdates"
_EP_SEND_MESSAGE = "ilink/bot/sendmessage"
_EP_GET_CONFIG = "ilink/bot/getconfig"
_EP_SEND_TYPING = "ilink/bot/sendtyping"

_CHANNEL_VERSION = "2.2.0"
_AUTH_TYPE = "ilink_bot_token"
_ILINK_APP_ID = "bot"
_ILINK_APP_CLIENT_VERSION = str((2 << 16) | (2 << 8) | 0)

_MSG_TYPE_USER = 1
_MSG_TYPE_BOT = 2
_MSG_STATE_FINISH = 2

_ITEM_TYPE_TEXT = 1
_ITEM_TYPE_IMAGE = 2
_ITEM_TYPE_VOICE = 3
_ITEM_TYPE_FILE = 4
_ITEM_TYPE_VIDEO = 5

_TYPING_START = 1
_TYPING_STOP = 2

_SESSION_EXPIRED_CODE = -14
_SESSION_PAUSE_S = 300.0
WEIXIN_TEXT_LIMIT = 2000


def _wechat_uin() -> str:
    """生成随机 X-WECHAT-UIN 请求头（4 字节无符号整数的 base64）。"""
    return base64.b64encode(struct.pack(">I", random.getrandbits(32))).decode("ascii")


def _client_id() -> str:
    return f"ohmo-weixin-{uuid.uuid4().hex}"


def split_weixin_text(text: str, limit: int = WEIXIN_TEXT_LIMIT) -> list[str]:
    """将长文本按段落/换行拆分为不超过微信单条上限的分片。"""
    remaining = (text or "").strip()
    if not remaining:
        return []
    if len(remaining) <= limit:
        return [remaining]

    parts: list[str] = []
    while remaining:
        if len(remaining) <= limit:
            parts.append(remaining)
            break
        window = remaining[:limit]
        cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
        if cut <= 0:
            cut = limit
        piece = remaining[:cut].strip()
        remaining = remaining[cut:].strip()
        if piece:
            parts.append(piece)
    return parts


def _decode_aes_key(value: str) -> bytes | None:
    if not value:
        return None
    try:
        raw = bytes.fromhex(value)
        return raw[:16] if len(raw) >= 16 else raw.ljust(16, b"\x00")
    except (TypeError, ValueError):
        pass
    try:
        decoded = base64.b64decode(value).decode("ascii")
        raw = bytes.fromhex(decoded)
        return raw[:16] if len(raw) >= 16 else raw.ljust(16, b"\x00")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None


def _aes_ecb_decrypt(data: bytes, key: bytes) -> bytes:
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    padded = decryptor.update(data) + decryptor.finalize()
    unpadder = PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def _detect_extension(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data.startswith(b"%PDF"):
        return ".pdf"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    return ".bin"


class WeixinAPIError(Exception):
    """微信 iLink 接口返回非 0 状态码时抛出的异常。"""

    def __init__(self, ret: int, errcode: int | None = None, errmsg: str | None = None) -> None:
        self.ret = ret
        self.errcode = errcode
        self.errmsg = errmsg
        msg = f"Weixin API error: ret={ret}"
        if errcode is not None:
            msg += f", errcode={errcode}"
        if errmsg:
            msg += f", errmsg={errmsg}"
        super().__init__(msg)


class WeixinChannel(BaseChannel):
    """基于微信 iLink Bot API 的个人微信消息通道。"""

    name = "weixin"

    def __init__(
        self,
        config: WeixinConfig | WeixinBotConfig,
        bus: MessageBus,
        *,
        channel_name: str = "weixin",
        bot_name: str | None = None,
        default_agent: str | None = None,
    ) -> None:
        super().__init__(config, bus)
        self.name = channel_name
        self.bot_name = bot_name or getattr(config, "name", "") or "default"
        resolved_agent = (
            default_agent
            if default_agent is not None
            else getattr(config, "default_agent", "")
        )
        self.default_agent = (resolved_agent or "").strip() or None
        self.account_id = (
            str(getattr(config, "account_id", "") or getattr(config, "bot_uin", "") or self.bot_name).strip()
            or "weixin"
        )
        self.base_url = (
            str(getattr(config, "base_url", "") or DEFAULT_WEIXIN_BASE_URL).strip().rstrip("/")
            or DEFAULT_WEIXIN_BASE_URL
        )
        self.token = str(getattr(config, "token", "") or "").strip()
        self._media_dir = resolve_channel_media_dir(self.name)
        self._client: httpx.AsyncClient | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._sync_buf: str = ""
        self._context_tokens: dict[str, str] = {}
        self._session_pause_until: float = 0.0

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "AuthorizationType": _AUTH_TYPE,
            "Authorization": f"Bearer {self.token}",
            "X-WECHAT-UIN": _wechat_uin(),
            "iLink-App-Id": _ILINK_APP_ID,
            "iLink-App-ClientVersion": _ILINK_APP_CLIENT_VERSION,
        }

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=45.0, follow_redirects=True)
        return self._client

    async def _post_json(
        self,
        endpoint: str,
        payload: dict[str, Any],
        *,
        timeout_s: float = 45.0,
    ) -> dict[str, Any]:
        client = await self._ensure_client()
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        body_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = self._headers()
        headers["Content-Length"] = str(len(body_bytes))
        resp = await client.post(url, content=body_bytes, headers=headers, timeout=timeout_s)
        if resp.status_code != 200:
            raise WeixinAPIError(
                ret=resp.status_code,
                errcode=resp.status_code,
                errmsg=f"HTTP {resp.status_code}: {resp.text[:160]}",
            )
        data = resp.json()
        if not isinstance(data, dict):
            raise WeixinAPIError(ret=-1, errcode=-1, errmsg="Invalid JSON response")
        ret = data.get("ret")
        errcode = data.get("errcode")
        if errcode not in (None, 0):
            raise WeixinAPIError(ret=int(ret or errcode or -1), errcode=int(errcode), errmsg=str(data.get("errmsg") or ""))
        if ret not in (None, 0):
            raise WeixinAPIError(ret=int(ret), errcode=errcode, errmsg=str(data.get("errmsg") or ""))
        return data

    async def start(self) -> None:
        """启动个人微信 iLink 长轮询循环。"""
        if not self.token:
            logger.error("Weixin channel %s not configured (missing token)", self.name)
            return
        self._running = True
        await self._ensure_client()
        logger.info("Weixin channel %s started (account_id=%s)", self.name, self.account_id)
        await self._poll_loop()

    async def stop(self) -> None:
        """停止个人微信长轮询。"""
        self._running = False
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
        self._poll_task = None
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        self._client = None
        logger.info("Weixin channel %s stopped", self.name)

    async def _poll_loop(self) -> None:
        timeout_ms = 35000
        failures = 0
        while self._running:
            now = time.monotonic()
            if self._session_pause_until > now:
                await asyncio.sleep(min(self._session_pause_until - now, 30.0))
                continue
            try:
                data = await self._post_json(
                    _EP_GET_UPDATES,
                    {
                        "get_updates_buf": self._sync_buf,
                        "longpolling_timeout_ms": timeout_ms,
                        "base_info": {"channel_version": _CHANNEL_VERSION},
                    },
                    timeout_s=(timeout_ms / 1000.0) + 10.0,
                )
                failures = 0
                new_buf = str(data.get("get_updates_buf") or data.get("getUpdatesBuf") or "")
                if new_buf:
                    self._sync_buf = new_buf
                next_timeout = data.get("longpolling_timeout_ms") or data.get("longpollingTimeoutMs")
                if isinstance(next_timeout, int) and next_timeout > 0:
                    timeout_ms = next_timeout

                msgs = data.get("msgs")
                if isinstance(msgs, list):
                    for raw_msg in msgs:
                        if isinstance(raw_msg, dict):
                            await self._handle_raw_weixin_msg(raw_msg)
            except asyncio.CancelledError:
                break
            except (httpx.TimeoutException, TimeoutError):
                # 长轮询空闲超时属于正常现象，直接发起下一轮
                continue
            except WeixinAPIError as exc:
                if exc.errcode == _SESSION_EXPIRED_CODE or exc.ret == _SESSION_EXPIRED_CODE:
                    logger.error(
                        "Weixin %s session expired (errcode -14); pausing %.0f min, please re-scan QR code.",
                        self.name,
                        _SESSION_PAUSE_S / 60,
                    )
                    self._session_pause_until = time.monotonic() + _SESSION_PAUSE_S
                    failures = 0
                    continue
                failures += 1
                logger.warning("Weixin %s poll failed: %s (attempt %d)", self.name, exc, failures)
                await asyncio.sleep(min(2**failures, 60))
            except Exception:
                failures += 1
                logger.exception("Weixin %s unexpected poll error", self.name)
                await asyncio.sleep(min(2**failures, 60))

    async def _handle_raw_weixin_msg(self, raw_msg: dict[str, Any]) -> None:
        msg_type = raw_msg.get("message_type") or raw_msg.get("messageType")
        if msg_type != _MSG_TYPE_USER:
            return
        from_user_id = str(raw_msg.get("from_user_id") or raw_msg.get("fromUserId") or "").strip()
        if not from_user_id:
            return
        context_token = str(raw_msg.get("context_token") or raw_msg.get("contextToken") or "").strip()
        if context_token:
            self._context_tokens[from_user_id] = context_token

        text_parts: list[str] = []
        media_paths: list[str] = []
        items = raw_msg.get("item_list") or raw_msg.get("itemList") or []
        if isinstance(items, list):
            for idx, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type")
                if item_type == _ITEM_TYPE_TEXT:
                    text_item = item.get("text_item") or item.get("textItem") or {}
                    if isinstance(text_item, dict):
                        txt = str(text_item.get("text") or "").strip()
                        if txt:
                            text_parts.append(txt)
                elif item_type == _ITEM_TYPE_VOICE:
                    voice_item = item.get("voice_item") or item.get("voiceItem") or {}
                    if isinstance(voice_item, dict):
                        txt = str(voice_item.get("text") or "").strip()
                        if txt:
                            text_parts.append(txt)
                elif item_type in (_ITEM_TYPE_IMAGE, _ITEM_TYPE_FILE, _ITEM_TYPE_VIDEO):
                    saved = await self._download_item_media(item, item_type, idx)
                    if saved:
                        media_paths.append(saved)

        content = "\n".join(text_parts).strip()
        if not content and media_paths:
            content = "[图片]" if any(p.endswith((".png", ".jpg", ".webp", ".gif")) for p in media_paths) else "[文件]"
        if not content and not media_paths:
            return

        # 异步点亮微信“对方正在输入...”状态灯
        asyncio.create_task(self._send_typing(from_user_id, context_token, _TYPING_START))

        await self._handle_message(
            sender_id=from_user_id,
            chat_id=from_user_id,
            content=content,
            media=media_paths,
            metadata={
                "platform": "weixin",
                "bot_name": self.bot_name,
                "default_agent": self.default_agent,
                "account_id": self.account_id,
                "from_user_id": from_user_id,
                "context_token": context_token,
                "message_id": str(raw_msg.get("message_id") or raw_msg.get("messageId") or ""),
            },
        )

    async def _download_item_media(
        self,
        item: dict[str, Any],
        item_type: int,
        index: int,
    ) -> str | None:
        key_map = {
            _ITEM_TYPE_IMAGE: ("image_item", "imageItem"),
            _ITEM_TYPE_FILE: ("file_item", "fileItem"),
            _ITEM_TYPE_VIDEO: ("video_item", "videoItem"),
        }
        keys = key_map.get(item_type, ("image_item", "imageItem"))
        sub = item.get(keys[0]) or item.get(keys[1]) or {}
        if not isinstance(sub, dict):
            return None
        media = sub.get("media")
        if not isinstance(media, dict):
            return None
        encrypt_param = str(media.get("encrypt_query_param") or media.get("encryptQueryParam") or "").strip()
        if not encrypt_param:
            return None
        extra_key = str(sub.get("aeskey") or sub.get("aesKey") or media.get("aes_key") or media.get("aesKey") or "").strip()
        url = f"{CDN_BASE_URL}/download?encrypted_query_param={quote(encrypt_param, safe='')}"
        try:
            client = await self._ensure_client()
            resp = await client.get(url, timeout=20.0)
            if resp.status_code != 200:
                return None
            raw = resp.content
            aes_key = _decode_aes_key(extra_key)
            if aes_key and len(raw) % 16 == 0:
                with contextlib.suppress(Exception):
                    raw = _aes_ecb_decrypt(raw, aes_key)
            ext = _detect_extension(raw)
            filename = str(sub.get("file_name") or sub.get("fileName") or f"weixin_{int(time.time())}_{index}{ext}")
            target = self._media_dir / filename
            target.write_bytes(raw)
            return str(target)
        except Exception:
            logger.debug("Weixin media download failed", exc_info=True)
            return None

    async def _send_typing(self, user_id: str, context_token: str, status_code: int) -> None:
        if not user_id:
            return
        token = context_token or self._context_tokens.get(user_id, "")
        try:
            cfg = await self._post_json(
                _EP_GET_CONFIG,
                {
                    "ilink_user_id": user_id,
                    "context_token": token,
                    "base_info": {"channel_version": _CHANNEL_VERSION},
                },
                timeout_s=4.0,
            )
            ticket = str(cfg.get("typing_ticket") or cfg.get("typingTicket") or "").strip()
            if not ticket:
                return
            await self._post_json(
                _EP_SEND_TYPING,
                {
                    "ilink_user_id": user_id,
                    "typing_ticket": ticket,
                    "status": status_code,
                    "base_info": {"channel_version": _CHANNEL_VERSION},
                },
                timeout_s=4.0,
            )
        except Exception:
            logger.debug("Weixin typing status=%s skipped", status_code)

    async def send(self, msg: OutboundMessage) -> None:
        """通过微信 iLink sendMessage 接口向用户发送回复。"""
        to_user_id = str(msg.chat_id or msg.metadata.get("from_user_id") or "").strip()
        if not to_user_id:
            logger.warning("Weixin send skipped: empty chat_id")
            return
        context_token = (
            self._context_tokens.get(to_user_id)
            or str(msg.metadata.get("context_token") or "").strip()
        )
        # 进度提示仅刷新“正在输入”状态，不重复推送气泡以免刷屏
        if msg.metadata.get("_progress"):
            await self._send_typing(to_user_id, context_token, _TYPING_START)
            return

        await self._send_typing(to_user_id, context_token, _TYPING_STOP)
        parts = split_weixin_text(msg.content)
        for part in parts:
            payload = {
                "msg": {
                    "from_user_id": "",
                    "to_user_id": to_user_id,
                    "client_id": _client_id(),
                    "message_type": _MSG_TYPE_BOT,
                    "message_state": _MSG_STATE_FINISH,
                    "context_token": context_token or None,
                    "item_list": [{"type": _ITEM_TYPE_TEXT, "text_item": {"text": part}}],
                },
                "base_info": {"channel_version": _CHANNEL_VERSION},
            }
            resp = await self._post_json(_EP_SEND_MESSAGE, payload, timeout_s=20.0)
            refreshed = str(
                resp.get("context_token")
                or resp.get("contextToken")
                or (resp.get("data") or {}).get("context_token")
                or ""
            ).strip()
            if refreshed:
                self._context_tokens[to_user_id] = refreshed
                context_token = refreshed
