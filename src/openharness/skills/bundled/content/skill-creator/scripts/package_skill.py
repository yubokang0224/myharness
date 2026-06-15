"""Package a directory skill into a zip archive."""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

SKIP_NAMES = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def package_skill(skill_dir: Path, output: Path | None = None) -> Path:
    skill_dir = skill_dir.resolve()
    if not (skill_dir / "SKILL.md").exists():
        raise FileNotFoundError(f"missing SKILL.md in {skill_dir}")
    output = output or skill_dir.with_suffix(".zip")
    output.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(skill_dir.rglob("*")):
            if any(part in SKIP_NAMES for part in path.parts):
                continue
            if path.is_file():
                archive.write(path, path.relative_to(skill_dir.parent))
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("skill_dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    try:
        output = package_skill(args.skill_dir, args.output)
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
