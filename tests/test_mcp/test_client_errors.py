"""Tests for MCP client error handling on disconnected servers."""

from __future__ import annotations

from pathlib import Path
import asyncio
from contextlib import AsyncExitStack
from unittest.mock import AsyncMock, MagicMock

import pytest

from openharness.mcp.client import McpClientManager, McpServerNotConnectedError
from openharness.mcp.types import McpConnectionStatus, McpSseServerConfig, McpStdioServerConfig, McpToolInfo
from openharness.tools.base import ToolExecutionContext
from openharness.tools.mcp_tool import McpToolAdapter
from openharness.tools.read_mcp_resource_tool import ReadMcpResourceTool


# --- McpClientManager.call_tool ---


class FakeAsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_call_tool_raises_when_server_never_connected():
    manager = McpClientManager({})
    with pytest.raises(McpServerNotConnectedError, match="not connected"):
        await manager.call_tool("missing", "some_tool", {})


@pytest.mark.asyncio
async def test_call_tool_raises_when_server_failed_to_connect():
    config = McpStdioServerConfig(command="false", args=[])
    manager = McpClientManager({"bad": config})
    manager._statuses["bad"] = McpConnectionStatus(
        name="bad", state="failed", detail="Connection refused",
    )
    with pytest.raises(McpServerNotConnectedError, match="Connection refused"):
        await manager.call_tool("bad", "tool", {})


@pytest.mark.asyncio
async def test_call_tool_raises_when_session_errors():
    manager = McpClientManager({})
    mock_session = AsyncMock()
    mock_session.call_tool.side_effect = RuntimeError("transport closed")
    manager._sessions["flaky"] = mock_session

    with pytest.raises(McpServerNotConnectedError, match="transport closed"):
        await manager.call_tool("flaky", "tool", {})


@pytest.mark.asyncio
async def test_call_tool_includes_unknown_server_detail_for_unconfigured():
    """When the server name is not even in _statuses, detail says 'unknown server'."""
    manager = McpClientManager({})
    with pytest.raises(McpServerNotConnectedError, match="unknown server"):
        await manager.call_tool("ghost", "tool", {})


# --- McpClientManager.read_resource ---


@pytest.mark.asyncio
async def test_read_resource_raises_when_server_never_connected():
    manager = McpClientManager({})
    with pytest.raises(McpServerNotConnectedError, match="not connected"):
        await manager.read_resource("missing", "res://data")


@pytest.mark.asyncio
async def test_read_resource_raises_when_session_errors():
    manager = McpClientManager({})
    mock_session = AsyncMock()
    mock_session.read_resource.side_effect = OSError("broken pipe")
    manager._sessions["flaky"] = mock_session

    with pytest.raises(McpServerNotConnectedError, match="broken pipe"):
        await manager.read_resource("flaky", "res://data")


@pytest.mark.asyncio
async def test_register_connected_session_tolerates_missing_resources_list():
    manager = McpClientManager({})
    session = AsyncMock()
    session.initialize.return_value = None
    session.list_tools.return_value.tools = []
    session.list_resources.side_effect = RuntimeError("Method not found")
    stack = AsyncExitStack()
    await stack.__aenter__()
    stack.enter_async_context = AsyncMock(return_value=session)

    await manager._register_connected_session(
        name="context7",
        config=McpStdioServerConfig(command="npx", args=[]),
        stack=stack,
        read_stream=object(),
        write_stream=object(),
        auth_configured=False,
    )

    assert manager._statuses["context7"].state == "connected"
    assert manager._statuses["context7"].resources == []


