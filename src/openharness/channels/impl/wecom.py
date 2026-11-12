# -*- coding: utf-8 -*-
"""企业微信（WeCom 智能机器人）通道实现：基于 WebSocket 长连接收发消息与流式回复。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from typing import Any

import httpx
import websockets
import websockets.asyncio.client

from openharness.channels.bus.events import OutboundMessage
from openharness.channels.bus.queue import MessageBus
from openharness.channels.impl.base import BaseChannel, resolve_channel_media_dir
from openharness.config.schema import WecomBotConfig, WecomConfig

logger = logging.getLogger(__name__)

DEFAULT_WECOM_WS_URL = "wss://openws.work.weixin.qq.com"

_CMD_SUBSCRIBE = "aibot_subscribe"
_CMD_HEARTBEAT = "ping"
_CMD_RESPONSE = "aibot_respond_msg"
_CMD_SEND_MSG = "aibot_send_msg"
_CMD_CALLBACK = "aibot_msg_callback"


def _gen_req_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"


async def probe_wecom_credentials(
    bot_id: str,
    secret: str,
    ws_url: str = DEFAULT_WECOM_WS_URL,
    timeout_s: float = 10.0,
) -> tuple[bool, str | None]:
    """通过企业微信智能机器人 WebSocket 发送 aibot_subscribe 认证帧校验凭证有效性。"""
    cleaned_id = (bot_id or "").strip()
    cleaned_secret = (secret or "").strip()
    target_url = (ws_url or DEFAULT_WECOM_WS_URL).strip() or DEFAULT_WECOM_WS_URL
    if not cleaned_id or not cleaned_secret:
        return False, "请先提供企业微信智能机器人的 Bot ID 和 Secret"

    req_id = _gen_req_id(_CMD_SUBSCRIBE)
    try:
        async with websockets.asyncio.client.connect(
            target_url,
            ping_interval=None,
            ping_timeout=None,
            close_timeout=3,
            open_timeout=timeout_s,
        ) as ws:
            auth_frame = {
                "cmd": _CMD_SUBSCRIBE,
                "headers": {"req_id": req_id},
                "body": {"bot_id": cleaned_id, "secret": cleaned_secret},
            }
            await ws.send(json.dumps(auth_frame, ensure_ascii=False))
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.5, deadline - time.monotonic()))
                frame = json.loads(raw)
                if not isinstance(frame, dict):
                    continue
                frame_req_id = str((frame.get("headers") or {}).get("req_id") or "")
                if frame_req_id == req_id or frame_req_id.startswith(_CMD_SUBSCRIBE):
                    errcode = int(frame.get("errcode", -1))
                    if errcode == 0:
                        return True, None
                    errmsg = str(frame.get("errmsg") or f"errcode={errcode}")
                    return False, f"企业微信认证失败: {errmsg}"
        return False, "企业微信认证超时"
    except Exception as exc:
        return False, f"连接企业微信网关失败: {exc}"


class WeComChannel(BaseChannel):
    """企业微信智能机器人 WebSocket 长连接通道。"""

    name = "wecom"

    def __init__(
        self,
        config: WecomConfig | WecomBotConfig,
        bus: MessageBus,
        *,
        channel_name: str = "wecom",
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
        self.bot_id = str(getattr(config, "bot_id", "") or "").strip()
        self.secret = str(getattr(config, "secret", "") or "").strip()
        self.ws_url = (
            str(getattr(config, "ws_url", "") or DEFAULT_WECOM_WS_URL).strip()
            or DEFAULT_WECOM_WS_URL
        )
        self._media_dir = resolve_channel_media_dir(self.name)
        self._ws_client: Any = None
        self._ws_conn: websockets.asyncio.client.ClientConnection | None = None
        self._http_client: httpx.AsyncClient | None = None
        # chat_id -> (frame, stream_id, created_monotonic, response_url)
        self._active_streams: dict[str, tuple[dict[str, Any], str, float, str]] = {}

    async def start(self) -> None:
        """启动企业微信 WebSocket 长连接。"""
        if not self.bot_id or not self.secret:
            logger.error("WeCom channel %s not configured (missing bot_id or secret)", self.name)
            return
        self._running = True

        # 优先使用官方 wecom_aibot_sdk，若不可用则回退到内置 websockets 长连接循环
        try:
            from wecom_aibot_sdk import WSClient

            self._ws_client = WSClient(
                bot_id=self.bot_id,
                secret=self.secret,
                ws_url=self.ws_url,
                max_reconnect_attempts=-1,
                heartbeat_interval=30000,
            )
            self._ws_client.on("authenticated", self._on_sdk_authenticated)
            self._ws_client.on("disconnected", self._on_sdk_disconnected)
            self._ws_client.on("error", self._on_sdk_error)
            self._ws_client.on("message", self._on_sdk_message)
            await self._ws_client.connect()
            logger.info("WeCom channel %s started via wecom_aibot_sdk (bot_id=%s)", self.name, self.bot_id[:12])
            while self._running:
                await asyncio.sleep(1.0)
            return
        except ImportError:
            logger.info("wecom_aibot_sdk not installed, using builtin WebSocket loop for %s", self.name)

        await self._builtin_ws_loop()

    async def stop(self) -> None:
        """停止企业微信 WebSocket 长连接。"""
        self._running = False
        if self._ws_client is not None:
            with contextlib.suppress(Exception):
                await self._ws_client.disconnect()
            self._ws_client = None
        if self._ws_conn is not None:
            with contextlib.suppress(Exception):
                await self._ws_conn.close()
            self._ws_conn = None
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()
        self._http_client = None
        logger.info("WeCom channel %s stopped", self.name)

    def _on_sdk_authenticated(self) -> None:
        logger.info("WeCom channel %s WebSocket authenticated", self.name)

    def _on_sdk_disconnected(self, reason: str = "") -> None:
        logger.warning("WeCom channel %s WebSocket disconnected: %s", self.name, reason)

    def _on_sdk_error(self, error: Exception) -> None:
        logger.error("WeCom channel %s WebSocket error: %s", self.name, error)

    async def _on_sdk_message(self, frame: dict[str, Any]) -> None:
        await self._handle_inbound_frame(frame)

    async def _builtin_ws_loop(self) -> None:
        failures = 0
        while self._running:
            try:
                async with websockets.asyncio.client.connect(
                    self.ws_url,
                    ping_interval=None,
                    ping_timeout=None,
                    close_timeout=5,
                ) as ws:
                    self._ws_conn = ws
                    sub_id = _gen_req_id(_CMD_SUBSCRIBE)
                    await ws.send(
                        json.dumps(
                            {
                                "cmd": _CMD_SUBSCRIBE,
                                "headers": {"req_id": sub_id},
                                "body": {"bot_id": self.bot_id, "secret": self.secret},
                            },
                            ensure_ascii=False,
                        )
                    )
                    hb_task = asyncio.create_task(self._builtin_heartbeat(ws))
                    try:
                        async for raw in ws:
                            failures = 0
                            try:
                                frame = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            if isinstance(frame, dict) and frame.get("cmd") == _CMD_CALLBACK:
                                asyncio.create_task(self._handle_inbound_frame(frame))
                    finally:
                        hb_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await hb_task
                        self._ws_conn = None
            except asyncio.CancelledError:
                break
            except Exception as exc:
                failures += 1
                logger.warning("WeCom %s connection lost (%s), reconnecting...", self.name, exc)
                await asyncio.sleep(min(2**failures, 30))

    async def _builtin_heartbeat(self, ws: websockets.asyncio.client.ClientConnection) -> None:
        while self._running:
            await asyncio.sleep(30.0)
            await ws.send(
                json.dumps(
                    {"cmd": _CMD_HEARTBEAT, "headers": {"req_id": _gen_req_id(_CMD_HEARTBEAT)}},
                    ensure_ascii=False,
                )
            )

    async def _handle_inbound_frame(self, frame: dict[str, Any]) -> None:
        body = frame.get("body") if isinstance(frame.get("body"), dict) else frame
        if not isinstance(body, dict):
            return
        msg_type = str(body.get("msgtype") or "text").strip()
        if msg_type not in ("text", "image", "file", "voice", "video", "mixed"):
            return

        sender_info = body.get("from") if isinstance(body.get("from"), dict) else {}
        sender_id = str(sender_info.get("userid") or sender_info.get("user_id") or "unknown").strip()
        chat_type = str(body.get("chattype") or "single").strip()
        chat_id = str(body.get("chatid") or sender_id).strip()
        response_url = str(body.get("response_url") or "").strip()

        text_parts: list[str] = []
        media_paths: list[str] = []

        if msg_type == "text":
            text_obj = body.get("text")
            txt = text_obj.get("content", "") if isinstance(text_obj, dict) else str(text_obj or "")
            if txt.strip():
                text_parts.append(txt.strip())
        elif msg_type == "mixed":
            mixed_obj = body.get("mixed") if isinstance(body.get("mixed"), dict) else {}
            items = mixed_obj.get("msg_item") if isinstance(mixed_obj.get("msg_item"), list) else []
            for idx, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                itype = str(item.get("msgtype") or "")
                if itype == "text":
                    tobj = item.get("text")
                    tval = tobj.get("content", "") if isinstance(tobj, dict) else ""
                    if tval.strip():
                        text_parts.append(tval.strip())
                elif itype == "image":
                    img_obj = item.get("image") if isinstance(item.get("image"), dict) else {}
                    saved = await self._download_wecom_media(img_obj, idx, ".png")
                    if saved:
                        media_paths.append(saved)
        elif msg_type in ("image", "file", "voice", "video"):
            media_obj = body.get(msg_type) if isinstance(body.get(msg_type), dict) else {}
            default_ext = ".png" if msg_type == "image" else ".bin"
            saved = await self._download_wecom_media(media_obj, 0, default_ext)
            if saved:
                media_paths.append(saved)

        content = "\n".join(text_parts).strip()
        if not content and media_paths:
            content = "[图片]" if msg_type == "image" else "[文件]"
        if not content and not media_paths:
            return

        stream_id = _gen_req_id("stream")
        self._active_streams[chat_id] = (frame, stream_id, time.monotonic(), response_url)

        await self._handle_message(
            sender_id=sender_id,
            chat_id=chat_id,
            content=content,
            media=media_paths,
            metadata={
                "platform": "wecom",
                "bot_name": self.bot_name,
                "default_agent": self.default_agent,
                "bot_id": self.bot_id,
                "chat_id": chat_id,
                "chat_type": chat_type,
                "msgid": str(body.get("msgid") or ""),
                "response_url": response_url,
            },
        )

    async def _download_wecom_media(
        self,
        media_obj: dict[str, Any],
        index: int,
        default_ext: str,
    ) -> str | None:
        url = str(media_obj.get("url") or "").strip()
        if not url:
            return None
        aes_key = str(media_obj.get("aeskey") or media_obj.get("aes_key") or "").strip()
        try:
            if self._ws_client is not None and aes_key and hasattr(self._ws_client, "download_file"):
                res = await self._ws_client.download_file(url, aes_key)
                raw = res.get("buffer") or b""
                filename = str(res.get("filename") or media_obj.get("file_name") or f"wecom_{int(time.time())}_{index}{default_ext}")
            else:
                if self._http_client is None or self._http_client.is_closed:
                    self._http_client = httpx.AsyncClient(timeout=20.0, follow_redirects=True)
                resp = await self._http_client.get(url)
                if resp.status_code != 200:
                    return None
                raw = resp.content
                filename = str(media_obj.get("file_name") or f"wecom_{int(time.time())}_{index}{default_ext}")
            if not raw:
                return None
            target = self._media_dir / filename
            target.write_bytes(raw)
            return str(target)
        except Exception:
            logger.debug("WeCom media download failed", exc_info=True)
            return None

    async def send(self, msg: OutboundMessage) -> None:
        """发送企业微信回复（优先使用 WebSocket 流式气泡回复，其次 response_url 或主动消息）。"""
        chat_id = str(msg.chat_id or msg.metadata.get("chat_id") or "").strip()
        text = (msg.content or "").strip()
        if not text:
            return
        is_progress = bool(msg.metadata.get("_progress"))
        active = self._active_streams.get(chat_id)

        # 1. 尝试通过 WebSocket reply_stream 回复（支持流式思考进度与最终回复）
        if active is not None:
            frame, stream_id, created_at, cached_resp_url = active
            if time.monotonic() - created_at < 580.0:
                finish = not is_progress
                if finish:
                    self._active_streams.pop(chat_id, None)
                try:
                    if self._ws_client is not None and hasattr(self._ws_client, "reply_stream"):
                        await self._ws_client.reply_stream(
                            frame=frame,
                            stream_id=stream_id,
                            content=text,
                            finish=finish,
                        )
                        return
                    if self._ws_conn is not None:
                        req_id = str((frame.get("headers") or {}).get("req_id") or "")
                        if req_id:
                            await self._ws_conn.send(
                                json.dumps(
                                    {
                                        "cmd": _CMD_RESPONSE,
                                        "headers": {"req_id": req_id},
                                        "body": {
                                            "msgtype": "stream",
                                            "stream": {
                                                "id": stream_id,
                                                "finish": finish,
                                                "content": text,
                                            },
                                        },
                                    },
                                    ensure_ascii=False,
                                )
                            )
                            return
                except Exception:
                    logger.warning("WeCom reply_stream failed, falling back to response_url/send_message", exc_info=True)

        if is_progress:
            # 没有可用流式帧时忽略中间进度消息
            return

        # 2. 尝试通过 response_url Webhook 回复
        response_url = str(msg.metadata.get("response_url") or (active[3] if active else "") or "").strip()
        if response_url:
            try:
                if self._http_client is None or self._http_client.is_closed:
                    self._http_client = httpx.AsyncClient(timeout=15.0)
                resp = await self._http_client.post(
                    response_url,
                    json={"msgtype": "text", "text": {"content": text}},
                )
                if resp.status_code == 200:
                    return
            except Exception:
                logger.warning("WeCom response_url post failed", exc_info=True)

        # 3. 尝试主动推送消息
        if self._ws_client is not None and chat_id and hasattr(self._ws_client, "send_message"):
            await self._ws_client.send_message(
                chat_id,
                {"msgtype": "markdown", "markdown": {"content": text}},
            )
        elif self._ws_conn is not None and chat_id:
            await self._ws_conn.send(
                json.dumps(
                    {
                        "cmd": _CMD_SEND_MSG,
                        "headers": {"req_id": _gen_req_id(_CMD_SEND_MSG)},
                        "body": {
                            "chatid": chat_id,
                            "msgtype": "markdown",
                            "markdown": {"content": text},
                        },
                    },
                    ensure_ascii=False,
                )
            )
