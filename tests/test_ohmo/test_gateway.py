import asyncio
import contextlib
import logging
from types import SimpleNamespace
from datetime import datetime
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from openharness.api.usage import UsageSnapshot
from openharness.bridge import get_bridge_manager
from openharness.channels.bus.events import InboundMessage
from openharness.channels.bus.queue import MessageBus
from openharness.commands import CommandResult
from openharness.commands.registry import SlashCommand, create_default_command_registry
from openharness.config.schema import Config, DingTalkBotConfig, DingTalkConfig
from openharness.config.settings import Settings
from openharness.coordinator.agent_definitions import AgentDefinition
from openharness.engine.messages import ConversationMessage, ImageBlock, TextBlock, ToolResultBlock, ToolUseBlock
from openharness.engine.stream_events import (
    AssistantTextDelta,
    AssistantTurnComplete,
    CompactProgressEvent,
    ErrorEvent,
    ToolExecutionStarted,
)
from openharness.memory import add_memory_entry as add_project_memory_entry
from openharness.memory import list_memory_files as list_project_memory_files
from openharness.channels.impl.manager import ChannelManager

from ohmo.gateway.bridge import OhmoGatewayBridge, _format_gateway_error
from ohmo.gateway.api import create_app
from ohmo.gateway.config import build_channel_manager_config, save_gateway_config
from ohmo.gateway.dependencies import AuthContext
from ohmo.gateway.models import GatewayConfig, GatewayState
from ohmo.gateway.routers.chat import (
    _default_persist_mode_for_session,
    _prepare_user_text,
    create_session,
    get_artifact_content,
    list_artifacts,
    list_sessions,
    send_message,
    send_message_sync,
)
from ohmo.gateway.routers.agents import list_agent_tools, update_agent
from ohmo.gateway.routers.skills import list_mcp_servers
from ohmo.gateway.schemas.agents import UpdateAgentRequest
from ohmo.gateway.schemas.chat import CreateSessionRequest, MessageRequest
from ohmo.gateway.runtime import OhmoSessionRuntimePool, _build_inbound_user_message, _format_channel_progress
from ohmo.gateway.service import OhmoGatewayService, gateway_status, stop_gateway_process
from ohmo.gateway.tool_policy import apply_agent_tool_policy
from ohmo.memory import add_memory_entry as add_ohmo_memory_entry
from ohmo.memory import list_memory_files as list_ohmo_memory_files
from ohmo.gateway.router import session_key_for_message
from ohmo.session_storage import load_latest_for_session_key, save_session_snapshot
from ohmo.workspace import get_gateway_restart_notice_path, initialize_workspace
from openharness.mcp.types import McpConnectionStatus, McpSseServerConfig, McpToolInfo


def test_gateway_router_uses_thread_and_sender_when_present():
    message = InboundMessage(
        channel="slack",
        sender_id="u1",
        chat_id="c1",
        content="hello",
        timestamp=datetime.utcnow(),
        metadata={"thread_ts": "t1"},
    )
    assert session_key_for_message(message) == "slack:c1:t1:u1"


def test_gateway_router_falls_back_to_chat_and_sender_scope():
    message = InboundMessage(
        channel="telegram",
        sender_id="u1",
        chat_id="chat-1",
        content="hello",
        timestamp=datetime.utcnow(),
    )
    assert session_key_for_message(message) == "telegram:chat-1:u1"


def test_gateway_router_separates_senders_in_same_chat_thread():
    first = InboundMessage(
        channel="slack",
        sender_id="alice",
        chat_id="shared-chat",
        content="hello",
        timestamp=datetime.utcnow(),
        metadata={"thread_ts": "thread-1"},
    )
    second = InboundMessage(
        channel="slack",
        sender_id="bob",
        chat_id="shared-chat",
        content="hello",
        timestamp=datetime.utcnow(),
        metadata={"thread_ts": "thread-1"},
    )
    assert session_key_for_message(first) == "slack:shared-chat:thread-1:alice"
    assert session_key_for_message(second) == "slack:shared-chat:thread-1:bob"


def test_gateway_router_scopes_dingtalk_by_bot_agent_and_sender():
    message = InboundMessage(
        channel="dingtalk:ops-bot",
        sender_id="u1",
        chat_id="chat-1",
        content="hello",
        timestamp=datetime.utcnow(),
        metadata={"bot_name": "ops-bot", "default_agent": "ops-agent"},
    )
    assert session_key_for_message(message) == "dingtalk:ops-bot:ops-agent:chat-1:u1"


@pytest.mark.asyncio
async def test_chat_session_list_hides_remote_channel_sessions_by_default(tmp_path):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)
    save_session_snapshot(
        cwd=tmp_path,
        workspace=workspace,
        model="gpt-5.4",
        system_prompt="system",
        messages=[ConversationMessage.from_user_text("local")],
        usage=UsageSnapshot(),
        session_id="local-session",
    )
    save_session_snapshot(
        cwd=tmp_path,
        workspace=workspace,
        model="gpt-5.4",
        system_prompt="system",
        messages=[ConversationMessage.from_user_text("remote")],
        usage=UsageSnapshot(),
        session_id="remote-session",
        session_key="dingtalk:dingtalk-bot:general-purpose:u1:u1",
    )

    local_only = await list_sessions(
        _user={"sub": "u1"},
        runtime=SimpleNamespace(workspace=workspace),
        include_remote=False,
        channel=None,
        agent_name=None,
    )
    with_remote = await list_sessions(
        _user={"sub": "u1"},
        runtime=SimpleNamespace(workspace=workspace),
        include_remote=True,
        channel=None,
        agent_name=None,
    )

    assert [session.id for session in local_only] == ["local-session"]
    assert {session.id for session in with_remote} == {"local-session", "remote-session"}
    remote = next(session for session in with_remote if session.id == "remote-session")
    assert remote.conversation_id
    assert remote.channel == "dingtalk"
    assert remote.bot_name == "dingtalk-bot"
    assert remote.agent_name == "general-purpose"
    assert remote.chat_id == "u1"
    assert remote.sender_id == "u1"


@pytest.mark.asyncio
async def test_create_session_allows_anonymous_external_call(tmp_path):
    session = await create_session(
        body=CreateSessionRequest(title="External session", agent_name="api-agent"),
        _user=None,
        runtime=SimpleNamespace(workspace=tmp_path),
    )

    assert session.title == "External session"
    assert session.channel == "api"
    assert session.id

    sessions = await list_sessions(
        _user={},
        runtime=SimpleNamespace(workspace=tmp_path),
        include_remote=True,
        channel=None,
        agent_name=None,
    )
    assert sessions == []

    api_sessions = await list_sessions(
        _user={},
        runtime=SimpleNamespace(workspace=tmp_path),
        include_remote=True,
        channel="api",
        agent_name=None,
    )
    assert len(api_sessions) == 1
    assert api_sessions[0].agent_name == "api-agent"


@pytest.mark.asyncio
async def test_list_sessions_scans_past_filtered_api_sessions(tmp_path):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)
    save_session_snapshot(
        cwd=tmp_path,
        workspace=workspace,
        model="gpt-5.4",
        system_prompt="system",
        messages=[ConversationMessage.from_user_text("remote")],
        usage=UsageSnapshot(),
        session_id="remote-session",
        session_key="dingtalk:dingtalk-bot:general-purpose:u1:u1",
    )
    for index in range(25):
        await create_session(
            body=CreateSessionRequest(title=f"API session {index}", agent_name="api-agent"),
            _user=None,
            runtime=SimpleNamespace(workspace=workspace),
        )

    sessions = await list_sessions(
        _user={},
        runtime=SimpleNamespace(workspace=workspace),
        include_remote=True,
        channel=None,
        agent_name=None,
    )

    assert [session.id for session in sessions] == ["remote-session"]


@pytest.mark.asyncio
async def test_create_session_page_mode_is_visible_in_chat_list(tmp_path):
    session = await create_session(
        body=CreateSessionRequest(title="Page session", persist_mode="session"),
        _user={"sub": "u1"},
        runtime=SimpleNamespace(workspace=tmp_path),
    )

    sessions = await list_sessions(
        _user={},
        runtime=SimpleNamespace(workspace=tmp_path),
        include_remote=False,
        channel=None,
        agent_name=None,
    )

    assert session.channel == "web"
    assert [item.id for item in sessions] == [session.id]


@pytest.mark.asyncio
async def test_api_session_defaults_to_log_persist_mode_for_streaming(tmp_path):
    api_session = await create_session(
        body=CreateSessionRequest(title="API session", agent_name="api-agent"),
        _user=None,
        runtime=SimpleNamespace(workspace=tmp_path),
    )
    web_session = await create_session(
        body=CreateSessionRequest(title="Page session", persist_mode="session"),
        _user={"sub": "u1"},
        runtime=SimpleNamespace(workspace=tmp_path),
    )

    assert _default_persist_mode_for_session(str(tmp_path), api_session.id) == "log"
    assert _default_persist_mode_for_session(str(tmp_path), web_session.id) == "session"


@pytest.mark.asyncio
async def test_gateway_startup_connects_shared_mcp_manager(tmp_path, monkeypatch):
    settings = Settings(
        mcp_servers={"metrics": McpSseServerConfig(url="http://192.168.6.131:8100/sse")},
    )
    connected = []
    closed = []

    class FakeMcpManager:
        def __init__(self, configs):
            self.configs = configs

        async def connect_all(self):
            connected.append(self.configs)

        def list_statuses(self):
            return [
                McpConnectionStatus(
                    name="metrics",
                    state="connected",
                    transport="sse",
                )
            ]

        async def close(self):
            closed.append(True)

    monkeypatch.setattr("openharness.config.load_settings", lambda: settings)
    monkeypatch.setattr("openharness.mcp.config.load_mcp_server_configs", lambda settings, plugins: settings.mcp_servers)
    monkeypatch.setattr("openharness.mcp.client.McpClientManager", FakeMcpManager)

    app = create_app(workspace=str(tmp_path))
    async with app.router.lifespan_context(app):
        pass

    assert connected and "metrics" in connected[0]
    assert closed == [True]


