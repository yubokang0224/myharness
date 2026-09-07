import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import openharness.channels.impl.dingtalk as dingtalk_module
from openharness.channels.bus.queue import MessageBus
from openharness.channels.impl.dingtalk import (
    DingTalkBoardImageEnqueueError,
    DingTalkChannel,
    NanobotDingTalkHandler,
)
from openharness.config.schema import DingTalkBotConfig, DingTalkConfig


class _FakeChatbotMessage:
    message_type = "picture"
    sender_staff_id = "staff-7"
    sender_id = "union-7"
    sender_nick = "项目经理"
    message_id = "msg/raw-7"
    conversation_id = "conv-7"

    @classmethod
    def from_dict(cls, _payload):
        return cls()

    def get_text_list(self):
        return []


class _FakeResponse:
    def __init__(self, payload, *, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


def _channel(*, defer_board_images: bool) -> tuple[DingTalkChannel, MessageBus]:
    bus = MessageBus()
    config = DingTalkBotConfig(
        name="project",
        enabled=True,
        allow_from=["*"],
        client_id="ding-app",
        client_secret="secret",
        robot_code="ding-app",
        default_agent="项目助手",
        defer_board_images=defer_board_images,
        board_business_domain="project",
        board_batch_quiet_seconds=20,
    )
    return (
        DingTalkChannel(
            config,
            bus,
            channel_name="dingtalk:project",
            bot_name="project",
            default_agent="项目助手",
        ),
        bus,
    )


def test_dingtalk_board_image_config_defaults_are_opt_in():
    for config in (DingTalkBotConfig(), DingTalkConfig()):
        assert config.defer_board_images is False
        assert config.board_business_domain == ""
        assert config.board_batch_quiet_seconds == 20


@pytest.mark.asyncio
async def test_dingtalk_rejects_error_body_disguised_as_image(tmp_path, monkeypatch):
    channel, _bus = _channel(defer_board_images=False)

    class FakeDownloadResponse:
        status_code = 200
        content = b'{"error":"download expired"}'
        headers = {"content-type": "image/png"}
        has_redirect_location = False
        url = "https://download.example/board.png"

    class FakeHttp:
        async def get(self, *_args, **_kwargs):
            return FakeDownloadResponse()

    chatbot_message = SimpleNamespace(
        message_type="picture",
        message_id="msg-1",
        get_image_list=lambda: ["download-code"],
    )
    channel._http = FakeHttp()
    channel._get_access_token = AsyncMock(return_value="token")
    channel._get_download_url = AsyncMock(
        return_value="https://download.example/board.png"
    )
    monkeypatch.setattr(dingtalk_module, "resolve_channel_media_dir", lambda _name: tmp_path)
    monkeypatch.setattr(dingtalk_module, "ensure_public_http_url", AsyncMock())

    paths, notes = await channel._download_inbound_media(
        chatbot_message,
        {"msgtype": "picture"},
    )

    assert paths == []
    assert notes == ["[picture: download failed]"]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_enqueue_board_images_uploads_multiple_images_in_source_order(tmp_path, monkeypatch):
    first = tmp_path / "first.jpg"
    first_bytes = b"\xff\xd8\xfffirst-board"
    first.write_bytes(first_bytes)
    second = tmp_path / "second.png"
    second_bytes = b"\x89PNG\r\n\x1a\nsecond-board"
    second.write_bytes(second_bytes)

    channel, _bus = _channel(defer_board_images=True)
    monkeypatch.setattr(
        dingtalk_module,
        "load_settings",
        lambda: SimpleNamespace(
            internal_api=SimpleNamespace(
                base_url="http://internal.example/api/v1",
                dingtalk_token="service-token",
            )
        ),
    )
    created_clients = []
    requests = []

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            created_clients.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, **kwargs):
            requests.append((url, kwargs))
            if "files" in kwargs:
                upload_index = sum(1 for _url, call in requests if "files" in call) - 1
                return _FakeResponse(
                    {
                        "isSuccess": True,
                        "backResult": f"/WebUp_Files/images/upload-{upload_index}.jpg",
                    }
                )
            return _FakeResponse(
                {
                    "isSuccess": True,
                    "backResult": {"batchId": "batch-42", "totalImageCount": 5},
                }
            )

    monkeypatch.setattr(dingtalk_module.httpx, "AsyncClient", FakeAsyncClient)

    result = await channel._enqueue_board_images(
        media_paths=[str(first), str(second)],
        sender_id="staff-7",
        sender_name="项目经理",
        conversation_id="conv-7",
        source_message_id="msg/raw-7",
        message_text="请记录白板",
        raw_data={"msgId": "msg/raw-7", "content": {"downloadCode": "redacted"}},
    )

    assert created_clients == [{"timeout": 60.0, "trust_env": False}]
    assert [request[0] for request in requests] == [
        "http://internal.example/api/v1/Common/UploadImage",
        "http://internal.example/api/v1/Common/UploadImage",
        "http://internal.example/api/v1/BoardImageRecognition/Enqueue",
    ]
    assert requests[0][1]["files"]["file"] == ("first.jpg", first_bytes, "image/jpeg")
    assert requests[1][1]["files"]["file"] == ("second.png", second_bytes, "image/png")

    enqueue_payload = requests[2][1]["json"]
    assert enqueue_payload == {
        "businessDomain": "project",
        "sourceChannel": "dingtalk:project",
        "botName": "project",
        "agentName": "项目助手",
        "sourceChatId": "staff-7",
        "sourceSenderId": "staff-7",
        "sourceSenderName": "项目经理",
        "conversationId": "conv-7",
        "sourceMessageId": "msg/raw-7",
        "messageText": "请记录白板",
        "quietSeconds": 20,
        "images": [
            {
                "fileUrl": "/WebUp_Files/images/upload-0.jpg",
                "fileName": "first.jpg",
                "contentType": "image/jpeg",
                "sizeBytes": len(first_bytes),
                "sha256": hashlib.sha256(first_bytes).hexdigest(),
                "sourceIndex": 0,
            },
            {
                "fileUrl": "/WebUp_Files/images/upload-1.jpg",
                "fileName": "second.png",
                "contentType": "image/png",
                "sizeBytes": len(second_bytes),
                "sha256": hashlib.sha256(second_bytes).hexdigest(),
                "sourceIndex": 1,
            },
        ],
    }
    assert requests[2][1]["headers"] == {
        "Authorization": "Bearer service-token",
        "Content-Type": "application/json",
    }
    assert result == {
        "batch_id": "batch-42",
        "total_image_count": 5,
        "source_message_id": "msg/raw-7",
    }