@pytest.mark.asyncio
async def test_connect_sse_uses_sse_client_and_registers_session(monkeypatch):
    import openharness.mcp.client as client_module

    calls = []

    def fake_sse_client(url, headers=None):
        calls.append((url, headers))
        return FakeAsyncContext(("read-stream", "write-stream"))

    async def fake_register(**kwargs):
        manager._statuses[kwargs["name"]] = McpConnectionStatus(
            name=kwargs["name"],
            state="connected",
            transport=kwargs["config"].type,
            auth_configured=kwargs["auth_configured"],
            tools=[McpToolInfo(server_name=kwargs["name"], name="query_metric_nl", description="", input_schema={})],
        )

    monkeypatch.setattr(client_module, "sse_client", fake_sse_client)
    manager = McpClientManager(
        {
            "metrics": McpSseServerConfig(
                url="http://192.168.6.131:8100/sse",
                headers={"Authorization": "Bearer token"},
            )
        }
    )
    monkeypatch.setattr(manager, "_register_connected_session", fake_register)

    await manager.connect_all()

    assert calls == [("http://192.168.6.131:8100/sse", {"Authorization": "Bearer token"})]
    status = manager.list_statuses()[0]
    assert status.state == "connected"
    assert status.transport == "sse"
    assert status.tools[0].name == "query_metric_nl"


@pytest.mark.asyncio
async def test_close_suppresses_known_runtime_error_from_stdio_cleanup():
    manager = McpClientManager({})
    stack = MagicMock()
    stack.aclose = AsyncMock(side_effect=RuntimeError("Attempted to exit cancel scope in a different task than it was entered in"))
    manager._stacks["context7"] = stack
    manager._sessions["context7"] = AsyncMock()

    await manager.close()

    assert manager._stacks == {}
    assert manager._sessions == {}


@pytest.mark.asyncio
async def test_close_suppresses_cancelled_error_from_stdio_cleanup():
    manager = McpClientManager({})
    stack = MagicMock()
    stack.aclose = AsyncMock(side_effect=asyncio.CancelledError())
    manager._stacks["context7"] = stack
    manager._sessions["context7"] = AsyncMock()

    await manager.close()

    assert manager._stacks == {}
    assert manager._sessions == {}


# --- McpToolAdapter catches error and returns ToolResult(is_error=True) ---


@pytest.mark.asyncio
async def test_mcp_tool_adapter_returns_error_result_on_disconnected_server():
    manager = McpClientManager({})
    tool_info = McpToolInfo(
        server_name="gone",
        name="hello",
        description="test",
        input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
    )
    adapter = McpToolAdapter(manager, tool_info)
    result = await adapter.execute(
        adapter.input_model.model_validate({"x": "1"}),
        ToolExecutionContext(cwd=Path(".")),
    )
    assert result.is_error is True
    assert "not connected" in result.output


@pytest.mark.asyncio
async def test_mcp_tool_adapter_returns_error_result_on_timeout(monkeypatch):
    class SlowManager:
        async def call_tool(self, server_name, tool_name, arguments):
            await asyncio.sleep(1)
            return "late"

    import openharness.tools.mcp_tool as mcp_tool_module

    monkeypatch.setattr(mcp_tool_module, "_MCP_TOOL_TIMEOUT_SECONDS", 0.01)
    tool_info = McpToolInfo(
        server_name="metrics",
        name="query_metric_nl",
        description="test",
        input_schema={"type": "object", "properties": {"user_query": {"type": "string"}}},
    )
    adapter = McpToolAdapter(SlowManager(), tool_info)
    result = await adapter.execute(
        adapter.input_model.model_validate({"user_query": "OEE"}),
        ToolExecutionContext(cwd=Path(".")),
    )

    assert result.is_error is True
    assert "timed out" in result.output
    assert "metrics.query_metric_nl" in result.output


# --- ReadMcpResourceTool catches error and returns ToolResult(is_error=True) ---


@pytest.mark.asyncio
async def test_read_mcp_resource_tool_returns_error_result_on_disconnected_server():
    manager = McpClientManager({})
    tool = ReadMcpResourceTool(manager)
    result = await tool.execute(
        tool.input_model.model_validate({"server": "gone", "uri": "res://x"}),
        ToolExecutionContext(cwd=Path(".")),
    )
    assert result.is_error is True
    assert "not connected" in result.output
