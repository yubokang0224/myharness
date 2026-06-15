"""Validate an OpenHarness-compatible skill.

Usage:
    python quick_validate.py path/to/skill-dir
    python quick_validate.py path/to/bundled-skill.md --flat
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def _split_frontmatter(content: str) -> tuple[dict, str]:
    if not content.startswith("---\n"):
        raise ValueError("SKILL.md must start with YAML frontmatter")
    end = content.find("\n---\n", 4)
    if end == -1:
        raise ValueError("YAML frontmatter must end with a line containing ---")
    data = yaml.safe_load(content[4:end]) or {}
    if not isinstance(data, dict):
        raise ValueError("YAML frontmatter must be a mapping")
    return data, content[end + len("\n---\n") :]


def validate(path: Path, *, flat: bool = False) -> list[str]:
    errors: list[str] = []
    skill_file = path if flat else path / "SKILL.md"
    if not skill_file.exists():
        return [f"missing skill file: {skill_file}"]

    try:
        content = skill_file.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return [f"not valid UTF-8: {skill_file}"]

    try:
        metadata, body = _split_frontmatter(content)
    except Exception as exc:
        return [str(exc)]

    name = metadata.get("name")
    description = metadata.get("description")
    if not isinstance(name, str) or not name.strip():
        errors.append("frontmatter.name must be a non-empty string")
    elif not NAME_RE.match(name.strip()):
        errors.append("frontmatter.name must use lowercase letters, digits, and hyphens")
    elif not flat and path.name != name.strip():
        errors.append(f"directory name '{path.name}' does not match frontmatter.name '{name.strip()}'")

    if not isinstance(description, str) or not description.strip():
        errors.append("frontmatter.description must be a non-empty string")
    elif len(description.strip()) < 25:
        errors.append("frontmatter.description is too short to be a useful trigger")

    if not body.strip():
        errors.append("skill body is empty")

    for resource_dir in ("scripts", "references", "agents", "assets"):
        candidate = path / resource_dir
        if not flat and candidate.exists() and not candidate.is_dir():
            errors.append(f"{resource_dir} exists but is not a directory")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--flat", action="store_true", help="validate a single bundled .md skill file")
    args = parser.parse_args()

    errors = validate(args.path, flat=args.flat)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(f"OK: {args.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
