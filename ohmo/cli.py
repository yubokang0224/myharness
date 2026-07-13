"""CLI entry point for the ohmo personal-agent app."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import typer

from openharness.auth.manager import AuthManager
from openharness.config import load_settings

from ohmo.gateway.config import load_gateway_config, save_gateway_config
from ohmo.gateway.models import GatewayConfig
from ohmo.gateway.service import (
    OhmoGatewayService,
    gateway_status,
    start_gateway_process,
    stop_gateway_process,
)
from ohmo.memory import add_memory_entry, list_memory_files, remove_memory_entry
from ohmo.runtime import launch_ohmo_react_tui, run_ohmo_backend, run_ohmo_print_mode
from ohmo.session_storage import OhmoSessionBackend
from ohmo.workspace import (
    get_gateway_config_path,
    get_workspace_root,
    get_soul_path,
    get_state_path,
    get_user_path,
    initialize_workspace,
    workspace_health,
)


app = typer.Typer(
    name="ohmo",
    help="ohmo: a personal-agent app built on top of OpenHarness.",
    invoke_without_command=True,
    add_completion=False,
)
memory_app = typer.Typer(name="memory", help="Manage .ohmo memory")
soul_app = typer.Typer(name="soul", help="Inspect or edit soul.md")
user_app = typer.Typer(name="user", help="Inspect or edit user.md")
gateway_app = typer.Typer(name="gateway", help="Run the ohmo gateway")
failures_app = typer.Typer(name="failures", help="Aggregate failure signals into review reports")
eval_app = typer.Typer(name="eval", help="Regression evaluation for prompt/skill changes")

app.add_typer(memory_app)
app.add_typer(soul_app)
app.add_typer(user_app)
app.add_typer(gateway_app)
app.add_typer(failures_app)
app.add_typer(eval_app)

_INTERACTIVE_CHANNELS = ("telegram", "slack", "discord", "feishu")
_WORKSPACE_HELP = "Path to the ohmo workspace (defaults to ~/.ohmo)"


def _can_use_questionary() -> bool:
    """Return True when a real interactive terminal is available."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    if sys.stdin is not sys.__stdin__ or sys.stdout is not sys.__stdout__:
        return False
    try:
        import questionary  # noqa: F401
    except ImportError:
        return False
    return True


def _select_with_questionary(
    title: str,
    options: list[tuple[str, str]],
    *,
    default_value: str | None = None,
) -> str:
    import questionary

    choices = [
        questionary.Choice(
            title=label,
            value=value,
            checked=(value == default_value),
        )
        for value, label in options
    ]
    result = questionary.select(title, choices=choices, default=default_value).ask()
    if result is None:
        raise typer.Abort()
    return str(result)


def _confirm_prompt(message: str, *, default: bool = False) -> bool:
    """Ask for confirmation, preferring questionary in a real TTY."""
    if _can_use_questionary():
        import questionary

        result = questionary.confirm(message, default=default).ask()
        if result is None:
            raise typer.Abort()
        return bool(result)
    return typer.confirm(message, default=default)


def _text_prompt(message: str, *, default: str = "") -> str:
    """Prompt for text input, preferring questionary in a real TTY."""
    if _can_use_questionary():
        import questionary

        result = questionary.text(message, default=default).ask()
        if result is None:
            raise typer.Abort()
        return str(result)
    return typer.prompt(message, default=default)


def _select_from_menu(
    title: str,
    options: list[tuple[str, str]],
    *,
    default_value: str | None = None,
) -> str:
    """Render a simple numbered picker and return the selected value."""
    if _can_use_questionary():
        return _select_with_questionary(title, options, default_value=default_value)
    print(title)
    default_index = 1
    for index, (value, label) in enumerate(options, 1):
        marker = " (default)" if value == default_value else ""
        if value == default_value:
            default_index = index
        print(f"  {index}. {label}{marker}")
    raw = typer.prompt("Choose", default=str(default_index))
    try:
        selected = options[int(raw) - 1]
    except (ValueError, IndexError):
        raise typer.BadParameter(f"Invalid selection: {raw}") from None
    return selected[0]