@pytest.mark.asyncio
async def test_list_mcp_servers_syncs_settings_and_returns_sse(tmp_path, monkeypatch):
    settings = Settings(
        mcp_servers={"metrics": McpSseServerConfig(url="http://192.168.6.131:8100/sse")},
    )
    synced = []

    class FakeMcpManager:
        async def sync_server_configs(self, configs):
            synced.append(configs)

        def list_statuses(self):
            return [
                McpConnectionStatus(
                    name="metrics",
                    state="connected",
                    transport="sse",
                    tools=[
                        McpToolInfo(
                            server_name="metrics",
                            name="query_metric_nl",
                            description="Query metrics with natural language",
                            input_schema={"type": "object"},
                        )
                    ],
                )
            ]

    monkeypatch.setattr("openharness.config.load_settings", lambda: settings)

    servers = await list_mcp_servers(
        _user={},
        runtime=SimpleNamespace(workspace=tmp_path, mcp_manager=FakeMcpManager()),
    )

    assert synced == [settings.mcp_servers]
    assert servers[0].name == "metrics"
    assert servers[0].type == "sse"
    assert servers[0].transport == "sse"
    assert servers[0].tools[0]["name"] == "query_metric_nl"


@pytest.mark.asyncio
async def test_chat_artifacts_list_generated_files_from_tool_metadata(tmp_path):
    generated = tmp_path / "report.md"
    generated.write_text("# Report\n", encoding="utf-8")
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)

    save_session_snapshot(
        cwd=tmp_path,
        workspace=workspace,
        model="gpt-5.4",
        system_prompt="system",
        messages=[
            ConversationMessage(
                role="assistant",
                content=[ToolUseBlock(id="toolu_write", name="write_file", input={"path": "report.md"})],
            ),
            ConversationMessage(
                role="user",
                content=[
                    ToolResultBlock(
                        tool_use_id="toolu_write",
                        content=f"Wrote {generated}",
                        metadata={
                            "artifacts": [
                                {
                                    "path": str(generated),
                                    "name": "report.md",
                                    "preview_kind": "markdown",
                                }
                            ]
                        },
                    )
                ],
            ),
        ],
        usage=UsageSnapshot(),
        session_id="artifact-session",
    )

    artifacts = await list_artifacts(
        "artifact-session",
        _user={"sub": "u1"},
        runtime=SimpleNamespace(workspace=workspace),
    )

    assert len(artifacts) == 1
    assert artifacts[0].name == "report.md"
    assert artifacts[0].tool_name == "write_file"
    assert artifacts[0].preview_kind == "markdown"


@pytest.mark.asyncio
async def test_chat_artifacts_list_legacy_generated_files_from_tool_input(tmp_path):
    generated = tmp_path / "legacy.txt"
    generated.write_text("legacy output", encoding="utf-8")
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)

    save_session_snapshot(
        cwd=tmp_path,
        workspace=workspace,
        model="gpt-5.4",
        system_prompt="system",
        messages=[
            ConversationMessage(
                role="assistant",
                content=[ToolUseBlock(id="toolu_write", name="write_file", input={"path": "legacy.txt"})],
            ),
            ConversationMessage(
                role="user",
                content=[ToolResultBlock(tool_use_id="toolu_write", content=f"Wrote {generated}")],
            ),
        ],
        usage=UsageSnapshot(),
        session_id="legacy-artifact-session",
    )

    artifacts = await list_artifacts(
        "legacy-artifact-session",
        _user={"sub": "u1"},
        runtime=SimpleNamespace(workspace=workspace),
    )

    assert len(artifacts) == 1
    assert artifacts[0].name == "legacy.txt"
    assert artifacts[0].tool_name == "write_file"
    assert artifacts[0].preview_kind == "text"


@pytest.mark.asyncio
async def test_chat_artifacts_list_legacy_pptx_from_output_path_label(tmp_path):
    generated = tmp_path / "deck.pptx"
    generated.write_bytes(b"pptx")
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)

    save_session_snapshot(
        cwd=tmp_path,
        workspace=workspace,
        model="gpt-5.4",
        system_prompt="system",
        messages=[
            ConversationMessage(
                role="assistant",
                content=[ToolUseBlock(id="toolu_bash", name="bash", input={"command": "make deck"})],
            ),
            ConversationMessage(
                role="user",
                content=[ToolResultBlock(tool_use_id="toolu_bash", content=f"路径: {generated}")],
            ),
        ],
        usage=UsageSnapshot(),
        session_id="legacy-pptx-artifact-session",
    )

    artifacts = await list_artifacts(
        "legacy-pptx-artifact-session",
        _user={"sub": "u1"},
        runtime=SimpleNamespace(workspace=workspace),
    )

    assert len(artifacts) == 1
    assert artifacts[0].name == "deck.pptx"
    assert artifacts[0].tool_name == "bash"
    assert artifacts[0].preview_kind == "binary"


@pytest.mark.asyncio
async def test_chat_artifacts_ignore_user_upload_attachments(tmp_path):
    uploaded = tmp_path / "uploaded.txt"
    uploaded.write_text("user file", encoding="utf-8")
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)

    save_session_snapshot(
        cwd=tmp_path,
        workspace=workspace,
        model="gpt-5.4",
        system_prompt="system",
        messages=[
            ConversationMessage.from_user_content([TextBlock(text="see attachment")]).model_copy(
                update={"attachments": [{"name": "uploaded.txt", "path": str(uploaded)}]}
            )
        ],
        usage=UsageSnapshot(),
        session_id="attachment-session",
    )

    artifacts = await list_artifacts(
        "attachment-session",
        _user={"sub": "u1"},
        runtime=SimpleNamespace(workspace=workspace),
    )

    assert artifacts == []