@pytest.mark.asyncio
async def test_enqueue_board_images_rejects_more_than_twenty_before_upload():
    channel, _bus = _channel(defer_board_images=True)

    with pytest.raises(DingTalkBoardImageEnqueueError, match="exceeds 20 images"):
        await channel._enqueue_board_images(
            media_paths=[f"missing-{index}.jpg" for index in range(21)],
            sender_id="staff-7",
            sender_name="项目经理",
            conversation_id="conv-7",
            source_message_id="msg-7",
            message_text="",
            raw_data={},
        )


@pytest.mark.asyncio
async def test_enqueue_board_images_treats_any_upload_failure_as_batch_failure(tmp_path, monkeypatch):
    first = tmp_path / "first.jpg"
    first.write_bytes(b"\xff\xd8\xfffirst-board")
    second = tmp_path / "second.jpg"
    second.write_bytes(b"\xff\xd8\xffsecond-board")
    channel, _bus = _channel(defer_board_images=True)
    monkeypatch.setattr(
        dingtalk_module,
        "load_settings",
        lambda: SimpleNamespace(
            internal_api=SimpleNamespace(
                base_url="http://internal.example/api/v1",
                dingtalk_token="service-token",
            )
        ),
    )
    requests = []

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, **kwargs):
            requests.append((url, kwargs))
            if len(requests) == 1:
                return _FakeResponse(
                    {"isSuccess": True, "backResult": "/WebUp_Files/images/upload-0.jpg"}
                )
            return _FakeResponse({"isSuccess": False, "message": "upload rejected"})

    monkeypatch.setattr(dingtalk_module.httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(DingTalkBoardImageEnqueueError, match="upload board image index=1"):
        await channel._enqueue_board_images(
            media_paths=[str(first), str(second)],
            sender_id="staff-7",
            sender_name="项目经理",
            conversation_id="conv-7",
            source_message_id="msg/raw-7",
            message_text="请记录白板",
            raw_data={"msgId": "msg/raw-7"},
        )

    assert len(requests) == 2
    assert all("Common/UploadImage" in url for url, _kwargs in requests)


def test_missing_dingtalk_message_id_uses_stable_content_fallback():
    channel, _bus = _channel(defer_board_images=True)
    arguments = {
        "raw_data": {"content": {"downloadCode": "download-1"}},
        "sender_id": "staff-7",
        "conversation_id": "conv-7",
        "image_hashes": ["abc", "def"],
    }

    first = channel._fallback_message_id(**arguments)
    second = channel._fallback_message_id(**arguments)

    assert first == second
    assert first.startswith("fallback-")


@pytest.mark.asyncio
async def test_deferred_board_images_reply_with_independent_recognition_semantics(monkeypatch):
    channel, bus = _channel(defer_board_images=True)
    monkeypatch.setattr(dingtalk_module, "ChatbotMessage", _FakeChatbotMessage)
    channel._download_inbound_media = AsyncMock(
        return_value=(["/tmp/board-1.jpg", "/tmp/board-2.jpg"], [])
    )
    channel._enqueue_board_images = AsyncMock(
        return_value={
            "batch_id": "batch-42",
            "total_image_count": 2,
            "source_message_id": "msg/raw-7",
        }
    )
    channel._on_message = AsyncMock()
    handler = NanobotDingTalkHandler(channel)

    status, _message = await handler.process(SimpleNamespace(data={"msgtype": "picture"}))

    assert status == dingtalk_module.AckMessage.STATUS_OK
    channel._on_message.assert_not_called()
    assert bus.inbound.empty()
    outbound = bus.outbound.get_nowait()
    assert outbound.channel == "dingtalk:project"
    assert outbound.chat_id == "staff-7"
    assert outbound.content == (
        "已保存 2 张白板照片，系统将逐张识别并分别生成白板草稿。"
    )


@pytest.mark.asyncio
async def test_deferred_board_image_failure_is_retryable_and_never_reaches_llm(monkeypatch):
    channel, bus = _channel(defer_board_images=True)
    monkeypatch.setattr(dingtalk_module, "ChatbotMessage", _FakeChatbotMessage)
    channel._download_inbound_media = AsyncMock(return_value=(["/tmp/board-1.jpg"], []))
    channel._enqueue_board_images = AsyncMock(
        side_effect=DingTalkBoardImageEnqueueError("upstream unavailable")
    )
    channel._on_message = AsyncMock()
    handler = NanobotDingTalkHandler(channel)

    status, _message = await handler.process(SimpleNamespace(data={"msgtype": "picture"}))

    assert status != dingtalk_module.AckMessage.STATUS_OK
    assert status == getattr(dingtalk_module.AckMessage, "STATUS_SYSTEM_EXCEPTION", 500)
    channel._on_message.assert_not_called()
    assert bus.inbound.empty()
    assert bus.outbound.empty()


@pytest.mark.asyncio
async def test_deferred_board_image_total_download_failure_is_retryable(monkeypatch):
    channel, bus = _channel(defer_board_images=True)
    monkeypatch.setattr(dingtalk_module, "ChatbotMessage", _FakeChatbotMessage)
    channel._download_inbound_media = AsyncMock(
        return_value=([], ["[picture: download failed]"])
    )
    channel._enqueue_board_images = AsyncMock()
    channel._on_message = AsyncMock()
    handler = NanobotDingTalkHandler(channel)

    status, _message = await handler.process(SimpleNamespace(data={"msgtype": "picture"}))

    assert status != dingtalk_module.AckMessage.STATUS_OK
    channel._enqueue_board_images.assert_not_called()
    channel._on_message.assert_not_called()
    assert bus.inbound.empty()
    assert bus.outbound.empty()


@pytest.mark.asyncio
async def test_unauthorized_sender_is_rejected_before_media_download(monkeypatch):
    channel, bus = _channel(defer_board_images=True)
    channel.config.allow_from = ["different-user"]
    monkeypatch.setattr(dingtalk_module, "ChatbotMessage", _FakeChatbotMessage)
    channel._download_inbound_media = AsyncMock()
    channel._enqueue_board_images = AsyncMock()
    channel._on_message = AsyncMock()
    handler = NanobotDingTalkHandler(channel)

    status, _message = await handler.process(SimpleNamespace(data={"msgtype": "picture"}))

    assert status == dingtalk_module.AckMessage.STATUS_OK
    channel._download_inbound_media.assert_not_called()
    channel._enqueue_board_images.assert_not_called()
    channel._on_message.assert_not_called()
    assert bus.inbound.empty()
    assert bus.outbound.empty()


@pytest.mark.asyncio
async def test_disabled_deferred_board_images_preserve_normal_channel_flow(monkeypatch):
    channel, _bus = _channel(defer_board_images=False)
    monkeypatch.setattr(dingtalk_module, "ChatbotMessage", _FakeChatbotMessage)
    channel._download_inbound_media = AsyncMock(return_value=(["/tmp/board-1.jpg"], []))
    channel._enqueue_board_images = AsyncMock(
        side_effect=AssertionError("deferred enqueue must remain opt-in")
    )
    channel._on_message = AsyncMock()
    handler = NanobotDingTalkHandler(channel)

    status, _message = await handler.process(SimpleNamespace(data={"msgtype": "picture"}))
    await asyncio.sleep(0)

    assert status == dingtalk_module.AckMessage.STATUS_OK
    channel._enqueue_board_images.assert_not_called()
    channel._on_message.assert_awaited_once()
    assert channel._on_message.await_args.kwargs["media"] == ["/tmp/board-1.jpg"]