def _format_provider_profile_label(info: dict[str, object]) -> str:
    label = str(info["label"])
    if bool(info["configured"]):
        return label
    return f"{label} (missing)"


def _prompt_provider_profile(workspace: str | Path) -> str:
    settings = load_settings()
    statuses = AuthManager(settings).get_profile_statuses()
    default_value = load_gateway_config(workspace).provider_profile
    hints = {
        "claude-api": ("Claude / Kimi / GLM / MiniMax", "fg:#7aa2f7"),
        "openai-compatible": ("OpenAI / OpenRouter", "fg:#9ece6a"),
    }

    if _can_use_questionary():
        import questionary

        choices = []
        for name, info in statuses.items():
            label = str(info["label"])
            missing = "" if bool(info["configured"]) else " (missing)"
            hint = hints.get(name)
            if hint is None:
                title = label if not missing else [("", label), ("fg:#d3869b", missing)]
            else:
                hint_text, hint_style = hint
                title = [
                    ("", f"{label}  "),
                    (hint_style, hint_text),
                ]
                if missing:
                    title.extend([("", "  "), ("fg:#d3869b", missing.strip())])
            choices.append(questionary.Choice(title=title, value=name, checked=(name == default_value)))
        result = questionary.select("Choose provider profile for ohmo:", choices=choices, default=default_value).ask()
        if result is None:
            raise typer.Abort()
        return str(result)

    options = []
    for name, info in statuses.items():
        label = _format_provider_profile_label(info)
        hint = hints.get(name)
        if hint is not None:
            label = f"{label} ({hint[0]})"
        options.append((name, label))
    return _select_from_menu(
        "Choose provider profile for ohmo:",
        options,
        default_value=default_value,
    )


def _prompt_channels(existing: GatewayConfig) -> tuple[list[str], dict[str, dict]]:
    enabled: list[str] = []
    configs: dict[str, dict] = {}
    print("Configure channels for ohmo gateway:")
    for channel in _INTERACTIVE_CHANNELS:
        current = channel in existing.enabled_channels
        prior = dict(existing.channel_configs.get(channel, {}))
        if current:
            enabled.append(channel)
            if not _confirm_prompt(f"Reconfigure {channel}?", default=False):
                configs[channel] = prior
                continue
        elif not _confirm_prompt(f"Enable {channel}?", default=False):
            continue
        else:
            enabled.append(channel)
        allow_from_raw = _text_prompt(
            f"{channel} allow_from (comma separated user/chat IDs; leave blank to deny all; '*' for everyone)",
            default=",".join(prior.get("allow_from", [])),
        )
        allow_from = [item.strip() for item in allow_from_raw.split(",") if item.strip()]
        config: dict[str, object] = {"allow_from": allow_from}
        if channel == "telegram":
            config["token"] = _text_prompt(
                "Telegram bot token",
                default=str(prior.get("token", "")),
            )
            config["reply_to_message"] = _confirm_prompt(
                "Reply to the original Telegram message?",
                default=bool(prior.get("reply_to_message", True)),
            )
        elif channel == "slack":
            config["bot_token"] = _text_prompt(
                "Slack bot token",
                default=str(prior.get("bot_token", "")),
            )
            config["app_token"] = _text_prompt(
                "Slack app token",
                default=str(prior.get("app_token", "")),
            )
            config["mode"] = "socket"
            config["reply_in_thread"] = _confirm_prompt(
                "Reply in thread?",
                default=bool(prior.get("reply_in_thread", True)),
            )
            config["group_policy"] = _select_from_menu(
                "Slack group policy:",
                [
                    ("mention", "Mention only"),
                    ("open", "Always reply in channels"),
                    ("allowlist", "Only allow configured channels"),
                ],
                default_value=str(prior.get("group_policy", "mention")),
            )
        elif channel == "discord":
            config["token"] = _text_prompt(
                "Discord bot token",
                default=str(prior.get("token", "")),
            )
            config["gateway_url"] = _text_prompt(
                "Discord gateway URL",
                default=str(prior.get("gateway_url", "wss://gateway.discord.gg/?v=10&encoding=json")),
            )
            config["intents"] = int(
                _text_prompt(
                    "Discord intents bitmask",
                    default=str(prior.get("intents", 513)),
                )
            )
            config["group_policy"] = _select_from_menu(
                "Discord group policy:",
                [
                    ("mention", "Mention only"),
                    ("open", "Always reply in channels"),
                ],
                default_value=str(prior.get("group_policy", "mention")),
            )
        elif channel == "feishu":
            config["app_id"] = _text_prompt(
                "Feishu app id",
                default=str(prior.get("app_id", "")),
            )
            config["app_secret"] = _text_prompt(
                "Feishu app secret",
                default=str(prior.get("app_secret", "")),
            )
            config["encrypt_key"] = _text_prompt(
                "Feishu encrypt key",
                default=str(prior.get("encrypt_key", "")),
            )
            config["verification_token"] = _text_prompt(
                "Feishu verification token",
                default=str(prior.get("verification_token", "")),
            )
            config["react_emoji"] = _text_prompt(
                "Feishu reaction emoji",
                default=str(prior.get("react_emoji", "OK")),
            )
        configs[channel] = config
    return enabled, configs