@pytest.mark.asyncio
async def test_chat_artifact_content_rejects_unregistered_path(tmp_path):
    generated = tmp_path / "inside.txt"
    generated.write_text("inside", encoding="utf-8")
    outside = tmp_path.parent / "outside-artifact.txt"
    outside.write_text("outside", encoding="utf-8")
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)

    save_session_snapshot(
        cwd=tmp_path,
        workspace=workspace,
        model="gpt-5.4",
        system_prompt="system",
        messages=[
            ConversationMessage(
                role="assistant",
                content=[ToolUseBlock(id="toolu_write", name="write_file", input={"path": "inside.txt"})],
            ),
            ConversationMessage(
                role="user",
                content=[
                    ToolResultBlock(
                        tool_use_id="toolu_write",
                        content=f"Wrote {outside}",
                        metadata={"artifacts": [{"path": str(outside), "name": outside.name}]},
                    )
                ],
            ),
        ],
        usage=UsageSnapshot(),
        session_id="unsafe-artifact-session",
    )

    artifacts = await list_artifacts(
        "unsafe-artifact-session",
        _user={"sub": "u1"},
        runtime=SimpleNamespace(workspace=workspace),
    )
    assert artifacts == []

    with pytest.raises(HTTPException) as exc:
        await get_artifact_content(
            "unsafe-artifact-session",
            "missing",
            _user={"sub": "u1"},
            runtime=SimpleNamespace(workspace=workspace),
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_chat_send_message_returns_sse_error_when_provider_auth_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("openharness.config.load_settings", lambda: SimpleNamespace())

    def missing_auth(settings):
        del settings
        raise SystemExit(1)

    monkeypatch.setattr("openharness.ui.runtime._resolve_api_client_from_settings", missing_auth)

    response = await send_message(
        session_id="session-1",
        body=MessageRequest(content="hello"),
        auth=AuthContext(user={"sub": "u1"}, raw_token="token"),
        runtime=SimpleNamespace(workspace=tmp_path),
    )

    chunks = [chunk async for chunk in response.body_iterator]
    payload = "".join(chunk.decode() if isinstance(chunk, bytes) else chunk for chunk in chunks)

    assert '"event":"error"' in payload
    assert "Authentication is not configured" in payload
    assert '"recoverable":false' in payload


@pytest.mark.asyncio
async def test_chat_send_message_sync_returns_json_error_when_provider_auth_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("openharness.config.load_settings", lambda: SimpleNamespace())

    def missing_auth(settings):
        del settings
        raise SystemExit(1)

    monkeypatch.setattr("openharness.ui.runtime._resolve_api_client_from_settings", missing_auth)

    response = await send_message_sync(
        session_id="session-1",
        body=MessageRequest(content="hello"),
        auth=None,
        runtime=SimpleNamespace(workspace=tmp_path),
    )

    assert response.session_id == "session-1"
    assert response.status == "error"
    assert response.text == ""
    assert response.error is not None
    assert "Authentication is not configured" in response.error
    assert response.recoverable is False


def test_message_request_json_response_format_does_not_add_no_think_prefix():
    body = MessageRequest(content="Return JSON only", response_format="json")

    assert _prepare_user_text(body) == "Return JSON only"


def test_message_request_json_response_format_preserves_existing_content():
    body = MessageRequest(content="/no_think\nReturn JSON only", response_format="json")

    assert _prepare_user_text(body) == "/no_think\nReturn JSON only"


def test_message_request_text_response_format_does_not_add_no_think_prefix():
    body = MessageRequest(content="normal chat", response_format="text")

    assert _prepare_user_text(body) == "normal chat"


def test_agent_tool_policy_supports_aliases_blacklist_and_empty_whitelist():
    registry = SimpleNamespace(
        _tools={"read_file": object(), "bash": object(), "web_search": object()}
    )
    agent = AgentDefinition(
        name="limited",
        description="Limited tools",
        tools=["Read", "Bash"],
        disallowed_tools=["Bash"],
    )

    apply_agent_tool_policy(registry, agent)

    assert set(registry._tools) == {"read_file"}

    no_tools_registry = SimpleNamespace(_tools={"read_file": object(), "bash": object()})
    no_tools_agent = AgentDefinition(name="none", description="No tools", tools=[])
    apply_agent_tool_policy(no_tools_registry, no_tools_agent)
    assert no_tools_registry._tools == {}


@pytest.mark.asyncio
async def test_agent_update_can_clear_tool_whitelist(tmp_path, monkeypatch):
    agent_file = tmp_path / "limited-agent.md"
    agent_file.write_text("---\nname: limited-agent\ntools:\n- file_read\n---\n\nPrompt", encoding="utf-8")
    existing = AgentDefinition(
        name="limited-agent",
        description="Limited agent",
        system_prompt="Prompt",
        tools=["file_read"],
    )
    monkeypatch.setattr("ohmo.gateway.routers.agents._agent_file", lambda _name: agent_file)
    monkeypatch.setattr("ohmo.gateway.routers.agents._agents_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "openharness.coordinator.agent_definitions.load_agents_dir",
        lambda _path: [existing],
    )

    response = await update_agent(
        name="limited-agent",
        body=UpdateAgentRequest(tools=None),
        _user={},
    )

    assert response.tools is None
    assert "\ntools:" not in agent_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_agent_tool_catalog_lists_builtin_tools():
    tools = await list_agent_tools(_user={}, runtime=SimpleNamespace(mcp_manager=None))

    names = {tool.name for tool in tools}
    assert {"bash", "read_file", "write_file", "web_search"}.issubset(names)


@pytest.mark.asyncio
async def test_sync_message_passes_model_token_settings_to_query_engine(tmp_path, monkeypatch):
    settings = Settings(
        model="Qwen36_30b",
        max_tokens=40000,
        context_window_tokens=220000,
        auto_compact_threshold_tokens=180000,
    )
    monkeypatch.setattr("openharness.config.load_settings", lambda: settings)
    monkeypatch.setattr("openharness.ui.runtime._resolve_api_client_from_settings", lambda _: object())
    monkeypatch.setattr("openharness.mcp.config.load_mcp_server_configs", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("openharness.utils.internal_api_auth.make_hsjm_auth_metadata", lambda token: None)
    monkeypatch.setattr(
        "ohmo.gateway.routers.chat.load_by_id",
        lambda **_kwargs: {"agent_name": "limited-agent"},
    )
    monkeypatch.setattr(
        "openharness.coordinator.agent_definitions.get_agent_definition",
        lambda name: AgentDefinition(
            name=name,
            description="Limited agent",
            tools=["Read", "Bash"],
            disallowed_tools=["Bash"],
        ),
    )

    class FakeMcpManager:
        async def connect_all(self):
            return None

        async def close(self):
            return None

    class FakeToolRegistry:
        def __init__(self):
            self._tools = {"read_file": object(), "bash": object(), "write_file": object()}

        def to_api_schema(self):
            return []

    captured: dict[str, object] = {}

    class FakeEngine:
        messages = []
        total_usage = UsageSnapshot()

        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def submit_message(self, _message):
            yield AssistantTextDelta(text="{}")

    monkeypatch.setattr("openharness.mcp.client.McpClientManager", lambda *args, **kwargs: FakeMcpManager())
    monkeypatch.setattr("openharness.tools.create_default_tool_registry", lambda _manager: FakeToolRegistry())
    monkeypatch.setattr("openharness.engine.QueryEngine", FakeEngine)

    response = await send_message_sync(
        session_id="session-1",
        body=MessageRequest(content="Return JSON", response_format="json"),
        auth=None,
        runtime=SimpleNamespace(workspace=tmp_path),
    )

    assert response.text == "{}"
    assert captured["max_tokens"] == 40000
    assert captured["context_window_tokens"] == 220000
    assert captured["auto_compact_threshold_tokens"] == 180000
    assert set(captured["tool_registry"]._tools) == {"read_file"}


@pytest.mark.asyncio
async def test_sync_message_log_persist_mode_writes_invocation_not_session(tmp_path, monkeypatch):
    settings = Settings(model="test-model")
    monkeypatch.setattr("openharness.config.load_settings", lambda: settings)
    monkeypatch.setattr("openharness.ui.runtime._resolve_api_client_from_settings", lambda _: object())
    monkeypatch.setattr("openharness.mcp.config.load_mcp_server_configs", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("openharness.utils.internal_api_auth.make_hsjm_auth_metadata", lambda token: None)

    class FakeMcpManager:
        async def connect_all(self):
            return None

        async def close(self):
            return None

    class FakeToolRegistry:
        def to_api_schema(self):
            return []

    class FakeEngine:
        total_usage = UsageSnapshot()

        def __init__(self, **_kwargs):
            self.messages = []

        async def submit_message(self, message):
            assistant = ConversationMessage(role="assistant", content=[TextBlock(text="done")])
            self.messages = [message, assistant]
            yield AssistantTextDelta(text="done")
            yield AssistantTurnComplete(message=assistant, usage=UsageSnapshot())

    monkeypatch.setattr("openharness.mcp.client.McpClientManager", lambda *args, **kwargs: FakeMcpManager())
    monkeypatch.setattr("openharness.tools.create_default_tool_registry", lambda _manager: FakeToolRegistry())
    monkeypatch.setattr("openharness.engine.QueryEngine", FakeEngine)

    response = await send_message_sync(
        session_id="api-call-1",
        body=MessageRequest(content="run this"),
        auth=None,
        runtime=SimpleNamespace(workspace=tmp_path),
    )

    assert response.status == "completed"
    assert response.text == "done"
    assert response.invocation_id
    assert not list((tmp_path / "sessions").glob("session-*.json"))
    records = list((tmp_path / "invocations").glob("invocation-*.json"))
    assert len(records) == 1
    payload = json.loads(records[0].read_text(encoding="utf-8"))
    assert payload["kind"] == "agent_invocation"
    assert payload["session_id"] == "api-call-1"
    assert payload["request_content"] == "run this"
    assert payload["response_text"] == "done"


def test_channel_manager_expands_dingtalk_bots_and_skips_missing_agent(monkeypatch, caplog):
    monkeypatch.setattr(
        "openharness.channels.impl.manager.ChannelManager._agent_exists",
        staticmethod(lambda name: name != "missing-agent"),
    )
    config = Config()
    config.channels.dingtalk = DingTalkConfig(
        enabled=True,
        bots=[
            DingTalkBotConfig(
                name="ops-bot",
                client_id="ops-id",
                client_secret="ops-secret",
                robot_code="ops-robot",
                allow_from=["*"],
                default_agent="worker",
            ),
            DingTalkBotConfig(
                name="bad-bot",
                client_id="bad-id",
                client_secret="bad-secret",
                allow_from=["*"],
                default_agent="missing-agent",
            ),
        ],
    )

    with caplog.at_level(logging.ERROR):
        manager = ChannelManager(config, MessageBus())

    assert list(manager.channels) == ["dingtalk:ops-bot"]
    channel = manager.channels["dingtalk:ops-bot"]
    assert channel.name == "dingtalk:ops-bot"
    assert channel.bot_name == "ops-bot"
    assert channel.default_agent == "worker"
    assert "DingTalk bot bad-bot skipped: default_agent missing-agent not found" in caplog.text


def test_gateway_config_validates_dingtalk_bots_from_json_dicts():
    config = GatewayConfig(
        enabled_channels=["dingtalk"],
        channel_configs={
            "dingtalk": {
                "bots": [
                    {
                        "name": "dingtalk-bot",
                        "client_id": "client-id",
                        "client_secret": "client-secret",
                        "robot_code": "",
                        "allow_from": ["*"],
                        "default_agent": "general-purpose",
                    }
                ]
            }
        },
    )

    channel_config = build_channel_manager_config(config).channels.dingtalk

    assert isinstance(channel_config.bots[0], DingTalkBotConfig)
    assert channel_config.bots[0].name == "dingtalk-bot"
    assert channel_config.bots[0].default_agent == "general-purpose"


def test_channel_manager_keeps_legacy_single_dingtalk_config(monkeypatch):
    monkeypatch.setattr(
        "openharness.channels.impl.manager.ChannelManager._agent_exists",
        staticmethod(lambda name: True),
    )
    config = Config()
    config.channels.dingtalk = DingTalkConfig(
        enabled=True,
        client_id="legacy-id",
        client_secret="legacy-secret",
        robot_code="legacy-robot",
        allow_from=["u1"],
        default_agent="worker",
    )

    manager = ChannelManager(config, MessageBus())

    assert list(manager.channels) == ["dingtalk:default"]
    channel = manager.channels["dingtalk:default"]
    assert channel.bot_name == "default"
    assert channel.config.client_id == "legacy-id"
    assert channel.config.allow_from == ["u1"]


@pytest.mark.asyncio
async def test_dingtalk_channel_inbound_uses_instance_channel_and_agent_metadata():
    from openharness.channels.impl.dingtalk import DingTalkChannel

    bus = MessageBus()
    channel = DingTalkChannel(
        DingTalkBotConfig(
            name="ops-bot",
            client_id="ops-id",
            client_secret="ops-secret",
            allow_from=["staff-1"],
            default_agent="ops-agent",
        ),
        bus,
        channel_name="dingtalk:ops-bot",
        bot_name="ops-bot",
        default_agent="ops-agent",
    )

    await channel._on_message("hello", "staff-1", "Alice")
    inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=1.0)

    assert inbound.channel == "dingtalk:ops-bot"
    assert inbound.metadata["bot_name"] == "ops-bot"
    assert inbound.metadata["default_agent"] == "ops-agent"
    assert inbound.metadata["platform"] == "dingtalk"


@pytest.mark.asyncio
async def test_dingtalk_send_prefers_robot_code_over_client_id():
    from openharness.channels.impl.dingtalk import DingTalkChannel

    calls: list[dict[str, object]] = []

    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {}

    class FakeHttp:
        async def post(self, url, json=None, headers=None, files=None):
            del url, headers, files
            calls.append(json)
            return FakeResponse()

    channel = DingTalkChannel(
        DingTalkBotConfig(
            name="ops-bot",
            client_id="client-id",
            client_secret="secret",
            robot_code="robot-code",
            allow_from=["*"],
        ),
        MessageBus(),
        channel_name="dingtalk:ops-bot",
        bot_name="ops-bot",
    )
    channel._http = FakeHttp()

    ok = await channel._send_batch_message("token", "user-1", "sampleMarkdown", {"text": "hi"})

    assert ok is True
    assert calls[0]["robotCode"] == "robot-code"


def test_gateway_error_formats_claude_refresh_failure():
    exc = ValueError("Claude OAuth refresh failed: HTTP Error 400: Bad Request")
    assert "claude-login" in _format_gateway_error(exc)
    assert "Claude subscription auth refresh failed" in _format_gateway_error(exc)


def test_gateway_error_formats_generic_auth_failure():
    exc = ValueError("API key missing for current profile")
    assert "Authentication failed" in _format_gateway_error(exc)


def test_compact_progress_formats_reactive_channel_hint_in_chinese():
    text = _format_channel_progress(
        channel="feishu",
        kind="compact_progress",
        text="",
        session_key="feishu:c1",
        content="帮我继续处理",
        compact_phase="compact_start",
        compact_trigger="reactive",
        attempt=None,
    )
    assert "重试" in text


def test_gateway_status_prefers_live_config_over_stale_state(tmp_path):
    workspace = tmp_path / ".ohmo-home"
    workspace.mkdir()
    (workspace / "gateway.json").write_text(
        json.dumps({"provider_profile": "codex", "enabled_channels": ["feishu"]}) + "\n",
        encoding="utf-8",
    )
    (workspace / "state.json").write_text(
        GatewayState(
            running=False,
            provider_profile="claude-subscription",
            enabled_channels=["feishu"],
        ).model_dump_json(indent=2)
        + "\n",
        encoding="utf-8",
    )
    state = gateway_status(tmp_path, workspace)
    assert state.running is False
    assert state.provider_profile == "codex"
    assert state.enabled_channels == ["feishu"]


def test_stop_gateway_process_kills_matching_workspace_processes(tmp_path, monkeypatch):
    workspace = tmp_path / ".ohmo-home"
    workspace.mkdir()
    (workspace / "gateway.json").write_text('{"provider_profile":"codex"}\n', encoding="utf-8")
    (workspace / "gateway.pid").write_text("123\n", encoding="utf-8")

    killed: list[int] = []
    taskkilled: list[int] = []

    def fake_run(*args, **kwargs):
        command = args[0] if args else []
        if command and command[0] == "taskkill":
            taskkilled.append(int(command[-1]))

            class TaskkillResult:
                stdout = ""

            return TaskkillResult()
        if command and command[0] == "wmic":
            class WmicResult:
                stdout = "ProcessId\n123\n456\n"

            return WmicResult()

        class Result:
            stdout = (
                f"123 python -m ohmo gateway run --workspace {workspace}\n"
                f"456 python -m ohmo gateway run --workspace {workspace}\n"
            )

        return Result()

    monkeypatch.setattr("ohmo.gateway.service.subprocess.run", fake_run)
    monkeypatch.setattr("ohmo.gateway.service._pid_is_running", lambda pid: True)
    monkeypatch.setattr("ohmo.gateway.service.os.kill", lambda pid, sig: killed.append(pid))

    assert stop_gateway_process(tmp_path, workspace) is True
    assert (killed or taskkilled) == [123, 456]


@pytest.mark.asyncio
async def test_runtime_pool_restores_messages_for_sender_scoped_session_key(tmp_path, monkeypatch):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)
    save_session_snapshot(
        cwd=tmp_path,
        workspace=workspace,
        model="gpt-5.4",
        system_prompt="system",
        messages=[ConversationMessage.from_user_text("remember alice only")],
        usage=UsageSnapshot(),
        session_id="sess123",
        session_key="feishu:chat-1:alice",
    )

    captured: dict[str, object] = {}

    async def fake_build_runtime(**kwargs):
        captured["restore_messages"] = kwargs.get("restore_messages")
        return SimpleNamespace(
            engine=SimpleNamespace(set_system_prompt=lambda prompt: None, messages=[]),
            session_id="newsession",
        )

    async def fake_start_runtime(bundle):
        return None

    monkeypatch.setattr("ohmo.gateway.runtime.build_runtime", fake_build_runtime)
    monkeypatch.setattr("ohmo.gateway.runtime.start_runtime", fake_start_runtime)

    pool = OhmoSessionRuntimePool(cwd=tmp_path, workspace=workspace, provider_profile="codex")
    bundle = await pool.get_bundle("feishu:chat-1:alice")

    assert captured["restore_messages"] is not None
    assert bundle.session_id == "sess123"


@pytest.mark.asyncio
async def test_runtime_pool_does_not_restore_other_sender_session_key(tmp_path, monkeypatch):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)
    save_session_snapshot(
        cwd=tmp_path,
        workspace=workspace,
        model="gpt-5.4",
        system_prompt="system",
        messages=[ConversationMessage.from_user_text("remember alice only")],
        usage=UsageSnapshot(),
        session_id="sess123",
        session_key="feishu:chat-1:alice",
    )

    captured: dict[str, object] = {}

    async def fake_build_runtime(**kwargs):
        captured["restore_messages"] = kwargs.get("restore_messages")
        return SimpleNamespace(
            engine=SimpleNamespace(set_system_prompt=lambda prompt: None, messages=[]),
            session_id="newsession",
        )

    async def fake_start_runtime(bundle):
        return None

    monkeypatch.setattr("ohmo.gateway.runtime.build_runtime", fake_build_runtime)
    monkeypatch.setattr("ohmo.gateway.runtime.start_runtime", fake_start_runtime)

    pool = OhmoSessionRuntimePool(cwd=tmp_path, workspace=workspace, provider_profile="codex")
    bundle = await pool.get_bundle("feishu:chat-1:bob")

    assert captured["restore_messages"] is None
    assert bundle.session_id == "newsession"


