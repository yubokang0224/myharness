"""DingTalk/DingDing channel implementation using Stream Mode."""

import asyncio
import json
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
import logging

from openharness.channels.bus.events import OutboundMessage
from openharness.channels.bus.queue import MessageBus
from openharness.channels.impl.base import BaseChannel, resolve_channel_media_dir
from openharness.config.schema import DingTalkBotConfig, DingTalkConfig

logger = logging.getLogger(__name__)

MAX_INBOUND_MEDIA_BYTES = 50 * 1024 * 1024

try:
    from dingtalk_stream import (
        AckMessage,
        CallbackHandler,
        CallbackMessage,
        Credential,
        DingTalkStreamClient,
    )
    from dingtalk_stream.chatbot import ChatbotMessage

    DINGTALK_AVAILABLE = True
except ImportError:
    DINGTALK_AVAILABLE = False
    # Fallback so class definitions don't crash at module level
    CallbackHandler = object  # type: ignore[assignment,misc]
    CallbackMessage = None  # type: ignore[assignment,misc]
    AckMessage = None  # type: ignore[assignment,misc]
    ChatbotMessage = None  # type: ignore[assignment,misc]


class NanobotDingTalkHandler(CallbackHandler):
    """
    Standard DingTalk Stream SDK Callback Handler.
    Parses incoming messages and forwards them to the Nanobot channel.
    """

    def __init__(self, channel: "DingTalkChannel"):
        super().__init__()
        self.channel = channel

    async def process(self, message: CallbackMessage):
        """Process incoming stream message."""
        try:
            raw_data = self.channel._normalized_payload(message.data)

            # Parse using SDK's ChatbotMessage for robust handling
            chatbot_msg = ChatbotMessage.from_dict(raw_data)

            # Extract text content; fall back to raw dict if SDK object is empty
            content_parts = []
            try:
                content_parts.extend(part.strip() for part in (chatbot_msg.get_text_list() or []) if part and part.strip())
            except Exception:
                pass
            if not content_parts:
                text_payload = raw_data.get("text", {})
                if isinstance(text_payload, dict):
                    raw_text = str(text_payload.get("content", "")).strip()
                elif isinstance(text_payload, str):
                    raw_text = text_payload.strip()
                else:
                    raw_text = ""
                if raw_text:
                    content_parts.append(raw_text)

            media_paths, media_notes = await self.channel._download_inbound_media(
                chatbot_msg,
                raw_data,
            )
            # Keep downloaded attachment paths out of the user-visible prompt.
            # The runtime receives them through ``media`` and can attach image
            # blocks directly without polluting the conversation text.
            content_parts.extend(note for note in media_notes if "download failed" in note or "too large" in note)

            if not content_parts and not media_paths:
                logger.warning(
                    "Received empty or unsupported DingTalk message type=%s overview=%s",
                    chatbot_msg.message_type,
                    self.channel._payload_overview(raw_data),
                )
                return AckMessage.STATUS_OK, "OK"

            sender_id = (
                chatbot_msg.sender_staff_id
                or chatbot_msg.sender_id
                or raw_data.get("senderStaffId")
                or raw_data.get("senderId")
            )
            sender_name = chatbot_msg.sender_nick or raw_data.get("senderNick") or "Unknown"
            if not sender_id:
                logger.warning(
                    "DingTalk message missing sender id type=%s overview=%s",
                    chatbot_msg.message_type,
                    self.channel._payload_overview(raw_data),
                )
                return AckMessage.STATUS_OK, "OK"

            content = "\n".join(content_parts) if content_parts else "[attachment message]"

            logger.info(
                "Received DingTalk message from %s (%s): %s media=%s",
                sender_name,
                sender_id,
                content,
                len(media_paths),
            )

            # Forward to Nanobot via _on_message (non-blocking).
            # Store reference to prevent GC before task completes.
            task = asyncio.create_task(
                self.channel._on_message(
                    content,
                    sender_id,
                    sender_name,
                    media=media_paths,
                    metadata_extra={
                        "msg_type": chatbot_msg.message_type,
                        "message_id": chatbot_msg.message_id,
                        "conversation_id": chatbot_msg.conversation_id,
                    },
                )
            )
            self.channel._background_tasks.add(task)
            task.add_done_callback(self.channel._background_tasks.discard)

            return AckMessage.STATUS_OK, "OK"

        except Exception as e:
            logger.error("Error processing DingTalk message: %s", e)
            # Return OK to avoid retry loop from DingTalk server
            return AckMessage.STATUS_OK, "Error"