def _run_gateway_config_wizard(workspace: str | Path) -> GatewayConfig:
    """Interactive flow for provider/channel setup."""
    existing = load_gateway_config(workspace)
    provider_profile = _prompt_provider_profile(workspace)
    enabled_channels, channel_configs = _prompt_channels(existing)
    send_progress = _confirm_prompt(
        "Send progress updates to channels?",
        default=existing.send_progress,
    )
    send_tool_hints = _confirm_prompt(
        "Send tool hints to channels?",
        default=existing.send_tool_hints,
    )
    allow_remote_admin_commands = _confirm_prompt(
        "Allow explicitly listed administrative slash commands from remote channels?",
        default=existing.allow_remote_admin_commands,
    )
    default_allowlist = ", ".join(existing.allowed_remote_admin_commands)
    allowed_remote_admin_commands: list[str] = []
    if allow_remote_admin_commands:
        allowlist_raw = _text_prompt(
            "Allowed remote admin commands (comma-separated, e.g. permissions, plan)",
            default=default_allowlist,
        )
        allowed_remote_admin_commands = [
            item.strip().lstrip("/")
            for item in allowlist_raw.split(",")
            if item.strip()
        ]
    config = existing.model_copy(
        update={
            "provider_profile": provider_profile,
            "enabled_channels": enabled_channels,
            "channel_configs": channel_configs,
            "send_progress": send_progress,
            "send_tool_hints": send_tool_hints,
            "allow_remote_admin_commands": allow_remote_admin_commands,
            "allowed_remote_admin_commands": allowed_remote_admin_commands,
        }
    )
    save_gateway_config(config, workspace)
    return config


def _print_gateway_config_summary(config: GatewayConfig) -> None:
    if config.enabled_channels:
        print(
            "Configured channels: "
            + ", ".join(config.enabled_channels)
            + f" | provider_profile={config.provider_profile}"
        )
        deny_all_channels = [
            name for name in config.enabled_channels
            if not list(config.channel_configs.get(name, {}).get("allow_from", []))
        ]
        if deny_all_channels:
            print(
                "Remote access denied until allow_from is configured for: "
                + ", ".join(deny_all_channels)
            )
    else:
        print(f"Configured provider_profile={config.provider_profile}; no channels enabled yet.")
    if config.allow_remote_admin_commands and config.allowed_remote_admin_commands:
        print(
            "Remote admin opt-in enabled for: "
            + ", ".join(f"/{name}" for name in config.allowed_remote_admin_commands)
        )
    else:
        print("Remote admin commands remain local-only.")


