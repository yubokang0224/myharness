"""Lightweight regression evaluation for prompt/skill changes.

Runs a fixed set of real user prompts through ``ohmo`` print mode (the same
runtime path production uses, skills included), grades each output against
simple per-case criteria, and writes per-case JSON results compatible with
the skill-creator ``aggregate_benchmark.py`` format (``name`` / ``score`` /
``passed`` / ``notes``).

Typical flow::

    ohmo eval init --output eval-cases.json     # sample case file
    ohmo eval run --cases eval-cases.json       # grade current behaviour
    # ... edit a skill or prompt ...
    ohmo eval run --cases eval-cases.json --baseline <previous run dir>

The baseline comparison highlights regressions (cases that flipped from
passed to failed) so a skill edit can be validated in one command.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ohmo.workspace import get_workspace_root

_DEFAULT_TIMEOUT = 300


@dataclass
class EvalCase:
    name: str
    prompt: str
    must_contain: list[str]
    must_not_contain: list[str]
    regex: str | None = None
    timeout_seconds: int = _DEFAULT_TIMEOUT

    @staticmethod
    def from_dict(data: dict[str, Any], index: int) -> "EvalCase":
        prompt = str(data.get("prompt") or "").strip()
        if not prompt:
            raise ValueError(f"case #{index}: 'prompt' is required")
        return EvalCase(
            name=str(data.get("name") or f"case-{index}"),
            prompt=prompt,
            must_contain=[str(item) for item in data.get("must_contain") or []],
            must_not_contain=[str(item) for item in data.get("must_not_contain") or []],
            regex=data.get("regex"),
            timeout_seconds=int(data.get("timeout_seconds") or _DEFAULT_TIMEOUT),
        )


def load_cases(path: Path) -> list[EvalCase]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("cases") or []
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{path}: expected a non-empty JSON list of cases")
    return [EvalCase.from_dict(item, index) for index, item in enumerate(raw, start=1)]


def run_case(
    case: EvalCase,
    *,
    workspace: str | Path | None,
    cwd: str | None,
    model: str | None = None,
) -> dict[str, Any]:
    """Execute one case via ohmo print mode and grade the output."""
    command = [sys.executable, "-m", "ohmo", "--print", case.prompt]
    if workspace:
        command += ["--workspace", str(workspace)]
    if cwd:
        command += ["--cwd", cwd]
    if model:
        command += ["--model", model]

    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=case.timeout_seconds,
        )
        output = (completed.stdout or "").strip()
        run_error = None if completed.returncode == 0 else (
            f"exit={completed.returncode} stderr={(completed.stderr or '')[-400:]}"
        )
    except subprocess.TimeoutExpired:
        output = ""
        run_error = f"timeout after {case.timeout_seconds}s"
    duration_s = round(time.monotonic() - started, 1)

    checks: list[tuple[str, bool]] = []
    for needle in case.must_contain:
        checks.append((f"包含「{needle}」", needle in output))
    for needle in case.must_not_contain:
        checks.append((f"不包含「{needle}」", needle not in output))
    if case.regex:
        checks.append((f"匹配 /{case.regex}/", re.search(case.regex, output) is not None))
    if not checks:
        checks.append(("有非空输出", bool(output)))

    satisfied = sum(1 for _, ok in checks if ok)
    passed = run_error is None and satisfied == len(checks)
    score = round(5 * satisfied / len(checks), 1) if run_error is None else 0.0
    failed_checks = [label for label, ok in checks if not ok]
    notes = run_error or ("; ".join(f"未通过: {label}" for label in failed_checks) or "全部通过")

    return {
        "name": case.name,
        "score": score,
        "passed": passed,
        "notes": notes,
        "duration_s": duration_s,
        "prompt": case.prompt,
        "output": output[-4000:],
    }


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Same summary shape as skill-creator's aggregate_benchmark.py."""
    scores = [float(row["score"]) for row in rows if isinstance(row.get("score"), (int, float))]
    return {
        "total": len(rows),
        "average_score": round(sum(scores) / len(scores), 2) if scores else None,
        "passed": sum(1 for row in rows if row.get("passed") is True),
        "failed": sum(1 for row in rows if row.get("passed") is False),
        "rows": [
            {key: row.get(key) for key in ("name", "score", "passed", "notes", "duration_s")}
            for row in rows
        ],
    }


def compare_with_baseline(summary: dict[str, Any], baseline_summary: dict[str, Any]) -> list[str]:
    """Return human-readable regression/improvement lines."""
    baseline_by_name = {row.get("name"): row for row in baseline_summary.get("rows", [])}
    lines: list[str] = []
    for row in summary.get("rows", []):
        name = row.get("name")
        base = baseline_by_name.get(name)
        if base is None:
            lines.append(f"[新增] {name}: passed={row.get('passed')}")
            continue
        if base.get("passed") is True and row.get("passed") is False:
            lines.append(f"[回归] {name}: 之前通过，现在失败 — {row.get('notes')}")
        elif base.get("passed") is False and row.get("passed") is True:
            lines.append(f"[修复] {name}: 之前失败，现在通过")
    return lines


