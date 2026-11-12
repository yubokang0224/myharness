"""Gateway service lifecycle for ohmo."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import os.path
import signal
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

if sys.platform == "win32":
    import ctypes

from openharness.channels.bus.events import OutboundMessage
from openharness.channels.bus.queue import MessageBus
from openharness.channels.impl.manager import ChannelManager
from openharness.config import load_settings

from ohmo.gateway.board_image_recognition_worker import BoardImageRecognitionWorker
from ohmo.gateway.bridge import OhmoGatewayBridge
from ohmo.gateway.config import build_channel_manager_config, load_gateway_config
from ohmo.gateway.models import GatewayState
from ohmo.gateway.runtime import OhmoSessionRuntimePool
from ohmo.workspace import (
    get_gateway_config_path,
    get_gateway_restart_notice_path,
    get_logs_dir,
    get_state_path,
    get_workspace_root,
    initialize_workspace,
)

logger = logging.getLogger(__name__)
_REPO_ROOT = Path(__file__).resolve().parents[2]


class OhmoGatewayService:
    """Foreground/background service wrapper for the personal gateway."""

    def __init__(self, cwd: str | Path | None = None, workspace: str | Path | None = None) -> None:
        self._cwd = str(Path(cwd or Path.cwd()).resolve())
        self._workspace = workspace
        os.chdir(self._cwd)
        root = initialize_workspace(self._workspace)
        os.environ["OHMO_WORKSPACE"] = str(root)
        self._config = load_gateway_config(self._workspace)
        if self._config.allow_remote_admin_commands and self._config.allowed_remote_admin_commands:
            logger.warning(
                "ohmo gateway remote administrative commands enabled commands=%s",
                ",".join(self._config.allowed_remote_admin_commands),
            )
        self._bus = MessageBus()
        self._runtime_pool = OhmoSessionRuntimePool(
            cwd=self._cwd,
            workspace=self._workspace,
            provider_profile=self._config.provider_profile,
        )
        self._stop_event: asyncio.Event | None = None
        self._restart_requested = False
        self._bridge = OhmoGatewayBridge(
            bus=self._bus,
            runtime_pool=self._runtime_pool,
            restart_gateway=self.request_restart,
        )
        self._manager = ChannelManager(build_channel_manager_config(self._config), self._bus)
        self._manager_task: asyncio.Task[None] | None = None
        self._last_config_sig = self._channel_config_signature(self._config)

    @staticmethod
    def _channel_config_signature(config: object) -> str:
        return json.dumps(
            {
                "enabled_channels": getattr(config, "enabled_channels", []),
                "channel_configs": getattr(config, "channel_configs", {}),
            },
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )

    async def _watch_config_loop(self) -> None:
        """监听 gateway.json 变更并自动热重载消息通道（扫码绑定或切换智能体后立即生效）。"""
        config_path = get_gateway_config_path(self._workspace)
        last_mtime = config_path.stat().st_mtime_ns if config_path.exists() else 0
        while True:
            await asyncio.sleep(2.0)
            try:
                if not config_path.exists():
                    continue
                mtime = config_path.stat().st_mtime_ns
                if mtime == last_mtime:
                    continue
                last_mtime = mtime
                new_config = load_gateway_config(self._workspace)
                new_sig = self._channel_config_signature(new_config)
                if new_sig == self._last_config_sig:
                    continue
                logger.info(
                    "ohmo gateway detected channel configuration update in gateway.json; hot-reloading channels..."
                )
                self._last_config_sig = new_sig
                self._config = new_config
                self._runtime_pool._gateway_config = new_config
                old_task = self._manager_task
                old_manager = self._manager
                if old_task is not None and not old_task.done():
                    old_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await old_task
                await old_manager.stop_all()
                self._manager = ChannelManager(build_channel_manager_config(self._config), self._bus)
                self._manager_task = asyncio.create_task(
                    self._manager.start_all(), name="ohmo-gateway-channels"
                )
                self.write_state(running=True)
                logger.info(
                    "ohmo gateway hot-reloaded channels successfully enabled=%s",
                    self._config.enabled_channels,
                )
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("ohmo gateway failed to hot-reload gateway.json")

    @property
    def pid_file(self) -> Path:
        return get_workspace_root(self._workspace) / "gateway.pid"

    @property
    def log_file(self) -> Path:
        return get_logs_dir(self._workspace) / "gateway.log"

    @property
    def state_file(self) -> Path:
        return get_state_path(self._workspace)

    def write_state(self, *, running: bool, last_error: str | None = None) -> None:
        state = GatewayState(
            running=running,
            pid=os.getpid() if running else None,
            active_sessions=self._runtime_pool.active_sessions,
            provider_profile=self._config.provider_profile,
            enabled_channels=self._config.enabled_channels,
            last_error=last_error,
        )
        self.state_file.write_text(state.model_dump_json(indent=2) + "\n", encoding="utf-8")

    async def request_restart(self, message, session_key: str) -> None:
        """Ask the foreground gateway loop to restart itself."""
        restart_notice = {
            "channel": message.channel,
            "chat_id": message.chat_id,
            "session_key": session_key,
            "content": "✅ gateway 已经重新连上，可以继续了。\nGateway is back online. We can continue.",
        }
        get_gateway_restart_notice_path(self._workspace).write_text(
            json.dumps(restart_notice, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self._restart_requested = True
        # Let the outbound dispatcher flush the restart notice to the IM channel
        # before we tear down the bridge and channel connections.
        await asyncio.sleep(0.75)
        if self._stop_event is not None:
            self._stop_event.set()

    def _exec_restart(self) -> None:
        root = str(get_workspace_root(self._workspace))
        argv = [
            sys.executable,
            "-m",
            "ohmo",
            "gateway",
            "run",
            "--cwd",
            self._cwd,
            "--workspace",
            root,
        ]
        logger.info("ohmo gateway restarting in-place argv=%s", argv)
        os.execv(sys.executable, argv)

    async def _publish_pending_restart_notice(self) -> None:
        path = get_gateway_restart_notice_path(self._workspace)
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            channel = payload.get("channel")
            chat_id = payload.get("chat_id")
            content = payload.get("content")
            session_key = payload.get("session_key")
            if not isinstance(channel, str) or not isinstance(chat_id, str) or not isinstance(content, str):
                return
            await asyncio.sleep(2.0)
            await self._bus.publish_outbound(
                OutboundMessage(
                    channel=channel,
                    chat_id=chat_id,
                    content=content,
                    metadata={"_session_key": session_key} if isinstance(session_key, str) else {},
                )
            )
            logger.info(
                "ohmo gateway published restart confirmation channel=%s chat_id=%s session_key=%s",
                channel,
                chat_id,
                session_key,
            )
        finally:
            path.unlink(missing_ok=True)

    async def run_foreground(self) -> int:
        board_recognition_worker: BoardImageRecognitionWorker | None = None
        if self._config.board_recognition.enabled:
            settings = load_settings()
            _validate_board_recognition_settings(settings.internal_api)
            board_recognition_worker = BoardImageRecognitionWorker(
                config=self._config.board_recognition,
                upstream_base_url=settings.internal_api.base_url,
                dingtalk_token=settings.internal_api.dingtalk_token,
                workspace=self._workspace,
                runtime_pool=self._runtime_pool,
                bus=self._bus,
            )
        self.pid_file.write_text(str(os.getpid()), encoding="utf-8")
        self.write_state(running=True)
        bridge_task = asyncio.create_task(self._bridge.run(), name="ohmo-gateway-bridge")
        self._manager_task = asyncio.create_task(self._manager.start_all(), name="ohmo-gateway-channels")
        config_watch_task = asyncio.create_task(
            self._watch_config_loop(), name="ohmo-gateway-config-watcher"
        )
        restart_notice_task = asyncio.create_task(
            self._publish_pending_restart_notice(),
            name="ohmo-gateway-restart-notice",
        )
        board_recognition_task: asyncio.Task[None] | None = None
        if board_recognition_worker is not None:
            board_recognition_task = asyncio.create_task(
                board_recognition_worker.run(),
                name="ohmo-board-image-recognition",
            )
        stop_event = asyncio.Event()
        self._stop_event = stop_event
        self._restart_requested = False

        def _stop(*_: object) -> None:
            stop_event.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, _stop)

        try:
            await stop_event.wait()
        finally:
            self._bridge.stop()
            bridge_task.cancel()
            config_watch_task.cancel()
            if self._manager_task is not None:
                self._manager_task.cancel()
            if board_recognition_task is not None:
                board_recognition_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await bridge_task
            with contextlib.suppress(asyncio.CancelledError):
                await config_watch_task
            if self._manager_task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await self._manager_task
            if board_recognition_task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await board_recognition_task
            if not restart_notice_task.done():
                restart_notice_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await restart_notice_task
            await self._manager.stop_all()
            self.write_state(running=False)
            self.pid_file.unlink(missing_ok=True)
            self._stop_event = None
        if self._restart_requested:
            self._exec_restart()
        return 0


def _validate_board_recognition_settings(internal_api: object) -> None:
    """Fail startup before polling when durable recognition cannot authenticate."""
    upstream = str(getattr(internal_api, "base_url", "") or "").strip()
    agent_api = str(getattr(internal_api, "agent_api_base_url", "") or "").strip()
    token = str(getattr(internal_api, "dingtalk_token", "") or "").strip()
    missing: list[str] = []
    if not upstream:
        missing.append("internal_api.base_url")
    if not agent_api:
        missing.append("internal_api.agent_api_base_url")
    if not token or token.lower() == "bearer":
        missing.append("internal_api.dingtalk_token")
    if missing:
        raise ValueError(
            "board recognition is enabled but required settings are missing: "
            + ", ".join(missing)
        )
    _validate_http_base_url(upstream, name="internal_api.base_url")
    _validate_http_base_url(agent_api, name="internal_api.agent_api_base_url")


def _validate_http_base_url(value: str, *, name: str) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{name} is not a valid HTTP(S) URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is None and ":" in parsed.netloc.rsplit("]", 1)[-1]
    ):
        raise ValueError(f"{name} must be an HTTP(S) base URL without credentials, query, or fragment")
    decoded_segments = [segment for segment in unquote(parsed.path).split("/") if segment]
    if any(segment in {".", ".."} for segment in decoded_segments):
        raise ValueError(f"{name} must not contain dot path segments")


def start_gateway_process(cwd: str | Path | None = None, workspace: str | Path | None = None) -> int:
    """Start the gateway as a detached subprocess."""
    service = OhmoGatewayService(cwd, workspace)
    service.log_file.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    pythonpath_entries = [str(_REPO_ROOT)]
    existing_pythonpath = env.get("PYTHONPATH")
    if existing_pythonpath:
        pythonpath_entries.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)

    popen_kwargs: dict = {
        "cwd": service._cwd,
        "stdout": None,
        "stderr": None,
        "env": env,
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
        popen_kwargs["stdin"] = subprocess.DEVNULL
    else:
        popen_kwargs["start_new_session"] = True

    with service.log_file.open("a", encoding="utf-8") as log_file:
        popen_kwargs["stdout"] = log_file
        popen_kwargs["stderr"] = log_file
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "ohmo",
                "gateway",
                "run",
                "--cwd",
                service._cwd,
                "--workspace",
                str(get_workspace_root(workspace)),
            ],
            **popen_kwargs,
        )
    return process.pid


def _pid_is_running(pid: int) -> bool:
    if sys.platform == "win32":
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == 259  # STILL_ACTIVE
            return False
        finally:
            kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True


def _iter_workspace_gateway_pids(workspace: str | Path | None = None) -> list[int]:
    root = str(get_workspace_root(workspace))
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["wmic", "process", "where",
                 f"commandline like '%-m ohmo gateway run%' and commandline like '%--workspace {root}%'",
                 "get", "processid"],
                capture_output=True, text=True, check=True,
            )
        except Exception:
            return []
        current_pid = os.getpid()
        pids: list[int] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line or line.lower() == "processid":
                continue
            try:
                pid = int(line)
            except ValueError:
                continue
            if pid == current_pid:
                continue
            if _pid_is_running(pid):
                pids.append(pid)
        return pids
    else:
        try:
            result = subprocess.run(
                ["ps", "-eo", "pid=,args="],
                capture_output=True,
                text=True,
                check=True,
            )
        except Exception:
            return []

        current_pid = os.getpid()
        pids = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                pid_text, args = line.split(None, 1)
                pid = int(pid_text)
            except ValueError:
                continue
            if pid == current_pid:
                continue
            if "-m ohmo gateway run" not in args:
                continue
            if f"--workspace {root}" not in args:
                continue
            if _pid_is_running(pid):
                pids.append(pid)
        return pids


def stop_gateway_process(cwd: str | Path | None = None, workspace: str | Path | None = None) -> bool:
    """Stop the background gateway process if present."""
    service = OhmoGatewayService(cwd, workspace)
    pids: list[int] = []
    if service.pid_file.exists():
        try:
            pids.append(int(service.pid_file.read_text(encoding="utf-8").strip()))
        except ValueError:
            pass
    pids.extend(_iter_workspace_gateway_pids(workspace))
    unique_pids = []
    for pid in pids:
        if pid not in unique_pids and _pid_is_running(pid):
            unique_pids.append(pid)
    if not unique_pids:
        service.pid_file.unlink(missing_ok=True)
        return False
    if sys.platform == "win32":
        for pid in unique_pids:
            with contextlib.suppress(Exception):
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True,
                    check=False,
                )
    else:
        for pid in unique_pids:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)
    service.pid_file.unlink(missing_ok=True)
    service.write_state(running=False)
    return True


def gateway_status(cwd: str | Path | None = None, workspace: str | Path | None = None) -> GatewayState:
    """Load the last known gateway state."""
    service = OhmoGatewayService(cwd, workspace)
    live_pid: int | None = None
    if service.pid_file.exists():
        try:
            pid = int(service.pid_file.read_text(encoding="utf-8").strip())
        except ValueError:
            pid = None
        if pid is not None and _pid_is_running(pid):
            live_pid = pid
    if live_pid is None:
        live_pids = _iter_workspace_gateway_pids(workspace)
        if live_pids:
            live_pid = live_pids[0]
            service.pid_file.write_text(str(live_pid), encoding="utf-8")
        else:
            service.pid_file.unlink(missing_ok=True)

    active_sessions = 0
    last_error: str | None = None
    if service.state_file.exists():
        with contextlib.suppress(Exception):
            state = GatewayState.model_validate_json(service.state_file.read_text(encoding="utf-8"))
            active_sessions = state.active_sessions
            last_error = state.last_error

    return GatewayState(
        running=live_pid is not None,
        pid=live_pid,
        active_sessions=active_sessions,
        provider_profile=service._config.provider_profile,
        enabled_channels=service._config.enabled_channels,
        last_error=last_error,
    )
