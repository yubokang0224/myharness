"""Failure-signal aggregation and the weekly "建议修订清单" report.

Scans persisted invocation records and session snapshots for failure
signals — invocation errors, tool errors, user corrective replies — then
aggregates them into a markdown review list for a human to act on.

Deliberately offline: nothing here touches the live message pipeline or
injects anything into prompts.  Run it via ``ohmo failures report`` (cron /
systemd timer for the weekly cadence).
"""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ohmo.session_storage import get_invocation_dir, get_session_dir
from ohmo.workspace import get_workspace_root

# Heuristics for a user telling the assistant its previous answer was wrong.
_USER_CORRECTION_PATTERNS = re.compile(
    r"不对|错了|搞错|识别错|弄错|不是这样|不正确|重新(试|来|识别|生成|算)"
    r"|答非所问|没听懂|理解错|结果不对|数据不对|(?<![a-zA-Z])wrong|incorrect",
)

# Noise normalisation so the same error groups together despite ids/numbers.
_SIGNATURE_NOISE = [
    (re.compile(r"[0-9a-f]{8,}", re.IGNORECASE), "<hex>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]?[\d:.]*"), "<time>"),
    (re.compile(r"\d+"), "<n>"),
    (re.compile(r"\s+"), " "),
]


@dataclass
class FailureSignal:
    kind: str  # invocation_error | tool_error | user_correction
    occurred_at: float
    agent_name: str | None
    channel: str
    detail: str
    tool_name: str | None = None
    trace_id: str | None = None
    source: str = ""  # record file / session id for locating the sample


@dataclass
class FailureGroup:
    kind: str
    agent_name: str | None
    tool_name: str | None
    signature: str
    signals: list[FailureSignal] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.signals)


def _signature(text: str) -> str:
    normalized = (text or "").strip()
    for pattern, replacement in _SIGNATURE_NOISE:
        normalized = pattern.sub(replacement, normalized)
    return normalized[:120] or "<empty>"


def _iter_json_files(directory: Path, prefix: str, cutoff: float) -> list[tuple[Path, dict]]:
    items: list[tuple[Path, dict]] = []
    if not directory.exists():
        return items
    for path in directory.glob(f"{prefix}-*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict):
            items.append((path, data))
    return items


def _tool_name_for_result(messages: list[dict], tool_use_id: str) -> str | None:
    for message in messages:
        for block in message.get("content", []) or []:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("id") == tool_use_id
            ):
                return str(block.get("name") or "") or None
    return None


def _message_text(message: dict) -> str:
    parts = []
    for block in message.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "\n".join(parts).strip()


def _signals_from_messages(
    messages: list[dict],
    *,
    occurred_at: float,
    agent_name: str | None,
    channel: str,
    trace_id: str | None,
    source: str,
) -> list[FailureSignal]:
    signals: list[FailureSignal] = []
    saw_assistant_reply = False
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "assistant":
            saw_assistant_reply = True
        for block in message.get("content", []) or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result" and block.get("is_error"):
                tool_name = _tool_name_for_result(messages, str(block.get("tool_use_id") or ""))
                signals.append(
                    FailureSignal(
                        kind="tool_error",
                        occurred_at=occurred_at,
                        agent_name=agent_name,
                        channel=channel,
                        detail=str(block.get("content") or "")[:500],
                        tool_name=tool_name,
                        trace_id=trace_id,
                        source=source,
                    )
                )
        if role == "user" and saw_assistant_reply:
            text = _message_text(message)
            if text and len(text) <= 200 and _USER_CORRECTION_PATTERNS.search(text):
                signals.append(
                    FailureSignal(
                        kind="user_correction",
                        occurred_at=occurred_at,
                        agent_name=agent_name,
                        channel=channel,
                        detail=text[:300],
                        trace_id=trace_id,
                        source=source,
                    )
                )
    return signals


def scan_failures(workspace: str | Path | None = None, *, days: int = 7) -> list[FailureSignal]:
    """Collect failure signals from invocation records and session snapshots."""
    cutoff = time.time() - days * 86400
    signals: list[FailureSignal] = []

    for path, record in _iter_json_files(get_invocation_dir(workspace), "invocation", cutoff):
        occurred_at = float(record.get("created_at") or path.stat().st_mtime)
        if occurred_at < cutoff:
            continue
        agent_name = record.get("agent_name")
        channel = str(record.get("channel") or "api")
        trace_id = record.get("trace_id")
        source = path.name
        status = str(record.get("status") or "completed")
        if status in ("error", "failed") or record.get("error"):
            signals.append(
                FailureSignal(
                    kind="invocation_error",
                    occurred_at=occurred_at,
                    agent_name=agent_name,
                    channel=channel,
                    detail=str(record.get("error") or status)[:500],
                    trace_id=trace_id,
                    source=source,
                )
            )
        for call in record.get("tool_calls") or []:
            if isinstance(call, dict) and call.get("is_error"):
                signals.append(
                    FailureSignal(
                        kind="tool_error",
                        occurred_at=occurred_at,
                        agent_name=agent_name,
                        channel=channel,
                        detail=str(call.get("output") or "")[:500],
                        tool_name=str(call.get("tool_name") or "") or None,
                        trace_id=trace_id,
                        source=source,
                    )
                )
        messages = record.get("messages") if isinstance(record.get("messages"), list) else []
        signals.extend(
            _signals_from_messages(
                messages,
                occurred_at=occurred_at,
                agent_name=agent_name,
                channel=channel,
                trace_id=trace_id,
                source=source,
            )
        )

    for path, snapshot in _iter_json_files(get_session_dir(workspace), "session", cutoff):
        occurred_at = float(snapshot.get("created_at") or path.stat().st_mtime)
        if occurred_at < cutoff:
            continue
        messages = snapshot.get("messages") if isinstance(snapshot.get("messages"), list) else []
        signals.extend(
            _signals_from_messages(
                messages,
                occurred_at=occurred_at,
                agent_name=snapshot.get("agent_name"),
                channel=str(snapshot.get("channel") or "web"),
                trace_id=None,
                source=path.name,
            )
        )

    signals.sort(key=lambda item: item.occurred_at, reverse=True)
    return signals