def _maybe_restart_gateway(*, cwd: str | Path, workspace: str | Path) -> None:
    state = gateway_status(cwd, workspace)
    if not state.running:
        return
    if not _confirm_prompt("Gateway is running. Restart now to apply changes?", default=True):
        print("Configuration saved. Restart later with `ohmo gateway restart`.")
        return
    stop_gateway_process(cwd, workspace)
    pid = start_gateway_process(cwd, workspace)
    print(f"ohmo gateway restarted (pid={pid})")


def _configure_gateway_logging(workspace: str | Path | None = None) -> None:
    """Configure foreground gateway logging (console + JSONL file)."""
    from ohmo.logging_setup import configure_process_logging

    config = load_gateway_config(workspace)
    level_name = str(config.log_level or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    configure_process_logging("gateway", workspace=workspace, level=level)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    print_mode: str | None = typer.Option(None, "--print", "-p", help="Run a single prompt and exit"),
    model: str | None = typer.Option(None, "--model", help="Model override for this session"),
    profile: str | None = typer.Option(None, "--profile", help="Provider profile to use"),
    workspace: str | None = typer.Option(None, "--workspace", help=_WORKSPACE_HELP),
    max_turns: int | None = typer.Option(None, "--max-turns", help="Override max turns"),
    cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Working directory"),
    backend_only: bool = typer.Option(False, "--backend-only", hidden=True),
    resume: str | None = typer.Option(None, "--resume", help="Resume an ohmo session by id"),
    continue_session: bool = typer.Option(False, "--continue", help="Continue the latest ohmo session"),
) -> None:
    """Launch the ohmo app or invoke a subcommand."""
    if ctx.invoked_subcommand is not None:
        return

    cwd_path = str(Path(cwd).resolve())
    workspace_root = initialize_workspace(workspace)
    backend = OhmoSessionBackend(workspace_root)
    restore_messages = None
    restore_tool_metadata = None
    if continue_session:
        latest = backend.load_latest(cwd_path)
        if latest is None:
            print("No previous ohmo session found in this directory.", file=sys.stderr)
            raise typer.Exit(1)
        restore_messages = latest.get("messages")
        restore_tool_metadata = latest.get("tool_metadata")
    elif resume:
        snapshot = backend.load_by_id(cwd_path, resume)
        if snapshot is None:
            print(f"ohmo session not found: {resume}", file=sys.stderr)
            raise typer.Exit(1)
        restore_messages = snapshot.get("messages")
        restore_tool_metadata = snapshot.get("tool_metadata")

    if backend_only:
        raise SystemExit(
            asyncio.run(
                run_ohmo_backend(
                    cwd=cwd_path,
                    workspace=workspace_root,
                    model=model,
                    max_turns=max_turns,
                    provider_profile=profile,
                    restore_messages=restore_messages,
                    restore_tool_metadata=restore_tool_metadata,
                )
            )
        )

    if print_mode is not None:
        raise SystemExit(
            asyncio.run(
                run_ohmo_print_mode(
                    prompt=print_mode,
                    cwd=cwd_path,
                    workspace=workspace_root,
                    model=model,
                    max_turns=max_turns,
                    provider_profile=profile,
                )
            )
        )

    raise SystemExit(
        asyncio.run(
            launch_ohmo_react_tui(
                cwd=cwd_path,
                workspace=workspace_root,
                model=model,
                max_turns=max_turns,
                provider_profile=profile,
            )
        )
    )


@app.command("init")
def init_cmd(
    cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project working directory (reserved for future project overrides)"),
    workspace: str | None = typer.Option(None, "--workspace", help=_WORKSPACE_HELP),
    interactive: bool = typer.Option(
        True,
        "--interactive/--no-interactive",
        help="Run the provider/channel setup wizard when attached to a terminal",
    ),
) -> None:
    """Initialize the .ohmo workspace."""
    root_path = get_workspace_root(workspace)
    already_exists = root_path.exists()
    root = initialize_workspace(root_path)
    print(f"Initialized ohmo workspace at {root}")
    if already_exists:
        print("ohmo workspace already exists.")
        if not interactive:
            print("Use `ohmo config` to update provider and channel settings.")
            return
        if not _confirm_prompt("Open configuration now?", default=True):
            print("Use `ohmo config` when you want to change provider or channel settings.")
            return
    if interactive:
        config = _run_gateway_config_wizard(root)
        _print_gateway_config_summary(config)
        print(f"Saved gateway config to {get_gateway_config_path(root)}")