@pytest.mark.asyncio
async def test_runtime_pool_applies_dingtalk_bound_agent_prompt_model_and_tools(tmp_path, monkeypatch):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)
    captured: dict[str, object] = {}

    agent = AgentDefinition(
        name="ops-agent",
        description="Ops assistant",
        system_prompt="You are the ops DingTalk agent.",
        model="ops-model",
        permission_mode="dontAsk",
        max_turns=7,
        tools=["Read", "Bash"],
        disallowed_tools=["Bash"],
    )
    monkeypatch.setattr(
        "ohmo.gateway.runtime.get_agent_definition",
        lambda name: agent if name == "ops-agent" else None,
    )

    async def fake_build_runtime(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            engine=SimpleNamespace(set_system_prompt=lambda prompt: None, messages=[]),
            session_id="newsession",
            current_settings=lambda: Settings(system_prompt=kwargs["system_prompt"], model=kwargs["model"]),
            commands=create_default_command_registry(),
            tool_registry=SimpleNamespace(
                _tools={"file_read": object(), "bash": object(), "file_write": object()}
            ),
        )

    async def fake_start_runtime(bundle):
        return None

    monkeypatch.setattr("ohmo.gateway.runtime.build_runtime", fake_build_runtime)
    monkeypatch.setattr("ohmo.gateway.runtime.start_runtime", fake_start_runtime)

    pool = OhmoSessionRuntimePool(cwd=tmp_path, workspace=workspace, provider_profile="codex")
    message = InboundMessage(
        channel="dingtalk:ops-bot",
        sender_id="u1",
        chat_id="u1",
        content="hello",
        metadata={"default_agent": "ops-agent"},
    )
    bundle = await pool.get_bundle(
        "dingtalk:ops-bot:ops-agent:u1:u1",
        latest_user_prompt="hello",
        agent_name=message.metadata["default_agent"],
    )

    assert captured["system_prompt"] == "You are the ops DingTalk agent."
    assert captured["model"] == "ops-model"
    assert captured["permission_mode"] == "dontAsk"
    assert captured["max_turns"] == 7
    assert set(bundle.tool_registry._tools) == {"file_read"}


@pytest.mark.asyncio
async def test_runtime_pool_stream_message_emits_progress_and_tool_hint(tmp_path, monkeypatch):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)

    async def fake_build_runtime(**kwargs):
        class FakeEngine:
            messages = []
            total_usage = UsageSnapshot()

            def set_system_prompt(self, prompt):
                return None

            async def submit_message(self, content):
                yield ToolExecutionStarted(tool_name="web_fetch", tool_input={"url": "https://example.com"})
                yield AssistantTextDelta(text="done")

        return SimpleNamespace(
            engine=FakeEngine(),
            cwd=str(tmp_path),
            session_id="sess123",
            current_settings=lambda: SimpleNamespace(model="gpt-5.4"),
            commands=SimpleNamespace(lookup=lambda raw: None),
        )

    async def fake_start_runtime(bundle):
        return None

    monkeypatch.setattr("ohmo.gateway.runtime.build_runtime", fake_build_runtime)
    monkeypatch.setattr("ohmo.gateway.runtime.start_runtime", fake_start_runtime)

    pool = OhmoSessionRuntimePool(cwd=tmp_path, workspace=workspace, provider_profile="codex")
    message = InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="check")
    updates = [u async for u in pool.stream_message(message, "feishu:c1")]

    assert updates[0].kind == "progress"
    assert updates[0].text.startswith(("🤔", "🧠", "✨", "🔎", "🪄"))
    assert updates[1].kind == "tool_hint"
    assert updates[1].text.startswith("🛠️ ")
    assert "web_fetch" in updates[1].text
    assert updates[-1].kind == "final"
    assert updates[-1].text == "done"


@pytest.mark.asyncio
async def test_runtime_pool_stream_message_sets_dingtalk_internal_api_token(tmp_path, monkeypatch):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)
    captured: dict[str, object] = {}

    async def fake_build_runtime(**kwargs):
        class FakeEngine:
            def __init__(self):
                self.messages = []
                self.total_usage = UsageSnapshot()
                self.tool_metadata: dict[str, object] = {}

            def set_system_prompt(self, prompt):
                return None

            async def submit_message(self, content):
                captured["hsjm_auth"] = self.tool_metadata.get("hsjm_auth")
                yield AssistantTextDelta(text="done")

        return SimpleNamespace(
            engine=FakeEngine(),
            cwd=str(tmp_path),
            session_id="sess123",
            current_settings=lambda: SimpleNamespace(model="gpt-5.4"),
            commands=SimpleNamespace(lookup=lambda raw: None),
        )

    async def fake_start_runtime(bundle):
        return None

    monkeypatch.setattr("ohmo.gateway.runtime.build_runtime", fake_build_runtime)
    monkeypatch.setattr("ohmo.gateway.runtime.start_runtime", fake_start_runtime)

    pool = OhmoSessionRuntimePool(cwd=tmp_path, workspace=workspace, provider_profile="codex")
    message = InboundMessage(channel="dingtalk:ops-bot", sender_id="u1", chat_id="c1", content="check")
    updates = [u async for u in pool.stream_message(message, "dingtalk:ops-bot:ops-agent:c1:u1")]

    assert updates[-1].text == "done"
    assert captured["hsjm_auth"] == {"token": "123"}


@pytest.mark.asyncio
async def test_runtime_pool_stream_message_preserves_non_dingtalk_internal_api_token(tmp_path, monkeypatch):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)
    captured: dict[str, object] = {}

    async def fake_build_runtime(**kwargs):
        class FakeEngine:
            def __init__(self):
                self.messages = []
                self.total_usage = UsageSnapshot()
                self.tool_metadata: dict[str, object] = {"hsjm_auth": {"token": "web-token"}}

            def set_system_prompt(self, prompt):
                return None

            async def submit_message(self, content):
                captured["hsjm_auth"] = self.tool_metadata.get("hsjm_auth")
                yield AssistantTextDelta(text="done")

        return SimpleNamespace(
            engine=FakeEngine(),
            cwd=str(tmp_path),
            session_id="sess123",
            current_settings=lambda: SimpleNamespace(model="gpt-5.4"),
            commands=SimpleNamespace(lookup=lambda raw: None),
        )

    async def fake_start_runtime(bundle):
        return None

    monkeypatch.setattr("ohmo.gateway.runtime.build_runtime", fake_build_runtime)
    monkeypatch.setattr("ohmo.gateway.runtime.start_runtime", fake_start_runtime)

    pool = OhmoSessionRuntimePool(cwd=tmp_path, workspace=workspace, provider_profile="codex")
    message = InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="check")
    updates = [u async for u in pool.stream_message(message, "feishu:c1:u1")]

    assert updates[-1].text == "done"
    assert captured["hsjm_auth"] == {"token": "web-token"}


