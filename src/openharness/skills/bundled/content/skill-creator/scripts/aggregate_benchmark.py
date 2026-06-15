"""Aggregate simple skill evaluation results.

Input is a directory containing JSON files with optional fields:
`name`, `score`, `passed`, and `notes`.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def aggregate(results_dir: Path) -> dict:
    rows = []
    for path in sorted(results_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        rows.append({
            "file": path.name,
            "name": data.get("name", path.stem),
            "score": data.get("score"),
            "passed": data.get("passed"),
            "notes": data.get("notes", ""),
        })

    scores = [float(row["score"]) for row in rows if isinstance(row.get("score"), (int, float))]
    return {
        "total": len(rows),
        "average_score": round(sum(scores) / len(scores), 2) if scores else None,
        "passed": sum(1 for row in rows if row.get("passed") is True),
        "failed": sum(1 for row in rows if row.get("passed") is False),
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("results_dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    summary = aggregate(args.results_dir)
    text = json.dumps(summary, indent=2, ensure_ascii=False)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
