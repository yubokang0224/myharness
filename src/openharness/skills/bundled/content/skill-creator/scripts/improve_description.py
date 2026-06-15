"""Suggest a stronger skill description from example prompts."""

from __future__ import annotations

import argparse
import re
from collections import Counter
from pathlib import Path

import yaml

WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{2,}")
STOP = {
    "the", "and", "for", "with", "that", "this", "from", "into", "when",
    "use", "using", "please", "help", "create", "make", "write",
}


def _frontmatter(skill_file: Path) -> dict:
    content = skill_file.read_text(encoding="utf-8")
    end = content.find("\n---\n", 4) if content.startswith("---\n") else -1
    if end == -1:
        return {}
    data = yaml.safe_load(content[4:end]) or {}
    return data if isinstance(data, dict) else {}


def suggest(skill_file: Path, prompts_file: Path) -> str:
    metadata = _frontmatter(skill_file)
    name = str(metadata.get("name") or skill_file.parent.name)
    current = str(metadata.get("description") or "")
    prompts = prompts_file.read_text(encoding="utf-8")
    words = [
        word.lower()
        for word in WORD_RE.findall(prompts)
        if word.lower() not in STOP
    ]
    common = [word for word, _ in Counter(words).most_common(12)]
    trigger = ", ".join(common[:8]) or "the target workflow"
    return (
        f"{current.strip()}\n\n"
        f"Suggested trigger coverage for '{name}': include user wording around "
        f"{trigger}. Keep the final description under 120 words and mention both "
        "the artifact being created and the contexts that should activate the skill."
    ).strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("skill_file", type=Path)
    parser.add_argument("prompts_file", type=Path)
    args = parser.parse_args()
    print(suggest(args.skill_file, args.prompts_file))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
