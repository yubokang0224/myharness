"""Email alerting driven by log-record pattern matching.

An ``EmailAlertHandler`` sits on the root logger and watches for known
failure signatures (DingTalk disconnects, LLM API retry exhaustion, tool
error spikes).  When a rule's threshold is reached inside its time window,
one aggregated email is sent and the rule enters a cooldown so a sustained
outage produces a handful of emails, not a flood.

Configuration lives in ``gateway.json`` under ``alerting`` (see
``ohmo.gateway.models.AlertingConfig``).  SMTP sending happens on a daemon
thread so the event loop is never blocked.
"""

from __future__ import annotations

import logging
import re
import smtplib
import ssl
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from email.message import EmailMessage

from ohmo.gateway.models import AlertingConfig

logger = logging.getLogger(__name__)


@dataclass
class AlertRule:
    """Trigger when ``threshold`` matching records arrive within ``window_seconds``."""

    name: str
    title: str
    pattern: re.Pattern[str]
    min_level: int = logging.WARNING
    threshold: int = 1
    window_seconds: int = 600
    hits: deque = field(default_factory=deque, repr=False)
    last_alert_at: float = 0.0

    def matches(self, record: logging.LogRecord, message: str) -> bool:
        return record.levelno >= self.min_level and bool(self.pattern.search(message))


def default_rules() -> list[AlertRule]:
    return [
        AlertRule(
            name="dingtalk_startup_failure",
            title="钉钉机器人启动失败（静默死亡风险）",
            pattern=re.compile(
                r"DingTalk (client_id and client_secret not configured"
                r"|Stream SDK not installed)"
                r"|Failed to start DingTalk channel"
            ),
            min_level=logging.ERROR,
            threshold=1,
            window_seconds=60,
        ),
        AlertRule(
            name="dingtalk_disconnected",
            title="钉钉长连接持续断线",
            pattern=re.compile(r"DingTalk stream error"),
            threshold=8,
            window_seconds=600,
        ),
        AlertRule(
            name="llm_api_failure",
            title="LLM API 调用重试耗尽",
            pattern=re.compile(r"Model stream failed"),
            threshold=3,
            window_seconds=1800,
        ),
        AlertRule(
            name="tool_error_spike",
            title="工具执行异常激增",
            pattern=re.compile(r"tool execution raised"),
            min_level=logging.ERROR,
            threshold=5,
            window_seconds=600,
        ),
    ]


class EmailAlertHandler(logging.Handler):
    """Watch log records, aggregate matches per rule, email on threshold."""

    def __init__(
        self,
        config: AlertingConfig,
        *,
        process_name: str = "gateway",
        rules: list[AlertRule] | None = None,
    ) -> None:
        super().__init__(level=logging.WARNING)
        self._config = config
        self._process_name = process_name
        self._rules = rules if rules is not None else default_rules()
        self._lock_state = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        if record.name == __name__:
            return  # never react to our own logs
        try:
            message = record.getMessage()
        except Exception:
            return
        now = time.time()
        cooldown = max(1, self._config.cooldown_minutes) * 60
        for rule in self._rules:
            if not rule.matches(record, message):
                continue
            with self._lock_state:
                rule.hits.append((now, message))
                while rule.hits and now - rule.hits[0][0] > rule.window_seconds:
                    rule.hits.popleft()
                if len(rule.hits) < rule.threshold:
                    continue
                if now - rule.last_alert_at < cooldown:
                    continue
                rule.last_alert_at = now
                samples = [item[1] for item in list(rule.hits)[-20:]]
                hit_count = len(rule.hits)
                rule.hits.clear()
            self._send_async(rule, hit_count, samples)

    def _send_async(self, rule: AlertRule, hit_count: int, samples: list[str]) -> None:
        thread = threading.Thread(
            target=self._send,
            args=(rule, hit_count, samples),
            name=f"alert-email:{rule.name}",
            daemon=True,
        )
        thread.start()

    def _send(self, rule: AlertRule, hit_count: int, samples: list[str]) -> None:
        config = self._config
        if not (config.smtp_host and config.to_addresses):
            logger.warning(
                "Alert '%s' triggered but SMTP is not configured (smtp_host/to_addresses)",
                rule.name,
            )
            return
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        body_lines = [
            f"告警规则: {rule.title} ({rule.name})",
            f"进程: {self._process_name}",
            f"时间: {timestamp}",
            f"窗口 {rule.window_seconds}s 内命中 {hit_count} 条（阈值 {rule.threshold}）。",
            f"冷却 {config.cooldown_minutes} 分钟内不再重复发送。",
            "",
            "最近日志样本:",
            *[f"  - {sample[:500]}" for sample in samples],
            "",
            "排查: journalctl -u ohmo-gateway 或 web 智能体运行日志页面。",
        ]
        msg = EmailMessage()
        msg["Subject"] = f"[ohmo告警] {rule.title} ({self._process_name})"
        msg["From"] = config.from_address or config.smtp_username
        msg["To"] = ", ".join(config.to_addresses)
        msg.set_content("\n".join(body_lines))
        try:
            if config.smtp_use_ssl:
                with smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=30) as smtp:
                    if config.smtp_username:
                        smtp.login(config.smtp_username, config.smtp_password)
                    smtp.send_message(msg)
            else:
                with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30) as smtp:
                    if config.smtp_use_tls:
                        smtp.starttls(context=ssl.create_default_context())
                    if config.smtp_username:
                        smtp.login(config.smtp_username, config.smtp_password)
                    smtp.send_message(msg)
            logger.info("Alert email sent rule=%s hits=%s", rule.name, hit_count)
        except Exception:
            logger.exception("Failed to send alert email rule=%s", rule.name)


def send_email(
    config: AlertingConfig,
    *,
    subject: str,
    body: str,
    html: bool = False,
    attachments: list[tuple[str, bytes]] | None = None,
) -> bool:
    """One-shot email helper (used by the weekly failure report)."""
    if not (config.smtp_host and config.to_addresses):
        logger.warning("Cannot send email '%s': SMTP not configured", subject)
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.from_address or config.smtp_username
    msg["To"] = ", ".join(config.to_addresses)
    if html:
        msg.set_content("此邮件为 HTML 格式，请使用支持 HTML 的邮件客户端查看。")
        msg.add_alternative(body, subtype="html")
    else:
        msg.set_content(body)
    for filename, data in attachments or []:
        msg.add_attachment(
            data,
            maintype="text",
            subtype="markdown",
            filename=filename,
        )
    try:
        if config.smtp_use_ssl:
            with smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=30) as smtp:
                if config.smtp_username:
                    smtp.login(config.smtp_username, config.smtp_password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30) as smtp:
                if config.smtp_use_tls:
                    smtp.starttls(context=ssl.create_default_context())
                if config.smtp_username:
                    smtp.login(config.smtp_username, config.smtp_password)
                smtp.send_message(msg)
        return True
    except Exception:
        logger.exception("Failed to send email '%s'", subject)
        return False