@app.command("config")
def config_cmd(
    cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project working directory"),
    workspace: str | None = typer.Option(None, "--workspace", help=_WORKSPACE_HELP),
) -> None:
    """Configure provider profile and gateway channels."""
    cwd_path = str(Path(cwd).resolve())
    workspace_root = initialize_workspace(workspace)
    config = _run_gateway_config_wizard(workspace_root)
    _print_gateway_config_summary(config)
    print(f"Saved gateway config to {get_gateway_config_path(workspace_root)}")
    _maybe_restart_gateway(cwd=cwd_path, workspace=workspace_root)


@app.command("doctor")
def doctor_cmd(
    cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project working directory"),
    workspace: str | None = typer.Option(None, "--workspace", help=_WORKSPACE_HELP),
) -> None:
    """Check .ohmo workspace and provider readiness."""
    cwd_path = str(Path(cwd).resolve())
    workspace_root = initialize_workspace(workspace)
    health = workspace_health(workspace_root)
    settings = load_settings()
    statuses = AuthManager(settings).get_profile_statuses()
    lines = ["ohmo doctor:"]
    for name, ok in health.items():
        lines.append(f"- {name}: {'ok' if ok else 'missing'}")
    lines.append(f"- project_cwd: {cwd_path}")
    lines.append(f"- workspace_root: {workspace_root}")
    lines.append(f"- workspace_state: {get_state_path(workspace_root)}")
    lines.append(f"- gateway_config: {get_gateway_config_path(workspace_root)}")
    lines.append("- available_profiles:")
    for name, info in statuses.items():
        lines.append(
            f"  - {name}: {info['label']} ({'configured' if info['configured'] else 'missing auth'})"
        )
    print("\n".join(lines))


@memory_app.command("list")
def memory_list_cmd(workspace: str | None = typer.Option(None, "--workspace", help=_WORKSPACE_HELP)) -> None:
    for path in list_memory_files(workspace):
        print(path.name)


@memory_app.command("add")
def memory_add_cmd(
    title: str = typer.Argument(...),
    content: str = typer.Argument(...),
    workspace: str | None = typer.Option(None, "--workspace", help=_WORKSPACE_HELP),
) -> None:
    path = add_memory_entry(workspace, title, content)
    print(f"Added memory entry {path.name}")


@memory_app.command("remove")
def memory_remove_cmd(
    name: str = typer.Argument(...),
    workspace: str | None = typer.Option(None, "--workspace", help=_WORKSPACE_HELP),
) -> None:
    if remove_memory_entry(workspace, name):
        print(f"Removed memory entry {name}")
        return
    print(f"Memory entry not found: {name}", file=sys.stderr)
    raise typer.Exit(1)


