"""Bundled skill definitions loaded from .md files."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from openharness.skills.types import SkillDefinition

_CONTENT_DIR = Path(__file__).parent / "content"
logger = logging.getLogger(__name__)


def get_bundled_skills() -> list[SkillDefinition]:
    """Load all bundled skills from the content/ directory."""
    skills: list[SkillDefinition] = []
    if not _CONTENT_DIR.exists():
        return skills
    for path in sorted(_CONTENT_DIR.glob("*.md")):
        skills.append(_load_bundled_skill_file(path, default_name=path.stem))
    for path in sorted(_CONTENT_DIR.iterdir()):
        skill_file = path / "SKILL.md"
        if path.is_dir() and skill_file.exists():
            skills.append(_load_bundled_skill_file(skill_file, default_name=path.name))
    return skills


def _load_bundled_skill_file(path: Path, *, default_name: str) -> SkillDefinition:
    content = path.read_text(encoding="utf-8")
    name, description = _parse_frontmatter(default_name, content)
    return SkillDefinition(
        name=name,
        description=description,
        content=content,
        source="bundled",
        path=str(path),
    )


def _parse_frontmatter(default_name: str, content: str) -> tuple[str, str]:
    """Extract name and description from a skill markdown file.

    Supports YAML frontmatter (``---`` delimited) and falls back to heading/paragraph parsing.
    """
    name = default_name
    description = ""
    lines = content.splitlines()

    # Try YAML frontmatter first.
    if content.startswith("---\n"):
        end_index = content.find("\n---\n", 4)
        if end_index != -1:
            try:
                metadata = yaml.safe_load(content[4:end_index])
                if isinstance(metadata, dict):
                    val = metadata.get("name")
                    if isinstance(val, str) and val.strip():
                        name = val.strip()
                    val = metadata.get("description")
                    if isinstance(val, str) and val.strip():
                        description = val.strip()
            except yaml.YAMLError:
                logger.debug("Failed to parse bundled skill frontmatter for %s", default_name)
        if description:
            return name, description

    # Fallback: heading + first paragraph
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# "):
            name = stripped[2:].strip() or default_name
            continue
        if stripped and not stripped.startswith("---") and not stripped.startswith("#"):
            description = stripped[:200]
            break
    return name, description or f"Bundled skill: {name}"