@pytest.mark.asyncio
async def test_runtime_pool_checkpoints_user_message_before_tool_results(tmp_path, monkeypatch):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)

    async def fake_build_runtime(**kwargs):
        class FakeEngine:
            def __init__(self):
                self.messages = []
                self.total_usage = UsageSnapshot()

            def set_system_prompt(self, prompt):
                return None

            async def submit_message(self, content):
                self.messages.append(content)
                assistant = ConversationMessage(
                    role="assistant",
                    content=[
                        ToolUseBlock(
                            id="toolu_metrics",
                            name="mcp__metrics__query_metric_nl",
                            input={"user_query": "OP020 OEE"},
                        )
                    ],
                )
                self.messages.append(assistant)
                yield AssistantTurnComplete(message=assistant, usage=UsageSnapshot())

        return SimpleNamespace(
            engine=FakeEngine(),
            cwd=str(tmp_path),
            session_id="sess123",
            current_settings=lambda: SimpleNamespace(model="gpt-5.4"),
            commands=SimpleNamespace(lookup=lambda raw: None),
        )

    async def fake_start_runtime(bundle):
        return None

    monkeypatch.setattr("ohmo.gateway.runtime.build_runtime", fake_build_runtime)
    monkeypatch.setattr("ohmo.gateway.runtime.start_runtime", fake_start_runtime)

    pool = OhmoSessionRuntimePool(cwd=tmp_path, workspace=workspace, provider_profile="codex")
    message = InboundMessage(channel="dingtalk:dingtalk-bot", sender_id="u1", chat_id="c1", content="OP020 OEE")
    updates = [u async for u in pool.stream_message(message, "dingtalk:dingtalk-bot:生产助手:c1:u1")]

    snapshot = load_latest_for_session_key(workspace, "dingtalk:dingtalk-bot:生产助手:c1:u1")
    assert updates[0].kind == "progress"
    assert snapshot is not None
    assert snapshot["message_count"] == 1
    assert snapshot["messages"][0]["role"] == "user"
    assert snapshot["messages"][0]["content"][0]["text"] == "OP020 OEE"


@pytest.mark.asyncio
async def test_runtime_pool_stream_message_formats_auto_compact_status_for_feishu(tmp_path, monkeypatch):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)

    async def fake_build_runtime(**kwargs):
        class FakeEngine:
            messages = []
            total_usage = UsageSnapshot()

            def set_system_prompt(self, prompt):
                return None

            async def submit_message(self, content):
                yield CompactProgressEvent(phase="compact_start", trigger="auto")
                yield AssistantTextDelta(text="done")

        return SimpleNamespace(
            engine=FakeEngine(),
            cwd=str(tmp_path),
            session_id="sess123",
            current_settings=lambda: SimpleNamespace(model="gpt-5.4"),
            commands=SimpleNamespace(lookup=lambda raw: None),
        )

    async def fake_start_runtime(bundle):
        return None

    monkeypatch.setattr("ohmo.gateway.runtime.build_runtime", fake_build_runtime)
    monkeypatch.setattr("ohmo.gateway.runtime.start_runtime", fake_start_runtime)

    pool = OhmoSessionRuntimePool(cwd=tmp_path, workspace=workspace, provider_profile="codex")
    message = InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="继续")
    updates = [u async for u in pool.stream_message(message, "feishu:c1")]

    assert updates[1].kind == "progress"
    assert updates[1].text == "🧠 聊天有点长啦，我先帮你悄悄压缩一下记忆，马上继续～"
    assert updates[-1].kind == "final"
    assert updates[-1].text == "done"


@pytest.mark.asyncio
async def test_runtime_pool_stream_message_formats_compact_retry_for_feishu(tmp_path, monkeypatch):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)

    async def fake_build_runtime(**kwargs):
        class FakeEngine:
            messages = []
            total_usage = UsageSnapshot()

            def set_system_prompt(self, prompt):
                return None

            async def submit_message(self, content):
                yield CompactProgressEvent(phase="compact_retry", trigger="auto", attempt=2, message="retrying")
                yield AssistantTextDelta(text="done")

        return SimpleNamespace(
            engine=FakeEngine(),
            session_id="sess123",
            current_settings=lambda: SimpleNamespace(model="gpt-5.4"),
            commands=SimpleNamespace(lookup=lambda raw: None),
        )

    async def fake_start_runtime(bundle):
        return None

    monkeypatch.setattr("ohmo.gateway.runtime.build_runtime", fake_build_runtime)
    monkeypatch.setattr("ohmo.gateway.runtime.start_runtime", fake_start_runtime)

    pool = OhmoSessionRuntimePool(cwd=tmp_path, workspace=workspace, provider_profile="codex")
    message = InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="继续")
    updates = [u async for u in pool.stream_message(message, "feishu:c1")]

    assert updates[1].kind == "progress"
    assert "再试一次" in updates[1].text


@pytest.mark.asyncio
async def test_runtime_pool_stream_message_formats_compact_hooks_start_for_feishu(tmp_path, monkeypatch):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)

    async def fake_build_runtime(**kwargs):
        class FakeEngine:
            messages = []
            total_usage = UsageSnapshot()

            def set_system_prompt(self, prompt):
                return None

            async def submit_message(self, content):
                yield CompactProgressEvent(phase="hooks_start", trigger="auto")
                yield AssistantTextDelta(text="done")

        return SimpleNamespace(
            engine=FakeEngine(),
            session_id="sess123",
            current_settings=lambda: SimpleNamespace(model="gpt-5.4"),
            commands=SimpleNamespace(lookup=lambda raw: None),
        )

    async def fake_start_runtime(bundle):
        return None

    monkeypatch.setattr("ohmo.gateway.runtime.build_runtime", fake_build_runtime)
    monkeypatch.setattr("ohmo.gateway.runtime.start_runtime", fake_start_runtime)

    pool = OhmoSessionRuntimePool(cwd=tmp_path, workspace=workspace, provider_profile="codex")
    message = InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="继续")
    updates = [u async for u in pool.stream_message(message, "feishu:c1")]

    assert updates[1].kind == "progress"
    assert "准备" in updates[1].text


@pytest.mark.asyncio
async def test_runtime_pool_stream_message_uses_english_progress_for_english_input(tmp_path, monkeypatch):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)

    async def fake_build_runtime(**kwargs):
        class FakeEngine:
            messages = []
            total_usage = UsageSnapshot()

            def set_system_prompt(self, prompt):
                return None

            async def submit_message(self, content):
                yield ToolExecutionStarted(tool_name="web_fetch", tool_input={"url": "https://example.com"})
                yield AssistantTextDelta(text="done")

        return SimpleNamespace(
            engine=FakeEngine(),
            session_id="sess123",
            current_settings=lambda: SimpleNamespace(model="gpt-5.4"),
            commands=SimpleNamespace(lookup=lambda raw: None),
        )

    async def fake_start_runtime(bundle):
        return None

    monkeypatch.setattr("ohmo.gateway.runtime.build_runtime", fake_build_runtime)
    monkeypatch.setattr("ohmo.gateway.runtime.start_runtime", fake_start_runtime)

    pool = OhmoSessionRuntimePool(cwd=tmp_path, workspace=workspace, provider_profile="codex")
    message = InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="can you check this")
    updates = [u async for u in pool.stream_message(message, "feishu:c1")]

    assert updates[0].kind == "progress"
    assert updates[0].text.startswith(("🤔", "🧠", "✨", "🔎", "🪄"))
    assert "Thinking" in updates[0].text or "Working" in updates[0].text or "Looking" in updates[0].text or "Following" in updates[0].text or "Pulling" in updates[0].text
    assert updates[1].kind == "tool_hint"
    assert updates[1].text.startswith("🛠️ Using web_fetch")


@pytest.mark.asyncio
async def test_runtime_pool_blocks_local_only_commands_from_remote_messages(tmp_path, monkeypatch):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)
    handler_called = False

    async def forbidden_handler(args, context):
        nonlocal handler_called
        handler_called = True
        return CommandResult(message="should not run")

    async def fake_build_runtime(**kwargs):
        class FakeEngine:
            messages = []
            total_usage = UsageSnapshot()

            def set_system_prompt(self, prompt):
                return None

        command = SlashCommand(
            "permissions",
            "Show or update permission mode",
            forbidden_handler,
            remote_invocable=False,
        )
        command.remote_admin_opt_in = True
        return SimpleNamespace(
            engine=FakeEngine(),
            session_id="sess123",
            current_settings=lambda: SimpleNamespace(model="gpt-5.4"),
            commands=SimpleNamespace(lookup=lambda raw: (command, "full_auto")),
        )

    async def fake_start_runtime(bundle):
        return None

    monkeypatch.setattr("ohmo.gateway.runtime.build_runtime", fake_build_runtime)
    monkeypatch.setattr("ohmo.gateway.runtime.start_runtime", fake_start_runtime)

    pool = OhmoSessionRuntimePool(cwd=tmp_path, workspace=workspace, provider_profile="codex")
    message = InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="/permissions full_auto")
    updates = [u async for u in pool.stream_message(message, "feishu:c1")]

    assert handler_called is False
    assert updates[-1].kind == "final"
    assert updates[-1].text == "/permissions is only available in the local OpenHarness UI."


@pytest.mark.asyncio
async def test_runtime_pool_blocks_bridge_spawn_from_remote_messages(tmp_path, monkeypatch):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)
    handler_called = False

    async def forbidden_bridge_handler(args, context):
        nonlocal handler_called
        handler_called = True
        return CommandResult(message="spawned")

    async def fake_build_runtime(**kwargs):
        class FakeEngine:
            messages = []
            total_usage = UsageSnapshot()

            def set_system_prompt(self, prompt):
                return None

        command = SlashCommand(
            "bridge",
            "Inspect bridge helpers and spawn bridge sessions",
            forbidden_bridge_handler,
            remote_invocable=False,
            remote_admin_opt_in=True,
        )
        return SimpleNamespace(
            engine=FakeEngine(),
            session_id="sess123",
            current_settings=lambda: SimpleNamespace(model="gpt-5.4"),
            commands=SimpleNamespace(lookup=lambda raw: (command, "spawn id")),
        )

    async def fake_start_runtime(bundle):
        return None

    monkeypatch.setattr("ohmo.gateway.runtime.build_runtime", fake_build_runtime)
    monkeypatch.setattr("ohmo.gateway.runtime.start_runtime", fake_start_runtime)

    pool = OhmoSessionRuntimePool(cwd=tmp_path, workspace=workspace, provider_profile="codex")
    message = InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="/bridge spawn id")
    updates = [u async for u in pool.stream_message(message, "feishu:c1")]

    assert handler_called is False
    assert updates[-1].kind == "final"
    assert updates[-1].text == "/bridge is only available in the local OpenHarness UI."