def eval_output_dir(workspace: str | Path | None, label: str) -> Path:
    path = get_workspace_root(workspace) / "eval" / label
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_eval(
    cases_path: Path,
    *,
    workspace: str | Path | None = None,
    cwd: str | None = None,
    model: str | None = None,
    label: str | None = None,
    baseline: Path | None = None,
) -> tuple[Path, dict[str, Any], list[str]]:
    """Run all cases, persist results, and compare with a baseline run if given."""
    cases = load_cases(cases_path)
    run_label = label or time.strftime("%Y%m%d-%H%M%S")
    out_dir = eval_output_dir(workspace, run_label)

    rows = []
    for index, case in enumerate(cases, start=1):
        print(f"[{index}/{len(cases)}] {case.name} ...", flush=True)
        row = run_case(case, workspace=workspace, cwd=cwd, model=model)
        rows.append(row)
        (out_dir / f"{case.name}.json").write_text(
            json.dumps(row, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        status = "PASS" if row["passed"] else "FAIL"
        print(f"    {status} score={row['score']} {row['notes']}", flush=True)

    summary = aggregate(rows)
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    diff_lines: list[str] = []
    if baseline is not None:
        baseline_file = baseline / "summary.json" if baseline.is_dir() else baseline
        try:
            baseline_summary = json.loads(baseline_file.read_text(encoding="utf-8"))
            diff_lines = compare_with_baseline(summary, baseline_summary)
        except (OSError, json.JSONDecodeError) as exc:
            diff_lines = [f"[基线读取失败] {baseline_file}: {exc}"]
    return out_dir, summary, diff_lines


SAMPLE_CASES = [
    {
        "name": "delivery-note-basic",
        "prompt": "帮我查一下最近的送货单识别功能是否正常，回答里给出结论。",
        "must_contain": ["送货单"],
        "must_not_contain": ["无法回答"],
        "timeout_seconds": 300,
    },
    {
        "name": "greeting-scope",
        "prompt": "你好，你能做什么？",
        "must_contain": [],
        "must_not_contain": ["error", "Traceback"],
    },
]

# Inbound texts that are commands/noise rather than real questions.
_SKIP_PROMPT_PATTERNS = re.compile(r"^/|^新建对话$|^\[attachment message\]$")


def _sanitize_case_name(text: str, index: int) -> str:
    cleaned = re.sub(r"[^\w一-鿿]+", "-", text.strip())[:24].strip("-")
    return f"{index:02d}-{cleaned}" if cleaned else f"case-{index:02d}"


def extract_cases_from_sessions(
    workspace: str | Path | None = None,
    *,
    days: int = 30,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Build eval cases from real user prompts found in persisted records.

    Grading criteria are intentionally left empty (falls back to the
    "non-empty output" check); tighten ``must_contain`` per case by hand for
    the scenarios that matter.
    """
    import json as _json

    from ohmo.workspace import get_invocations_dir, get_sessions_dir

    cutoff = time.time() - days * 86400
    candidates: list[tuple[float, str]] = []

    def _collect(directory: Path, prefix: str, extractor) -> None:
        if not directory.exists():
            return
        for path in directory.glob(f"{prefix}-*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    continue
                data = _json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                extractor(data)

    def _from_session(data: dict) -> None:
        created_at = float(data.get("created_at") or 0.0)
        for message in data.get("messages") or []:
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = str(block.get("text") or "").strip()
                    if text:
                        candidates.append((created_at, text))

    def _from_invocation(data: dict) -> None:
        created_at = float(data.get("created_at") or 0.0)
        text = str(data.get("request_content") or "").strip()
        if text:
            candidates.append((created_at, text))

    _collect(get_sessions_dir(workspace), "session", _from_session)
    _collect(get_invocations_dir(workspace), "invocation", _from_invocation)

    candidates.sort(key=lambda item: item[0], reverse=True)
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _, text in candidates:
        normalized = " ".join(text.split())
        if len(normalized) < 6 or len(normalized) > 500:
            continue
        if _SKIP_PROMPT_PATTERNS.search(normalized):
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        cases.append(
            {
                "name": _sanitize_case_name(normalized, len(cases) + 1),
                "prompt": text,
                "must_contain": [],
                "must_not_contain": ["Traceback", "gateway error"],
                "timeout_seconds": _DEFAULT_TIMEOUT,
            }
        )
        if len(cases) >= limit:
            break
    return cases