class DingTalkChannel(BaseChannel):
    """
    DingTalk channel using Stream Mode.

    Uses WebSocket to receive events via `dingtalk-stream` SDK.
    Uses direct HTTP API to send messages (SDK is mainly for receiving).

    Note: Currently only supports private (1:1) chat. Group messages are
    received but replies are sent back as private messages to the sender.
    """

    name = "dingtalk"
    _IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
    _AUDIO_EXTS = {".amr", ".mp3", ".wav", ".ogg", ".m4a", ".aac"}
    _VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}

    def __init__(
        self,
        config: DingTalkConfig | DingTalkBotConfig,
        bus: MessageBus,
        *,
        channel_name: str | None = None,
        bot_name: str = "default",
        default_agent: str | None = None,
    ):
        super().__init__(config, bus)
        self.config: DingTalkConfig | DingTalkBotConfig = config
        if channel_name:
            self.name = channel_name
        self.bot_name = bot_name
        self.default_agent = default_agent
        self._client: Any = None
        self._http: httpx.AsyncClient | None = None

        # Access Token management for sending messages
        self._access_token: str | None = None
        self._token_expiry: float = 0

        # Hold references to background tasks to prevent GC
        self._background_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        """Start the DingTalk bot with Stream Mode."""
        try:
            if not DINGTALK_AVAILABLE:
                logger.error(
                    "DingTalk Stream SDK not installed. Run: pip install dingtalk-stream"
                )
                return

            if not self.config.client_id or not self.config.client_secret:
                logger.error("DingTalk client_id and client_secret not configured")
                return

            self._running = True
            self._http = httpx.AsyncClient()

            logger.info(
                "Initializing DingTalk Stream Client with Client ID: %s...",
                self.config.client_id,
            )
            credential = Credential(self.config.client_id, self.config.client_secret)
            self._client = DingTalkStreamClient(credential)

            # Register standard handler
            handler = NanobotDingTalkHandler(self)
            self._client.register_callback_handler(ChatbotMessage.TOPIC, handler)

            logger.info("DingTalk bot started with Stream Mode")

            # Reconnect loop: restart stream if SDK exits or crashes
            while self._running:
                try:
                    await self._client.start()
                except Exception as e:
                    logger.warning("DingTalk stream error: %s", e)
                if self._running:
                    logger.info("Reconnecting DingTalk stream in 5 seconds...")
                    await asyncio.sleep(5)

        except Exception as e:
            logger.exception("Failed to start DingTalk channel: %s", e)

    async def stop(self) -> None:
        """Stop the DingTalk bot."""
        self._running = False
        # Close the shared HTTP client
        if self._http:
            await self._http.aclose()
            self._http = None
        # Cancel outstanding background tasks
        for task in self._background_tasks:
            task.cancel()
        self._background_tasks.clear()

    async def _get_access_token(self) -> str | None:
        """Get or refresh Access Token."""
        if self._access_token and time.time() < self._token_expiry:
            return self._access_token

        url = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
        data = {
            "appKey": self.config.client_id,
            "appSecret": self.config.client_secret,
        }

        if not self._http:
            logger.warning("DingTalk HTTP client not initialized, cannot refresh token")
            return None

        try:
            resp = await self._http.post(url, json=data)
            resp.raise_for_status()
            res_data = resp.json()
            self._access_token = res_data.get("accessToken")
            # Expire 60s early to be safe
            self._token_expiry = time.time() + int(res_data.get("expireIn", 7200)) - 60
            return self._access_token
        except Exception as e:
            logger.error("Failed to get DingTalk access token: %s", e)
            return None

    @staticmethod
    def _is_http_url(value: str) -> bool:
        return urlparse(value).scheme in ("http", "https")

    def _guess_upload_type(self, media_ref: str) -> str:
        ext = Path(urlparse(media_ref).path).suffix.lower()
        if ext in self._IMAGE_EXTS:
            return "image"
        if ext in self._AUDIO_EXTS:
            return "voice"
        if ext in self._VIDEO_EXTS:
            return "video"
        return "file"

    def _guess_filename(self, media_ref: str, upload_type: str) -> str:
        name = os.path.basename(urlparse(media_ref).path)
        return name or {"image": "image.jpg", "voice": "audio.amr", "video": "video.mp4"}.get(upload_type, "file.bin")

    @staticmethod
    def _safe_filename(filename: str | None) -> str:
        name = Path(filename or "attachment.bin").name.strip() or "attachment.bin"
        return re.sub(r"[^A-Za-z0-9._ -]+", "_", name)

    @staticmethod
    def _filename_from_content_disposition(value: str | None) -> str | None:
        if not value:
            return None
        match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', value, flags=re.IGNORECASE)
        if not match:
            return None
        return unquote(match.group(1).strip())

    @staticmethod
    def _parse_json_object(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        value = value.strip()
        if not value:
            return value
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value
        return parsed

    @classmethod
    def _normalized_payload(cls, data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(data, dict):
            return {}
        normalized = dict(data)
        for key in ("content", "text", "atUsers", "hostingContext", "conversationMsgContext"):
            if key in normalized:
                normalized[key] = cls._parse_json_object(normalized[key])
        if "msgtype" not in normalized:
            msg_type = normalized.get("msgType") or normalized.get("messageType")
            if msg_type:
                normalized["msgtype"] = msg_type
        return normalized

    @classmethod
    def _iter_download_code_items(cls, data: Any) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []

        def walk(value: Any, inherited_name: str | None = None) -> None:
            value = cls._parse_json_object(value)
            if isinstance(value, dict):
                filename = (
                    value.get("fileName")
                    or value.get("filename")
                    or value.get("name")
                    or inherited_name
                )
                download_code = value.get("downloadCode") or value.get("download_code")
                if download_code:
                    items.append(
                        {
                            "download_code": str(download_code),
                            "filename": str(filename or ""),
                        }
                    )
                for child in value.values():
                    walk(child, str(filename) if filename else inherited_name)
            elif isinstance(value, list):
                for child in value:
                    walk(child, inherited_name)

        walk(data)
        deduped: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in items:
            code = item["download_code"]
            if code in seen:
                continue
            seen.add(code)
            deduped.append(item)
        return deduped

    @classmethod
    def _iter_media_url_items(cls, data: Any) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        url_keys = {
            "downloadUrl",
            "downloadURL",
            "url",
            "picUrl",
            "pictureUrl",
            "imageUrl",
            "imageURL",
            "mediaUrl",
            "mediaURL",
        }

        def walk(value: Any, inherited_name: str | None = None) -> None:
            value = cls._parse_json_object(value)
            if isinstance(value, dict):
                filename = (
                    value.get("fileName")
                    or value.get("filename")
                    or value.get("name")
                    or inherited_name
                )
                for key, child in value.items():
                    if key in url_keys and isinstance(child, str) and cls._is_http_url(child):
                        items.append({"url": child, "filename": str(filename or "")})
                    walk(child, str(filename) if filename else inherited_name)
            elif isinstance(value, list):
                for child in value:
                    walk(child, inherited_name)

        walk(data)
        deduped: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in items:
            url = item["url"]
            if url in seen:
                continue
            seen.add(url)
            deduped.append(item)
        return deduped

    @classmethod
    def _payload_overview(cls, data: dict[str, Any]) -> str:
        def compact(value: Any, depth: int = 0) -> Any:
            value = cls._parse_json_object(value)
            if depth >= 3:
                return "<nested>"
            if isinstance(value, dict):
                result: dict[str, Any] = {}
                for key, child in list(value.items())[:30]:
                    if key in {"downloadCode", "download_code"}:
                        text = str(child)
                        result[key] = f"{text[:6]}...{len(text)}"
                    elif "token" in key.lower() or "secret" in key.lower():
                        result[key] = "<redacted>"
                    else:
                        result[key] = compact(child, depth + 1)
                return result
            if isinstance(value, list):
                return [compact(child, depth + 1) for child in value[:8]]
            if isinstance(value, str):
                return value if len(value) <= 160 else f"{value[:160]}...{len(value)}"
            return value

        try:
            return json.dumps(compact(data), ensure_ascii=False)
        except Exception:
            return str(list(data.keys()))

    async def _get_download_url(self, token: str, download_code: str) -> str | None:
        if not self._http:
            return None
        url = "https://api.dingtalk.com/v1.0/robot/messageFiles/download"
        headers = {
            "Content-Type": "application/json",
            "Accept": "*/*",
            "x-acs-dingtalk-access-token": token,
        }
        robot_codes = []
        for candidate in (self.config.client_id, self.config.robot_code):
            if candidate and candidate not in robot_codes:
                robot_codes.append(candidate)
        try:
            last_error = ""
            for robot_code in robot_codes:
                payload = {
                    "robotCode": robot_code,
                    "downloadCode": download_code,
                }
                resp = await self._http.post(url, json=payload, headers=headers)
                text = resp.text
                if resp.status_code >= 400:
                    last_error = f"status={resp.status_code} body={text[:500]}"
                    logger.warning(
                        "DingTalk download url failed robotCode=%s status=%s body=%s",
                        robot_code,
                        resp.status_code,
                        text[:500],
                    )
                    continue
                result = resp.json()
                download_url = result.get("downloadUrl") or (result.get("result") or {}).get("downloadUrl")
                if download_url:
                    return str(download_url)
                last_error = f"missing downloadUrl body={text[:500]}"
                logger.warning("DingTalk download url missing robotCode=%s body=%s", robot_code, text[:500])
            logger.error("DingTalk download url failed for all robotCode candidates: %s", last_error)
            return None
        except Exception as e:
            logger.error("DingTalk download url error: %s", e)
            return None

    async def _download_inbound_media(
        self,
        chatbot_msg: Any,
        raw_data: dict[str, Any],
    ) -> tuple[list[str], list[str]]:
        media_paths: list[str] = []
        notes: list[str] = []

        raw_content = self._parse_json_object(raw_data.get("content") or {})
        download_items = self._iter_download_code_items(raw_content)
        download_items.extend(self._iter_download_code_items(raw_data))
        try:
            for code in chatbot_msg.get_image_list() or []:
                download_items.append({"download_code": str(code), "filename": "image.png"})
        except Exception:
            pass

        deduped_items: list[dict[str, str]] = []
        seen_codes: set[str] = set()
        for item in download_items:
            code = item.get("download_code")
            if not code or code in seen_codes:
                continue
            seen_codes.add(code)
            deduped_items.append(item)

        direct_url_items = self._iter_media_url_items(raw_content)
        direct_url_items.extend(self._iter_media_url_items(raw_data))
        deduped_urls: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        for item in direct_url_items:
            media_url = item.get("url")
            if not media_url or media_url in seen_urls:
                continue
            seen_urls.add(media_url)
            deduped_urls.append(item)

        if not deduped_items and not deduped_urls:
            return media_paths, notes

        token = await self._get_access_token() if deduped_items else None
        if deduped_items and not token:
            return media_paths, [f"[{chatbot_msg.message_type or 'attachment'}: download failed]"]

        media_dir = resolve_channel_media_dir(self.name)
        media_root = media_dir.resolve()
        msg_type = str(
            getattr(chatbot_msg, "message_type", None)
            or raw_data.get("msgtype")
            or raw_data.get("msgType")
            or "attachment"
        )
        message_id = str(getattr(chatbot_msg, "message_id", None) or raw_data.get("msgId") or int(time.time()))

        download_targets: list[dict[str, str]] = []
        for item in deduped_items:
            if not token:
                continue
            download_url = await self._get_download_url(token, item["download_code"])
            if not download_url:
                notes.append(f"[{msg_type}: download failed]")
                continue
            download_targets.append(
                {
                    "url": download_url,
                    "filename": item.get("filename") or "image.png",
                    "source": f"code:{item['download_code'][:8]}",
                }
            )
        for item in deduped_urls:
            download_targets.append(
                {
                    "url": item["url"],
                    "filename": item.get("filename") or "",
                    "source": "url",
                }
            )

        if not self._http:
            return media_paths, [f"[{msg_type}: download failed]"]

        for index, item in enumerate(download_targets):
            download_url = item["url"]
            try:
                resp = await self._http.get(download_url, follow_redirects=True)
                if resp.status_code >= 400:
                    logger.error(
                        "DingTalk media download failed status=%s source=%s",
                        resp.status_code,
                        item.get("source"),
                    )
                    notes.append(f"[{msg_type}: download failed]")
                    continue
                if len(resp.content) > MAX_INBOUND_MEDIA_BYTES:
                    notes.append(f"[{msg_type}: too large]")
                    continue

                content_type = (resp.headers.get("content-type") or "").split(";")[0].strip()
                filename = (
                    item.get("filename")
                    or self._filename_from_content_disposition(resp.headers.get("content-disposition"))
                    or f"{message_id}_{index}"
                )
                safe_name = self._safe_filename(filename)
                if "." not in safe_name:
                    ext = mimetypes.guess_extension(content_type) or (".png" if msg_type == "picture" else ".bin")
                    safe_name = f"{safe_name}{ext}"
                file_path = (media_root / f"{message_id}_{index}_{safe_name}").resolve()
                if not file_path.is_relative_to(media_root):
                    logger.warning("Rejected DingTalk download outside media directory: %r", filename)
                    notes.append(f"[{msg_type}: download failed]")
                    continue

                file_path.write_bytes(resp.content)
                media_paths.append(str(file_path))
                logger.debug("Downloaded DingTalk %s to %s", msg_type, file_path)
            except Exception as e:
                logger.error("DingTalk media download error source=%s err=%s", item.get("source"), e)
                notes.append(f"[{msg_type}: download failed]")

        return media_paths, notes

    async def _read_media_bytes(
        self,
        media_ref: str,
    ) -> tuple[bytes | None, str | None, str | None]:
        if not media_ref:
            return None, None, None

        if self._is_http_url(media_ref):
            if not self._http:
                return None, None, None
            try:
                resp = await self._http.get(media_ref, follow_redirects=True)
                if resp.status_code >= 400:
                    logger.warning(
                        "DingTalk media download failed status=%s ref=%s",
                        resp.status_code,
                        media_ref,
                    )
                    return None, None, None
                content_type = (resp.headers.get("content-type") or "").split(";")[0].strip()
                filename = self._guess_filename(media_ref, self._guess_upload_type(media_ref))
                return resp.content, filename, content_type or None
            except Exception as e:
                logger.error("DingTalk media download error ref=%s err=%s", media_ref, e)
                return None, None, None

        try:
            if media_ref.startswith("file://"):
                parsed = urlparse(media_ref)
                local_path = Path(unquote(parsed.path))
            else:
                local_path = Path(os.path.expanduser(media_ref))
            if not local_path.is_file():
                logger.warning("DingTalk media file not found: %s", local_path)
                return None, None, None
            data = await asyncio.to_thread(local_path.read_bytes)
            content_type = mimetypes.guess_type(local_path.name)[0]
            return data, local_path.name, content_type
        except Exception as e:
            logger.error("DingTalk media read error ref=%s err=%s", media_ref, e)
            return None, None, None

    async def _upload_media(
        self,
        token: str,
        data: bytes,
        media_type: str,
        filename: str,
        content_type: str | None,
    ) -> str | None:
        if not self._http:
            return None
        url = f"https://oapi.dingtalk.com/media/upload?access_token={token}&type={media_type}"
        mime = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        files = {"media": (filename, data, mime)}

        try:
            resp = await self._http.post(url, files=files)
            text = resp.text
            result = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            if resp.status_code >= 400:
                logger.error("DingTalk media upload failed status=%s type=%s body=%s", resp.status_code, media_type, text[:500])
                return None
            errcode = result.get("errcode", 0)
            if errcode != 0:
                logger.error("DingTalk media upload api error type=%s errcode=%s body=%s", media_type, errcode, text[:500])
                return None
            sub = result.get("result") or {}
            media_id = result.get("media_id") or result.get("mediaId") or sub.get("media_id") or sub.get("mediaId")
            if not media_id:
                logger.error("DingTalk media upload missing media_id body=%s", text[:500])
                return None
            return str(media_id)
        except Exception as e:
            logger.error("DingTalk media upload error type=%s err=%s", media_type, e)
            return None

    async def _send_batch_message(
        self,
        token: str,
        chat_id: str,
        msg_key: str,
        msg_param: dict[str, Any],
    ) -> bool:
        if not self._http:
            logger.warning("DingTalk HTTP client not initialized, cannot send")
            return False

        url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
        headers = {"x-acs-dingtalk-access-token": token}
        payload = {
            "robotCode": self.config.robot_code or self.config.client_id,
            "userIds": [chat_id],
            "msgKey": msg_key,
            "msgParam": json.dumps(msg_param, ensure_ascii=False),
        }

        try:
            resp = await self._http.post(url, json=payload, headers=headers)
            body = resp.text
            if resp.status_code != 200:
                logger.error("DingTalk send failed msgKey=%s status=%s body=%s", msg_key, resp.status_code, body[:500])
                return False
            try:
                result = resp.json()
            except Exception:
                result = {}
            errcode = result.get("errcode")
            if errcode not in (None, 0):
                logger.error("DingTalk send api error msgKey=%s errcode=%s body=%s", msg_key, errcode, body[:500])
                return False
            logger.debug("DingTalk message sent to %s with msgKey=%s", chat_id, msg_key)
            return True
        except Exception as e:
            logger.error("Error sending DingTalk message msgKey=%s err=%s", msg_key, e)
            return False

    async def _send_markdown_text(self, token: str, chat_id: str, content: str) -> bool:
        return await self._send_batch_message(
            token,
            chat_id,
            "sampleMarkdown",
            {"text": content, "title": "Nanobot Reply"},
        )

    async def _send_media_ref(self, token: str, chat_id: str, media_ref: str) -> bool:
        media_ref = (media_ref or "").strip()
        if not media_ref:
            return True

        upload_type = self._guess_upload_type(media_ref)
        if upload_type == "image" and self._is_http_url(media_ref):
            ok = await self._send_batch_message(
                token,
                chat_id,
                "sampleImageMsg",
                {"photoURL": media_ref},
            )
            if ok:
                return True
            logger.warning("DingTalk image url send failed, trying upload fallback: %s", media_ref)

        data, filename, content_type = await self._read_media_bytes(media_ref)
        if not data:
            logger.error("DingTalk media read failed: %s", media_ref)
            return False

        filename = filename or self._guess_filename(media_ref, upload_type)
        file_type = Path(filename).suffix.lower().lstrip(".")
        if not file_type:
            guessed = mimetypes.guess_extension(content_type or "")
            file_type = (guessed or ".bin").lstrip(".")
        if file_type == "jpeg":
            file_type = "jpg"

        media_id = await self._upload_media(
            token=token,
            data=data,
            media_type=upload_type,
            filename=filename,
            content_type=content_type,
        )
        if not media_id:
            return False

        if upload_type == "image":
            # Verified in production: sampleImageMsg accepts media_id in photoURL.
            ok = await self._send_batch_message(
                token,
                chat_id,
                "sampleImageMsg",
                {"photoURL": media_id},
            )
            if ok:
                return True
            logger.warning("DingTalk image media_id send failed, falling back to file: %s", media_ref)

        return await self._send_batch_message(
            token,
            chat_id,
            "sampleFile",
            {"mediaId": media_id, "fileName": filename, "fileType": file_type},
        )

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through DingTalk."""
        token = await self._get_access_token()
        if not token:
            return

        if msg.content and msg.content.strip():
            await self._send_markdown_text(token, msg.chat_id, msg.content.strip())

        for media_ref in msg.media or []:
            ok = await self._send_media_ref(token, msg.chat_id, media_ref)
            if ok:
                continue
            logger.error("DingTalk media send failed for %s", media_ref)
            # Send visible fallback so failures are observable by the user.
            filename = self._guess_filename(media_ref, self._guess_upload_type(media_ref))
            await self._send_markdown_text(
                token,
                msg.chat_id,
                f"[Attachment send failed: {filename}]",
            )

    async def _on_message(
        self,
        content: str,
        sender_id: str,
        sender_name: str,
        *,
        media: list[str] | None = None,
        metadata_extra: dict[str, Any] | None = None,
    ) -> None:
        """Handle incoming message (called by NanobotDingTalkHandler).

        Delegates to BaseChannel._handle_message() which enforces allow_from
        permission checks before publishing to the bus.
        """
        try:
            logger.info("DingTalk inbound: %s from %s", content, sender_name)
            metadata = {
                "bot_name": self.bot_name,
                "default_agent": self.default_agent,
                "sender_name": sender_name,
                "platform": "dingtalk",
            }
            if metadata_extra:
                metadata.update({k: v for k, v in metadata_extra.items() if v is not None})
            await self._handle_message(
                sender_id=sender_id,
                chat_id=sender_id,  # For private chat, chat_id == sender_id
                content=str(content),
                media=media or [],
                metadata=metadata,
            )
        except Exception as e:
            logger.error("Error publishing DingTalk message: %s", e)