@pytest.mark.asyncio
async def test_runtime_pool_blocks_registered_bridge_spawn_without_shelling_out(tmp_path, monkeypatch):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)
    marker = tmp_path / "remote-bridge-marker.txt"
    payload = f"/bridge spawn printf REMOTE_BRIDGE_EXEC > {marker}"
    registry = create_default_command_registry()
    command, _ = registry.lookup(payload)
    existing_bridge_sessions = {session.session_id for session in get_bridge_manager().list_sessions()}

    assert command is not None
    assert command.name == "bridge"

    async def fake_build_runtime(**kwargs):
        class FakeEngine:
            messages = []
            total_usage = UsageSnapshot()

            def set_system_prompt(self, prompt):
                return None

        return SimpleNamespace(
            engine=FakeEngine(),
            cwd=str(tmp_path),
            session_id="sess123",
            current_settings=lambda: SimpleNamespace(model="gpt-5.4"),
            commands=registry,
        )

    async def fake_start_runtime(bundle):
        return None

    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("ohmo.gateway.runtime.build_runtime", fake_build_runtime)
    monkeypatch.setattr("ohmo.gateway.runtime.start_runtime", fake_start_runtime)

    pool = OhmoSessionRuntimePool(cwd=tmp_path, workspace=workspace, provider_profile="codex")
    message = InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content=payload)
    updates = [u async for u in pool.stream_message(message, "feishu:c1")]

    assert updates[-1].kind == "final"
    assert updates[-1].text == "/bridge is only available in the local OpenHarness UI."
    assert {session.session_id for session in get_bridge_manager().list_sessions()} == existing_bridge_sessions
    assert marker.exists() is False


@pytest.mark.asyncio
async def test_runtime_pool_memory_command_uses_ohmo_personal_memory(tmp_path, monkeypatch):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)
    registry = create_default_command_registry()

    async def fake_build_runtime(**kwargs):
        class FakeEngine:
            messages = []
            total_usage = UsageSnapshot()

            def set_system_prompt(self, prompt):
                self.system_prompt = prompt

        return SimpleNamespace(
            engine=FakeEngine(),
            cwd=str(tmp_path),
            session_id="sess123",
            current_settings=lambda: SimpleNamespace(model="gpt-5.4"),
            commands=registry,
            tool_registry=None,
            app_state=None,
            session_backend=None,
            extra_skill_dirs=(),
            extra_plugin_roots=(),
            hook_summary=lambda: "",
            mcp_summary=lambda: "",
            plugin_summary=lambda: "",
        )

    async def fake_start_runtime(bundle):
        return None

    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("ohmo.gateway.runtime.build_runtime", fake_build_runtime)
    monkeypatch.setattr("ohmo.gateway.runtime.start_runtime", fake_start_runtime)

    pool = OhmoSessionRuntimePool(cwd=tmp_path, workspace=workspace, provider_profile="codex")
    message = InboundMessage(
        channel="feishu",
        sender_id="u1",
        chat_id="c1",
        content="/memory add Profile :: prefers concise answers",
    )
    updates = [u async for u in pool.stream_message(message, "feishu:c1")]

    assert updates[-1].text == "Added memory entry profile.md"
    assert [path.name for path in list_ohmo_memory_files(workspace)] == ["profile.md"]
    assert "prefers concise answers" in (workspace / "memory" / "profile.md").read_text(encoding="utf-8")
    assert list_project_memory_files(tmp_path) == []


@pytest.mark.asyncio
async def test_runtime_pool_prompt_excludes_project_memory(tmp_path, monkeypatch):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)
    add_ohmo_memory_entry(workspace, "personal", "ohmo-only personal fact")
    monkeypatch.delenv("CLAUDE_CODE_COORDINATOR_MODE", raising=False)
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    add_project_memory_entry(tmp_path, "project", "project memory should not leak")

    async def fake_build_runtime(**kwargs):
        class FakeEngine:
            messages = []
            total_usage = UsageSnapshot()

            def set_system_prompt(self, prompt):
                self.system_prompt = prompt

        engine = FakeEngine()
        return SimpleNamespace(
            engine=engine,
            session_id="sess123",
            current_settings=lambda: Settings(system_prompt=kwargs["system_prompt"]),
            commands=create_default_command_registry(),
        )

    async def fake_start_runtime(bundle):
        return None

    monkeypatch.setattr("ohmo.gateway.runtime.build_runtime", fake_build_runtime)
    monkeypatch.setattr("ohmo.gateway.runtime.start_runtime", fake_start_runtime)

    pool = OhmoSessionRuntimePool(cwd=tmp_path, workspace=workspace, provider_profile="codex")
    bundle = await pool.get_bundle("feishu:c1", latest_user_prompt="hello")

    assert "ohmo-only personal fact" in bundle.engine.system_prompt
    assert "project memory should not leak" not in bundle.engine.system_prompt


@pytest.mark.asyncio
async def test_runtime_pool_allows_opted_in_remote_admin_commands(tmp_path, monkeypatch, caplog):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)
    save_gateway_config(
        GatewayConfig(
            provider_profile="codex",
            allow_remote_admin_commands=True,
            allowed_remote_admin_commands=["permissions"],
        ),
        workspace,
    )
    handler_called = False

    async def allowed_handler(args, context):
        nonlocal handler_called
        handler_called = True
        return CommandResult(message=f"ran with {args}")

    async def fake_build_runtime(**kwargs):
        class FakeEngine:
            messages = []
            total_usage = UsageSnapshot()

            def set_system_prompt(self, prompt):
                return None

        command = SlashCommand(
            "permissions",
            "Show or update permission mode",
            allowed_handler,
            remote_invocable=False,
        )
        command.remote_admin_opt_in = True
        return SimpleNamespace(
            engine=FakeEngine(),
            session_id="sess123",
            current_settings=lambda: SimpleNamespace(model="gpt-5.4"),
            commands=SimpleNamespace(lookup=lambda raw: (command, "full_auto")),
            hook_summary=lambda: "",
            mcp_summary=lambda: "",
            plugin_summary=lambda: "",
            cwd=str(tmp_path),
            tool_registry=None,
            app_state=None,
            session_backend=None,
            extra_skill_dirs=(),
            extra_plugin_roots=(),
        )

    async def fake_start_runtime(bundle):
        return None

    monkeypatch.setattr("ohmo.gateway.runtime.build_runtime", fake_build_runtime)
    monkeypatch.setattr("ohmo.gateway.runtime.start_runtime", fake_start_runtime)

    with caplog.at_level(logging.WARNING):
        pool = OhmoSessionRuntimePool(cwd=tmp_path, workspace=workspace, provider_profile="codex")
        message = InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="/permissions full_auto")
        updates = [u async for u in pool.stream_message(message, "feishu:c1")]

    assert handler_called is True
    assert updates[-1].kind == "final"
    assert updates[-1].text == "ran with full_auto"
    assert "remote administrative command accepted" in caplog.text


@pytest.mark.asyncio
async def test_runtime_pool_includes_media_paths_in_prompt(tmp_path, monkeypatch):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)
    image_path = tmp_path / "example.png"
    image_path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
        b"\x90wS\xde\x00\x00\x00\nIDATx\x9cc`\x00\x00\x00\x02\x00\x01"
        b"\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    report_path = tmp_path / "report.txt"
    report_path.write_text("Quarterly summary\nRevenue up 12%\n", encoding="utf-8")
    captured: dict[str, object] = {}

    async def fake_build_runtime(**kwargs):
        class FakeEngine:
            messages = []
            total_usage = UsageSnapshot()

            def set_system_prompt(self, prompt):
                return None

            async def submit_message(self, content):
                captured["content"] = content
                yield AssistantTextDelta(text="done")

        return SimpleNamespace(
            engine=FakeEngine(),
            session_id="sess123",
            current_settings=lambda: SimpleNamespace(model="gpt-5.4"),
            commands=SimpleNamespace(lookup=lambda raw: None),
        )

    async def fake_start_runtime(bundle):
        return None

    monkeypatch.setattr("ohmo.gateway.runtime.build_runtime", fake_build_runtime)
    monkeypatch.setattr("ohmo.gateway.runtime.start_runtime", fake_start_runtime)

    pool = OhmoSessionRuntimePool(cwd=tmp_path, workspace=workspace, provider_profile="codex")
    message = InboundMessage(
        channel="feishu",
        sender_id="u1",
        chat_id="c1",
        content="请看这个图片",
        media=[str(image_path), str(report_path)],
    )
    updates = [u async for u in pool.stream_message(message, "feishu:c1")]

    assert updates[-1].text == "done"
    submitted = captured["content"]
    assert isinstance(submitted, ConversationMessage)
    assert any(isinstance(block, ImageBlock) for block in submitted.content)
    text = "".join(block.text for block in submitted.content if isinstance(block, TextBlock))
    assert "[Channel attachments]" in text
    assert f"image: example.png (path: {image_path})" in text
    assert f"file: report.txt (path: {report_path})" in text
    assert "text preview: Quarterly summary Revenue up 12%" in text


@pytest.mark.asyncio
async def test_runtime_pool_retries_with_attachment_summary_when_model_rejects_images(tmp_path, monkeypatch):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)
    image_path = tmp_path / "example.png"
    image_path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
        b"\x90wS\xde\x00\x00\x00\nIDATx\x9cc`\x00\x00\x00\x02\x00\x01"
        b"\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    captured: dict[str, object] = {}

    async def fake_build_runtime(**kwargs):
        class FakeEngine:
            def __init__(self):
                self.messages = []
                self.total_usage = UsageSnapshot()
                self.max_turns = 8

            def set_system_prompt(self, prompt):
                return None

            def load_messages(self, messages):
                self.messages = list(messages)

            async def submit_message(self, content):
                self.messages.append(content)
                yield ErrorEvent(
                    message=(
                        "API error: Error code: 404 - {'error': {'message': "
                        "'No endpoints found that support image input', 'code': 404}}"
                    )
                )

            async def continue_pending(self, *, max_turns=None):
                captured["retry_messages"] = list(self.messages)
                yield AssistantTextDelta(text="done")

        return SimpleNamespace(
            engine=FakeEngine(),
            session_id="sess123",
            current_settings=lambda: SimpleNamespace(model="openrouter/text-only"),
            commands=SimpleNamespace(lookup=lambda raw: None),
        )

    async def fake_start_runtime(bundle):
        return None

    monkeypatch.setattr("ohmo.gateway.runtime.build_runtime", fake_build_runtime)
    monkeypatch.setattr("ohmo.gateway.runtime.start_runtime", fake_start_runtime)

    pool = OhmoSessionRuntimePool(cwd=tmp_path, workspace=workspace, provider_profile="openrouter")
    message = InboundMessage(
        channel="telegram",
        sender_id="u1",
        chat_id="c1",
        content="帮我看这个图片",
        media=[str(image_path)],
    )
    updates = [u async for u in pool.stream_message(message, "telegram:c1")]

    assert updates[-1].kind == "final"
    assert updates[-1].text == "done"
    assert not any(update.kind == "error" for update in updates)
    assert any(update.metadata.get("_image_fallback") for update in updates)
    retry_messages = captured["retry_messages"]
    assert all(
        not isinstance(block, ImageBlock)
        for item in retry_messages
        for block in item.content
    )
    text = "".join(
        block.text
        for item in retry_messages
        for block in item.content
        if isinstance(block, TextBlock)
    )
    assert "[Channel attachments]" in text
    assert f"image: example.png (path: {image_path})" in text


