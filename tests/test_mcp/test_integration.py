"""Tests for MCP config and tool adapters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from openharness.config.settings import Settings
from openharness.mcp.config import load_mcp_server_configs
from openharness.mcp.types import McpResourceInfo, McpSseServerConfig, McpStdioServerConfig, McpToolInfo
from openharness.plugins.types import LoadedPlugin
from openharness.plugins.schemas import PluginManifest
from openharness.tools import create_default_tool_registry
from openharness.tools.base import ToolExecutionContext


@dataclass
class FakeMcpManager:
    tools: list[McpToolInfo]
    resources: list[McpResourceInfo]

    def list_tools(self):
        return self.tools

    def list_resources(self):
        return self.resources

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict):
        return f"{server_name}:{tool_name}:{arguments['name']}"

    async def read_resource(self, server_name: str, uri: str):
        return f"{server_name}:{uri}"


def test_load_mcp_server_configs_merges_plugins():
    settings = Settings(
        mcp_servers={"local": McpStdioServerConfig(command="python", args=["server.py"])}
    )
    plugin = LoadedPlugin(
        manifest=PluginManifest(name="demo", version="1.0.0"),
        path=Path("/tmp/demo"),
        enabled=True,
        mcp_servers={"remote": McpStdioServerConfig(command="python", args=["remote.py"])},
    )

    servers = load_mcp_server_configs(settings, [plugin])

    assert "local" in servers
    assert "demo:remote" in servers


def test_sse_mcp_config_validates():
    settings = Settings(
        mcp_servers={"metrics": McpSseServerConfig(url="http://192.168.6.131:8100/sse")}
    )

    servers = load_mcp_server_configs(settings, [])

    assert servers["metrics"].type == "sse"
    assert servers["metrics"].url == "http://192.168.6.131:8100/sse"


async def test_mcp_tools_are_registered():
    manager = FakeMcpManager(
        tools=[
            McpToolInfo(
                server_name="demo",
                name="hello",
                description="Say hello",
                input_schema={
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
            )
        ],
        resources=[McpResourceInfo(server_name="demo", name="Readme", uri="demo://readme")],
    )
    registry = create_default_tool_registry(manager)

    tool = registry.get("mcp__demo__hello")
    assert tool is not None
    parsed = tool.input_model.model_validate({"name": "world"})
    result = await tool.execute(parsed, ToolExecutionContext(cwd=Path(".")))
    assert result.output == "demo:hello:world"

    list_tool = registry.get("list_mcp_resources")
    assert list_tool is not None
    list_result = await list_tool.execute(list_tool.input_model(), ToolExecutionContext(cwd=Path(".")))
    assert "demo://readme" in list_result.output
