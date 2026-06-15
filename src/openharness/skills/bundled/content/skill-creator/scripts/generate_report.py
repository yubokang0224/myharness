"""Generate an HTML report from aggregate_benchmark.py output."""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path


def generate(summary_file: Path, template_file: Path, output_file: Path) -> None:
    summary = json.loads(summary_file.read_text(encoding="utf-8"))
    rows = summary.get("rows", [])
    table_rows = []
    for row in rows:
        table_rows.append(
            "<tr>"
            f"<td>{html.escape(str(row.get('name', '')))}</td>"
            f"<td>{html.escape(str(row.get('score', '')))}</td>"
            f"<td>{html.escape(str(row.get('passed', '')))}</td>"
            f"<td>{html.escape(str(row.get('notes', '')))}</td>"
            "</tr>"
        )
    table = (
        "<table><thead><tr><th>Name</th><th>Score</th><th>Passed</th><th>Notes</th></tr></thead>"
        f"<tbody>{''.join(table_rows)}</tbody></table>"
    )
    template = template_file.read_text(encoding="utf-8")
    output = (
        template.replace("{{TOTAL}}", str(summary.get("total", 0)))
        .replace("{{AVERAGE_SCORE}}", str(summary.get("average_score", "")))
        .replace("{{ROWS}}", table)
    )
    output_file.write_text(output, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("summary_file", type=Path)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    generate(args.summary_file, args.template, args.output)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