def test_runtime_pool_includes_group_speaker_context():
    built = _build_inbound_user_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_123",
            chat_id="oc_group",
            content="请帮我看一下",
            metadata={"chat_type": "group", "sender_display_name": "Tang Jiabin"},
        )
    )
    text = "".join(block.text for block in built.content if isinstance(block, TextBlock))
    assert "[Channel speaker]" in text
    assert "Tang Jiabin" in text
    assert "Sender id: ou_123" in text
    assert "请帮我看一下" in text


@pytest.mark.asyncio
async def test_gateway_bridge_publishes_progress_updates():
    bus = MessageBus()

    class FakeRuntimePool:
        async def stream_message(self, message, session_key):
            yield SimpleNamespace(kind="progress", text="🤔 想一想…", metadata={"_progress": True, "_session_key": session_key})
            yield SimpleNamespace(kind="tool_hint", text="🛠️ 正在使用 web_fetch: https://example.com", metadata={"_progress": True, "_tool_hint": True, "_session_key": session_key})
            yield SimpleNamespace(kind="final", text="Done", metadata={"_session_key": session_key})

    bridge = OhmoGatewayBridge(bus=bus, runtime_pool=FakeRuntimePool())
    task = asyncio.create_task(bridge.run())
    try:
        await bus.publish_inbound(
            InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="hi")
        )
        first = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
        second = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
        third = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
    finally:
        bridge.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert first.content.startswith(("🤔", "🧠", "✨", "🔎", "🪄"))
    assert first.metadata["_progress"] is True
    assert second.metadata["_tool_hint"] is True
    assert second.content.startswith("🛠️ ")
    assert "web_fetch" in second.content
    assert third.content == "Done"


@pytest.mark.asyncio
async def test_gateway_bridge_suppresses_dingtalk_tool_hints():
    bus = MessageBus()

    class FakeRuntimePool:
        async def stream_message(self, message, session_key):
            yield SimpleNamespace(
                kind="tool_hint",
                text="正在使用 web_fetch: https://example.com",
                metadata={"_progress": True, "_tool_hint": True, "_session_key": session_key},
            )
            yield SimpleNamespace(kind="final", text="Done", metadata={"_session_key": session_key})

    bridge = OhmoGatewayBridge(bus=bus, runtime_pool=FakeRuntimePool())
    task = asyncio.create_task(bridge.run())
    try:
        await bus.publish_inbound(
            InboundMessage(
                channel="dingtalk:dingtalk-bot",
                sender_id="u1",
                chat_id="c1",
                content="hi",
                metadata={"bot_name": "dingtalk-bot", "default_agent": "生产助手"},
            )
        )
        outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(bus.consume_outbound(), timeout=0.1)
    finally:
        bridge.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert outbound.content == "Done"
    assert not outbound.metadata.get("_tool_hint")


@pytest.mark.asyncio
async def test_gateway_bridge_logs_inbound_and_final(caplog):
    bus = MessageBus()

    class FakeRuntimePool:
        async def stream_message(self, message, session_key):
            yield SimpleNamespace(kind="progress", text="🤔 想一想…", metadata={"_progress": True, "_session_key": session_key})
            yield SimpleNamespace(kind="final", text="Done", metadata={"_session_key": session_key})

    bridge = OhmoGatewayBridge(bus=bus, runtime_pool=FakeRuntimePool())
    task = asyncio.create_task(bridge.run())
    caplog.set_level(logging.INFO)
    try:
        await bus.publish_inbound(
            InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="please translate this")
        )
        await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
        await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
    finally:
        bridge.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert "ohmo inbound received" in caplog.text
    assert "ohmo outbound final" in caplog.text
    assert "please translate this" in caplog.text


@pytest.mark.asyncio
async def test_gateway_bridge_stop_command_cancels_current_session():
    bus = MessageBus()
    cancelled = asyncio.Event()
    release = asyncio.Event()

    class FakeRuntimePool:
        async def stream_message(self, message, session_key):
            try:
                yield SimpleNamespace(kind="progress", text="🤔 想一想…", metadata={"_progress": True, "_session_key": session_key})
                await release.wait()
                yield SimpleNamespace(kind="final", text="Done", metadata={"_session_key": session_key})
            except asyncio.CancelledError:
                cancelled.set()
                raise

    bridge = OhmoGatewayBridge(bus=bus, runtime_pool=FakeRuntimePool())
    task = asyncio.create_task(bridge.run())
    try:
        await bus.publish_inbound(
            InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="long task")
        )
        first = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
        assert first.metadata["_progress"] is True
        await bus.publish_inbound(
            InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="/stop")
        )
        stopped = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
        await asyncio.wait_for(cancelled.wait(), timeout=1.0)
    finally:
        bridge.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert stopped.content == "⏹️ 已停止当前正在运行的任务。"


@pytest.mark.asyncio
async def test_gateway_bridge_restart_command_requests_gateway_restart():
    bus = MessageBus()
    restarted = asyncio.Event()
    restart_payloads: list[tuple[str, str, str]] = []

    class FakeRuntimePool:
        async def stream_message(self, message, session_key):
            if False:
                yield

    async def fake_restart(message, session_key: str) -> None:
        restart_payloads.append((message.channel, message.chat_id, session_key))
        restarted.set()

    bridge = OhmoGatewayBridge(bus=bus, runtime_pool=FakeRuntimePool(), restart_gateway=fake_restart)
    task = asyncio.create_task(bridge.run())
    try:
        await bus.publish_inbound(
            InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="/restart")
        )
        restarting = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
        await asyncio.wait_for(restarted.wait(), timeout=1.0)
    finally:
        bridge.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert restarting.content == (
        "🔄 正在重启 gateway，马上回来。\n"
        "Restarting the gateway now. I'll be back in a moment."
    )
    assert restart_payloads == [("feishu", "c1", "feishu:c1:u1")]


@pytest.mark.asyncio
async def test_gateway_bridge_dingtalk_new_dialog_resets_session_without_streaming():
    bus = MessageBus()
    reset_keys: list[str] = []
    stream_called = False

    class FakeRuntimePool:
        async def reset_session(self, session_key):
            reset_keys.append(session_key)

        async def stream_message(self, message, session_key):
            nonlocal stream_called
            stream_called = True
            if False:
                yield

    bridge = OhmoGatewayBridge(bus=bus, runtime_pool=FakeRuntimePool())
    task = asyncio.create_task(bridge.run())
    try:
        await bus.publish_inbound(
            InboundMessage(
                channel="dingtalk:ops-bot",
                sender_id="u1",
                chat_id="u1",
                content="\u65b0\u5efa\u5bf9\u8bdd",
                metadata={"default_agent": "ops-agent"},
            )
        )
        outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
    finally:
        bridge.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert reset_keys == ["dingtalk:ops-bot:ops-agent:u1:u1"]
    assert stream_called is False
    assert outbound.channel == "dingtalk:ops-bot"
    assert outbound.content == (
        "\u5df2\u65b0\u5efa\u5bf9\u8bdd\uff0c"
        "\u540e\u7eed\u6d88\u606f\u4f1a\u4ece\u7a7a\u4e0a\u4e0b\u6587\u5f00\u59cb\u3002"
    )


@pytest.mark.asyncio
async def test_runtime_pool_reset_session_deletes_latest_key_but_keeps_history(tmp_path, monkeypatch):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)
    session_key = "dingtalk:ops-bot:ops-agent:u1:u1"
    save_session_snapshot(
        cwd=tmp_path,
        workspace=workspace,
        model="gpt-5.4",
        system_prompt="system",
        messages=[ConversationMessage.from_user_text("old context")],
        usage=UsageSnapshot(),
        session_id="sess123",
        session_key=session_key,
    )

    closed: list[str] = []

    async def fake_close_runtime(bundle):
        closed.append(bundle.session_id)

    monkeypatch.setattr("ohmo.gateway.runtime.close_runtime", fake_close_runtime)

    pool = OhmoSessionRuntimePool(cwd=tmp_path, workspace=workspace, provider_profile="codex")
    pool._bundles[session_key] = SimpleNamespace(session_id="active-session")

    assert load_latest_for_session_key(workspace, session_key) is not None
    assert await pool.reset_session(session_key) is True

    assert closed == ["active-session"]
    assert load_latest_for_session_key(workspace, session_key) is None
    assert (workspace / "sessions" / "session-sess123.json").exists()


@pytest.mark.asyncio
async def test_gateway_bridge_new_dialog_text_on_non_dingtalk_is_normal_message():
    bus = MessageBus()
    seen: list[tuple[str, str]] = []

    class FakeRuntimePool:
        async def stream_message(self, message, session_key):
            seen.append((message.channel, session_key))
            yield SimpleNamespace(kind="final", text="normal", metadata={"_session_key": session_key})

    bridge = OhmoGatewayBridge(bus=bus, runtime_pool=FakeRuntimePool())
    task = asyncio.create_task(bridge.run())
    try:
        await bus.publish_inbound(
            InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="\u65b0\u5efa\u5bf9\u8bdd")
        )
        outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
    finally:
        bridge.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert seen == [("feishu", "feishu:c1:u1")]
    assert outbound.content == "normal"


@pytest.mark.asyncio
async def test_gateway_service_request_restart_waits_before_stop(monkeypatch):
    service = object.__new__(OhmoGatewayService)
    service._restart_requested = False
    service._stop_event = asyncio.Event()
    service._workspace = "/tmp/ohmo"

    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr("ohmo.gateway.service.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(
        "ohmo.gateway.service.get_gateway_restart_notice_path",
        lambda workspace: Path("/tmp/restart-notice.json"),
    )
    writes: list[str] = []
    monkeypatch.setattr(
        "pathlib.Path.write_text",
        lambda self, content, encoding=None: writes.append(content) or len(content),
    )

    message = InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="/restart")

    await OhmoGatewayService.request_restart(service, message, "feishu:c1")

    assert service._restart_requested is True
    assert service._stop_event.is_set() is True
    assert slept == [0.75]
    assert writes