def _show_or_edit(path: Path, set_text: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if set_text is not None:
        path.write_text(set_text.strip() + "\n", encoding="utf-8")
        print(f"Updated {path}")
        return
    if not path.exists():
        print(f"{path} does not exist yet.", file=sys.stderr)
        raise typer.Exit(1)
    print(path.read_text(encoding="utf-8"))


@soul_app.command("show")
def soul_show_cmd(workspace: str | None = typer.Option(None, "--workspace", help=_WORKSPACE_HELP)) -> None:
    _show_or_edit(get_soul_path(workspace), None)


@soul_app.command("edit")
def soul_edit_cmd(
    workspace: str | None = typer.Option(None, "--workspace", help=_WORKSPACE_HELP),
    set_text: str | None = typer.Option(None, "--set", help="Replace soul.md with this text"),
) -> None:
    _show_or_edit(get_soul_path(workspace), set_text)


@user_app.command("show")
def user_show_cmd(workspace: str | None = typer.Option(None, "--workspace", help=_WORKSPACE_HELP)) -> None:
    _show_or_edit(get_user_path(workspace), None)


@user_app.command("edit")
def user_edit_cmd(
    workspace: str | None = typer.Option(None, "--workspace", help=_WORKSPACE_HELP),
    set_text: str | None = typer.Option(None, "--set", help="Replace user.md with this text"),
) -> None:
    _show_or_edit(get_user_path(workspace), set_text)


@gateway_app.command("run")
def gateway_run_cmd(
    cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project working directory"),
    workspace: str | None = typer.Option(None, "--workspace", help=_WORKSPACE_HELP),
) -> None:
    """Run the ohmo gateway in the foreground."""
    _configure_gateway_logging(workspace)
    service = OhmoGatewayService(cwd, workspace)
    raise SystemExit(asyncio.run(service.run_foreground()))


@gateway_app.command("start")
def gateway_start_cmd(
    cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project working directory"),
    workspace: str | None = typer.Option(None, "--workspace", help=_WORKSPACE_HELP),
) -> None:
    pid = start_gateway_process(cwd, workspace)
    print(f"ohmo gateway started (pid={pid})")


@gateway_app.command("stop")
def gateway_stop_cmd(
    cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project working directory"),
    workspace: str | None = typer.Option(None, "--workspace", help=_WORKSPACE_HELP),
) -> None:
    if stop_gateway_process(cwd, workspace):
        print("ohmo gateway stopped.")
        return
    print("ohmo gateway is not running.")


@gateway_app.command("restart")
def gateway_restart_cmd(
    cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project working directory"),
    workspace: str | None = typer.Option(None, "--workspace", help=_WORKSPACE_HELP),
) -> None:
    stop_gateway_process(cwd, workspace)
    pid = start_gateway_process(cwd, workspace)
    print(f"ohmo gateway restarted (pid={pid})")


@gateway_app.command("status")
def gateway_status_cmd(
    cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project working directory"),
    workspace: str | None = typer.Option(None, "--workspace", help=_WORKSPACE_HELP),
) -> None:
    state = gateway_status(cwd, workspace)
    print(state.model_dump_json(indent=2))


@failures_app.command("report")
def failures_report_cmd(
    days: int = typer.Option(7, "--days", help="Look-back window in days"),
    workspace: str | None = typer.Option(None, "--workspace", help=_WORKSPACE_HELP),
    email: bool = typer.Option(False, "--email", help="Email the report via alerting SMTP config"),
    max_groups: int = typer.Option(30, "--max-groups", help="Maximum groups listed in the report"),
) -> None:
    """Scan invocation/session records for failure signals and write the review report."""
    from ohmo.failure_report import write_report

    path, markdown, count = write_report(workspace, days=days, max_groups=max_groups)
    print(f"失败信号 {count} 条，报告已生成: {path}")
    if email:
        from datetime import datetime

        from ohmo.alerting import send_email

        alerting = load_gateway_config(workspace).alerting
        subject = f"[ohmo周报] 智能体失败信号建议修订清单 {datetime.now().strftime('%Y-%m-%d')}"
        ok = send_email(
            alerting,
            subject=subject,
            body=markdown,
            attachments=[(path.name, markdown.encode("utf-8"))],
        )
        print("邮件已发送。" if ok else "邮件发送失败（检查 gateway.json alerting SMTP 配置）。")


@eval_app.command("init")
def eval_init_cmd(
    output: str = typer.Option("eval-cases.json", "--output", help="Where to write the case file"),
    from_sessions: bool = typer.Option(
        False, "--from-sessions", help="Extract real user prompts from persisted session/invocation records"
    ),
    workspace: str | None = typer.Option(None, "--workspace", help=_WORKSPACE_HELP),
    days: int = typer.Option(30, "--days", help="Look-back window when using --from-sessions"),
    limit: int = typer.Option(20, "--limit", help="Max cases when using --from-sessions"),
) -> None:
    """Write an evaluation case file (sample, or extracted from real history)."""
    import json as _json

    from ohmo.eval_runner import SAMPLE_CASES, extract_cases_from_sessions

    path = Path(output)
    if path.exists():
        print(f"{path} 已存在，不覆盖。")
        raise typer.Exit(1)
    if from_sessions:
        cases = extract_cases_from_sessions(workspace, days=days, limit=limit)
        if not cases:
            print(f"最近 {days} 天的会话/调用记录里没有可用的用户问题，改用 `ohmo eval init` 生成样例后手工填写。")
            raise typer.Exit(1)
        path.write_text(_json.dumps(cases, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"已从历史记录抽取 {len(cases)} 条真实用户问题写入: {path}")
        print("默认只校验“有非空输出且不报错”；对重要场景补充 must_contain 判分标准。")
    else:
        path.write_text(_json.dumps(SAMPLE_CASES, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"样例用例已写入: {path}（把 prompt 换成真实用户问题；或用 --from-sessions 从会话记录自动抽取）")


@eval_app.command("run")
def eval_run_cmd(
    cases: str = typer.Option(..., "--cases", help="JSON case file (see `ohmo eval init`)"),
    workspace: str | None = typer.Option(None, "--workspace", help=_WORKSPACE_HELP),
    cwd: str | None = typer.Option(None, "--cwd", help="Working directory for the agent run"),
    model: str | None = typer.Option(None, "--model", help="Model override"),
    label: str | None = typer.Option(None, "--label", help="Run label (defaults to timestamp)"),
    baseline: str | None = typer.Option(
        None, "--baseline", help="Previous run dir (or its summary.json) to diff against"
    ),
) -> None:
    """Run all cases through ohmo print mode and grade the outputs."""
    from ohmo.eval_runner import run_eval

    out_dir, summary, diff_lines = run_eval(
        Path(cases),
        workspace=workspace,
        cwd=cwd,
        model=model,
        label=label,
        baseline=Path(baseline) if baseline else None,
    )
    print(
        f"\n共 {summary['total']} 例：通过 {summary['passed']}，失败 {summary['failed']}，"
        f"平均分 {summary['average_score']}"
    )
    print(f"结果目录: {out_dir}")
    for line in diff_lines:
        print(line)
    if any(line.startswith("[回归]") for line in diff_lines) or summary["failed"] > 0:
        raise typer.Exit(1)


@app.command("serve")
def serve_cmd(
    host: str = typer.Option("0.0.0.0", "--host", help="Bind host"),
    port: int = typer.Option(8000, "--port", help="Bind port"),
    workspace: str | None = typer.Option(None, "--workspace", help=_WORKSPACE_HELP),
    reload: bool = typer.Option(False, "--reload", help="Enable hot-reload (development only)"),
    cors_origins: list[str] = typer.Option([], "--cors-origin", help="Allowed CORS origins (repeatable)"),
) -> None:
    """Start the OpenHarness Agent HTTP API server (FastAPI + uvicorn)."""
    try:
        import uvicorn
    except ImportError:
        print("uvicorn is not installed. Run: pip install 'uvicorn[standard]'")
        raise typer.Exit(1)

    from ohmo.gateway.api import create_app
    from ohmo.logging_setup import configure_process_logging

    effective_workspace = workspace
    if effective_workspace:
        effective_workspace = str(Path(effective_workspace).resolve())

    config = load_gateway_config(effective_workspace)
    level = getattr(logging, str(config.log_level or "INFO").upper(), logging.INFO)
    configure_process_logging("agent-api", workspace=effective_workspace, level=level)

    api_app = create_app(
        cors_origins=cors_origins or None,
        workspace=effective_workspace,
    )

    print(f"Starting OpenHarness Agent API on http://{host}:{port}")
    print(f"  OpenAPI docs: http://{host}:{port}/docs")

    uvicorn.run(
        api_app,
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )
