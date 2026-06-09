"""Shared agent tool-policy helpers for gateway runtimes and HTTP routes."""

from __future__ import annotations

from typing import Any


_TOOL_NAME_ALIASES = {
    "bash": {"bash"},
    "read": {"read_file", "file_read"},
    "readfile": {"read_file", "file_read"},
    "fileread": {"read_file", "file_read"},
    "write": {"write_file", "file_write"},
    "writefile": {"write_file", "file_write"},
    "filewrite": {"write_file", "file_write"},
    "edit": {"edit_file", "file_edit"},
    "editfile": {"edit_file", "file_edit"},
    "fileedit": {"edit_file", "file_edit"},
    "multiedit": {"edit_file", "file_edit"},
    "glob": {"glob"},
    "grep": {"grep"},
    "webfetch": {"web_fetch"},
    "websearch": {"web_search"},
    "agent": {"agent"},
    "sendmessage": {"send_message"},
    "todowrite": {"todo_write"},
    "task": {"task_create", "task_get", "task_list", "task_stop", "task_output", "task_update"},
}


def _normalize_tool_name(name: str) -> str:
    return name.replace("-", "_").strip().lower()


def expand_tool_names(names: list[str] | None) -> set[str] | None:
    """Expand configured aliases; ``None`` or ``*`` means all tools."""
    if names is None:
        return None
    expanded: set[str] = set()
    for raw in names:
        value = str(raw).strip()
        if not value:
            continue
        if value == "*":
            return None
        normalized = _normalize_tool_name(value)
        expanded.add(normalized)
        expanded.update(_TOOL_NAME_ALIASES.get(normalized.replace("_", ""), set()))
    return expanded


def apply_agent_tool_policy(tool_registry: Any, agent_def: Any | None) -> None:
    """Filter a tool registry according to an agent definition."""
    if agent_def is None:
        return
    tools = getattr(tool_registry, "_tools", None)
    if not isinstance(tools, dict):
        return

    allowed = expand_tool_names(getattr(agent_def, "tools", None))
    if allowed is not None:
        for name in list(tools):
            if name not in allowed:
                tools.pop(name, None)

    disallowed = expand_tool_names(getattr(agent_def, "disallowed_tools", None))
    for name in disallowed or set():
        tools.pop(name, None)