@pytest.mark.asyncio
async def test_gateway_service_publishes_pending_restart_notice(tmp_path, monkeypatch):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)
    notice_path = get_gateway_restart_notice_path(workspace)
    notice_path.write_text(
        json.dumps(
            {
                "channel": "feishu",
                "chat_id": "chat-1",
                "session_key": "feishu:chat-1",
                "content": "✅ gateway 已经重新连上，可以继续了。\nGateway is back online. We can continue.",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    service = object.__new__(OhmoGatewayService)
    service._workspace = workspace
    service._bus = MessageBus()

    async def fake_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr("ohmo.gateway.service.asyncio.sleep", fake_sleep)

    await OhmoGatewayService._publish_pending_restart_notice(service)

    outbound = await asyncio.wait_for(service._bus.consume_outbound(), timeout=1.0)
    assert outbound.content == "✅ gateway 已经重新连上，可以继续了。\nGateway is back online. We can continue."
    assert outbound.chat_id == "chat-1"
    assert not notice_path.exists()


@pytest.mark.asyncio
async def test_gateway_bridge_new_message_interrupts_same_session():
    bus = MessageBus()
    first_cancelled = asyncio.Event()
    second_started = asyncio.Event()

    class FakeRuntimePool:
        async def stream_message(self, message, session_key):
            if message.content == "first":
                try:
                    yield SimpleNamespace(kind="progress", text="🤔 想一想…", metadata={"_progress": True, "_session_key": session_key})
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    first_cancelled.set()
                    raise
            else:
                second_started.set()
                yield SimpleNamespace(kind="final", text="second-done", metadata={"_session_key": session_key})

    bridge = OhmoGatewayBridge(bus=bus, runtime_pool=FakeRuntimePool())
    task = asyncio.create_task(bridge.run())
    try:
        await bus.publish_inbound(
            InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="first")
        )
        await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
        await bus.publish_inbound(
            InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="second")
        )
        interrupted = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
        final = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
        await asyncio.wait_for(first_cancelled.wait(), timeout=1.0)
        await asyncio.wait_for(second_started.wait(), timeout=1.0)
    finally:
        bridge.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert interrupted.content == "⏹️ 已停止上一条正在处理的任务，继续看你的最新消息。"
    assert final.content == "second-done"


@pytest.mark.asyncio
async def test_runtime_pool_logs_session_lifecycle(tmp_path, monkeypatch, caplog):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)

    async def fake_build_runtime(**kwargs):
        class FakeEngine:
            messages = []
            total_usage = UsageSnapshot()

            def set_system_prompt(self, prompt):
                return None

            async def submit_message(self, content):
                yield ToolExecutionStarted(tool_name="web_fetch", tool_input={"url": "https://example.com"})
                yield AssistantTextDelta(text="done")

        return SimpleNamespace(
            engine=FakeEngine(),
            session_id="sess123",
            current_settings=lambda: SimpleNamespace(model="gpt-5.4"),
            commands=SimpleNamespace(lookup=lambda raw: None),
        )

    async def fake_start_runtime(bundle):
        return None

    monkeypatch.setattr("ohmo.gateway.runtime.build_runtime", fake_build_runtime)
    monkeypatch.setattr("ohmo.gateway.runtime.start_runtime", fake_start_runtime)

    pool = OhmoSessionRuntimePool(cwd=tmp_path, workspace=workspace, provider_profile="codex")
    message = InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="check")
    caplog.set_level(logging.INFO)
    updates = [u async for u in pool.stream_message(message, "feishu:c1")]

    assert updates[-1].text == "done"
    assert "ohmo runtime processing start" in caplog.text
    assert "ohmo runtime tool start" in caplog.text
    assert "ohmo runtime saved snapshot" in caplog.text
    assert "ohmo runtime processing complete" in caplog.text


@pytest.mark.asyncio
async def test_runtime_pool_stream_message_handles_slash_command_and_refresh_runtime(tmp_path, monkeypatch):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)
    build_calls: list[dict[str, object]] = []
    close_calls: list[str] = []

    class FakeEngine:
        def __init__(self):
            self.messages = [ConversationMessage.from_user_text("before")]
            self.total_usage = UsageSnapshot()
            self.system_prompts: list[str] = []

        def set_system_prompt(self, prompt):
            self.system_prompts.append(prompt)

        async def submit_message(self, content):
            yield AssistantTextDelta(text="done")

    class FakeCommand:
        async def handler(self, args, context):
            assert args == ""
            return CommandResult(message="Permission mode set to plan", refresh_runtime=True)

    async def fake_build_runtime(**kwargs):
        build_calls.append(kwargs)
        engine = FakeEngine()
        return SimpleNamespace(
            engine=engine,
            session_id="sess123",
            current_settings=lambda: SimpleNamespace(model="gpt-5.4"),
            commands=SimpleNamespace(lookup=lambda raw: (FakeCommand(), "") if raw == "/plan" else None),
            hook_summary=lambda: "",
            mcp_summary=lambda: "",
            plugin_summary=lambda: "",
            cwd=str(tmp_path),
            tool_registry=None,
            app_state=None,
            session_backend=None,
            extra_skill_dirs=(),
            extra_plugin_roots=(),
            enforce_max_turns=False,
        )

    async def fake_start_runtime(bundle):
        return None

    async def fake_close_runtime(bundle):
        close_calls.append(bundle.session_id)

    monkeypatch.setattr("ohmo.gateway.runtime.build_runtime", fake_build_runtime)
    monkeypatch.setattr("ohmo.gateway.runtime.start_runtime", fake_start_runtime)
    monkeypatch.setattr("ohmo.gateway.runtime.close_runtime", fake_close_runtime)

    pool = OhmoSessionRuntimePool(cwd=tmp_path, workspace=workspace, provider_profile="codex")
    message = InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="/plan")
    updates = [u async for u in pool.stream_message(message, "feishu:c1")]

    assert [u.text for u in updates] == ["Permission mode set to plan"]
    assert len(build_calls) == 2
    assert close_calls == ["sess123"]
    assert build_calls[1]["restore_messages"] == [ConversationMessage.from_user_text("before").model_dump(mode="json")]


@pytest.mark.asyncio
async def test_runtime_pool_refresh_runtime_drops_dangling_tool_use_tail(tmp_path, monkeypatch):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)
    build_calls: list[dict[str, object]] = []

    class FakeEngine:
        def __init__(self):
            self.messages = [
                ConversationMessage.from_user_text("before"),
                ConversationMessage(
                    role="assistant",
                    content=[ToolUseBlock(id="write_file:234", name="write_file", input={"path": "x"})],
                ),
            ]
            self.total_usage = UsageSnapshot()

        def set_system_prompt(self, prompt):
            del prompt
            return None

        async def submit_message(self, content):
            del content
            if False:
                yield None

    class FakeCommand:
        async def handler(self, args, context):
            del args, context
            return CommandResult(message="Switched provider profile", refresh_runtime=True)

    async def fake_build_runtime(**kwargs):
        build_calls.append(kwargs)
        return SimpleNamespace(
            engine=FakeEngine(),
            session_id="sess123",
            current_settings=lambda: SimpleNamespace(model="gpt-5.4"),
            commands=SimpleNamespace(lookup=lambda raw: (FakeCommand(), "") if raw == "/provider github" else None),
            hook_summary=lambda: "",
            mcp_summary=lambda: "",
            plugin_summary=lambda: "",
            cwd=str(tmp_path),
            tool_registry=None,
            app_state=None,
            session_backend=None,
            extra_skill_dirs=(),
            extra_plugin_roots=(),
            enforce_max_turns=False,
        )

    async def fake_start_runtime(bundle):
        del bundle
        return None

    async def fake_close_runtime(bundle):
        del bundle
        return None

    monkeypatch.setattr("ohmo.gateway.runtime.build_runtime", fake_build_runtime)
    monkeypatch.setattr("ohmo.gateway.runtime.start_runtime", fake_start_runtime)
    monkeypatch.setattr("ohmo.gateway.runtime.close_runtime", fake_close_runtime)

    pool = OhmoSessionRuntimePool(cwd=tmp_path, workspace=workspace, provider_profile="codex")
    message = InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="/provider github")
    _ = [u async for u in pool.stream_message(message, "feishu:c1")]

    assert len(build_calls) == 2
    assert build_calls[1]["restore_messages"] == [ConversationMessage.from_user_text("before").model_dump(mode="json")]


@pytest.mark.asyncio
async def test_runtime_pool_stream_message_handles_plugin_command_submit_prompt(tmp_path, monkeypatch):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)
    submitted: list[object] = []

    class FakeEngine:
        messages = []
        total_usage = UsageSnapshot()
        model = "gpt-5.4"

        def set_system_prompt(self, prompt):
            return None

        def set_model(self, model):
            self.model = model

        async def submit_message(self, content):
            submitted.append(content)
            yield AssistantTextDelta(text="plugin-done")

    class FakeCommand:
        async def handler(self, args, context):
            assert args == "hello"
            return CommandResult(submit_prompt="plugin expanded prompt")

    async def fake_build_runtime(**kwargs):
        return SimpleNamespace(
            engine=FakeEngine(),
            session_id="sess123",
            current_settings=lambda: SimpleNamespace(model="gpt-5.4"),
            commands=SimpleNamespace(lookup=lambda raw: (FakeCommand(), "hello") if raw == "/plugin-cmd hello" else None),
            hook_summary=lambda: "",
            mcp_summary=lambda: "",
            plugin_summary=lambda: "",
            cwd=str(tmp_path),
            tool_registry=None,
            app_state=None,
            session_backend=None,
            extra_skill_dirs=(),
            extra_plugin_roots=(),
            enforce_max_turns=False,
        )

    async def fake_start_runtime(bundle):
        return None

    monkeypatch.setattr("ohmo.gateway.runtime.build_runtime", fake_build_runtime)
    monkeypatch.setattr("ohmo.gateway.runtime.start_runtime", fake_start_runtime)

    pool = OhmoSessionRuntimePool(cwd=tmp_path, workspace=workspace, provider_profile="codex")
    message = InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="/plugin-cmd hello")
    updates = [u async for u in pool.stream_message(message, "feishu:c1")]

    assert submitted == ["plugin expanded prompt"]
    assert updates[-1].text == "plugin-done"
