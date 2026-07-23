"""Tool for reading skill contents."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from openharness.skills import load_skill_registry
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class SkillToolInput(BaseModel):
    """Arguments for skill lookup."""

    name: str = Field(description="Skill name")


class SkillTool(BaseTool):
    """Return the content of a loaded skill."""

    name = "skill"
    description = "Read a bundled, user, or plugin skill by name."
    input_model = SkillToolInput

    def is_read_only(self, arguments: SkillToolInput) -> bool:
        del arguments
        return True

    async def execute(self, arguments: SkillToolInput, context: ToolExecutionContext) -> ToolResult:
        registry = load_skill_registry(
            context.cwd,
            extra_skill_dirs=context.metadata.get("extra_skill_dirs"),
            extra_plugin_roots=context.metadata.get("extra_plugin_roots"),
        )
        skill = registry.get(arguments.name) or registry.get(arguments.name.lower()) or registry.get(arguments.name.title())
        if skill is None:
            return ToolResult(output=f"Skill not found: {arguments.name}", is_error=True)
        if skill.path:
            # Skills reference bundled files relative to their own directory
            # (./scripts/…, ./references/…); tell the model where that is.
            skill_dir = str(Path(skill.path).parent)
            header = (
                f"[Skill directory: {skill_dir}]\n"
                "(Relative paths in this skill such as ./scripts/… resolve against this directory.)\n\n"
            )
            return ToolResult(output=header + skill.content)
        return ToolResult(output=skill.content)