def group_failures(signals: list[FailureSignal]) -> list[FailureGroup]:
    grouped: dict[tuple, FailureGroup] = {}
    for signal in signals:
        key = (signal.kind, signal.agent_name, signal.tool_name, _signature(signal.detail))
        group = grouped.get(key)
        if group is None:
            group = FailureGroup(
                kind=signal.kind,
                agent_name=signal.agent_name,
                tool_name=signal.tool_name,
                signature=key[3],
            )
            grouped[key] = group
        group.signals.append(signal)
    return sorted(grouped.values(), key=lambda g: g.count, reverse=True)


_KIND_LABEL = {
    "invocation_error": "调用失败",
    "tool_error": "工具报错",
    "user_correction": "用户纠错",
}

_KIND_ADVICE = {
    "invocation_error": "按错误内容分类处理：认证/配额问题走运维，业务性报错检查对应技能的输入约束。",
    "tool_error": "检查该工具的输入约束与失败样本；若是技能内调用，考虑在技能文档中补充参数说明或前置校验。",
    "user_correction": "复核该场景的 prompt / 技能定义是否覆盖此类输入；确认是否需要在 SKILL.md 中补充规则或示例。",
}


def render_report(
    signals: list[FailureSignal],
    *,
    days: int,
    max_groups: int = 30,
    max_samples: int = 3,
) -> str:
    """Render the aggregated failure signals as a markdown review list."""
    groups = group_failures(signals)
    today = datetime.now().strftime("%Y-%m-%d")
    kind_totals: dict[str, int] = defaultdict(int)
    for signal in signals:
        kind_totals[signal.kind] += 1

    lines = [
        f"# 智能体失败信号周报（建议修订清单） {today}",
        "",
        f"统计窗口：最近 {days} 天；失败信号共 **{len(signals)}** 条，聚合为 **{len(groups)}** 组。",
        "",
        "| 类型 | 条数 |",
        "| --- | --- |",
    ]
    for kind, label in _KIND_LABEL.items():
        lines.append(f"| {label} | {kind_totals.get(kind, 0)} |")
    lines.append("")

    if not groups:
        lines.append("窗口内未发现失败信号。")
        return "\n".join(lines) + "\n"

    lines.append("## 建议修订清单（按频次排序）")
    lines.append("")
    for index, group in enumerate(groups[:max_groups], start=1):
        scope = group.agent_name or "默认"
        tool_suffix = f" / 工具 `{group.tool_name}`" if group.tool_name else ""
        lines.append(
            f"### {index}. [{_KIND_LABEL.get(group.kind, group.kind)}] "
            f"{scope}{tool_suffix} — {group.count} 次"
        )
        lines.append("")
        lines.append(f"- **特征**：{group.signature}")
        lines.append(f"- **建议**：{_KIND_ADVICE.get(group.kind, '人工复核。')}")
        lines.append("- **样本**：")
        for signal in group.signals[:max_samples]:
            when = datetime.fromtimestamp(signal.occurred_at).strftime("%m-%d %H:%M")
            trace = f" trace={signal.trace_id}" if signal.trace_id else ""
            lines.append(
                f"  - {when} [{signal.channel}]{trace} {signal.detail[:200]}（{signal.source}）"
            )
        lines.append("")
    if len(groups) > max_groups:
        lines.append(f"> 另有 {len(groups) - max_groups} 组低频信号未列出，可用 --max-groups 调整。")
        lines.append("")
    return "\n".join(lines) + "\n"


def get_reports_dir(workspace: str | Path | None = None) -> Path:
    reports = get_workspace_root(workspace) / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    return reports


def write_report(
    workspace: str | Path | None = None,
    *,
    days: int = 7,
    max_groups: int = 30,
) -> tuple[Path, str, int]:
    """Scan, render, and persist the report. Returns (path, markdown, signal count)."""
    signals = scan_failures(workspace, days=days)
    markdown = render_report(signals, days=days, max_groups=max_groups)
    today = datetime.now().strftime("%Y-%m-%d")
    path = get_reports_dir(workspace) / f"failure-report-{today}.md"
    path.write_text(markdown, encoding="utf-8")
    return path, markdown, len(signals)
