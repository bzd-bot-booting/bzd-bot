"""Ежедневный email-отчёт и уведомления об ошибках. SMTP необязателен."""
from __future__ import annotations
import asyncio
import logging
import smtplib
import socket
import time
from collections import deque
from datetime import datetime, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo
from app.core.redaction import redact_sensitive_text

logger = logging.getLogger(__name__)

class EmailReporter(logging.Handler):
    def __init__(self, settings):
        super().__init__(level=logging.ERROR)
        self.settings = settings
        self.loop = None
        self.started_at = time.time()
        self.errors = deque(maxlen=200)
        self._last_alert_at = 0.0
        self._alert_cooldown = max(60, settings.smtp_error_cooldown_seconds)

    @property
    def enabled(self):
        return bool(self.settings.smtp_host and self.settings.email_to and self.settings.email_from)

    def emit(self, record):
        if not self.enabled:
            return
        try:
            rendered = redact_sensitive_text(self.format(record))
            self.errors.append((time.time(), rendered))
            now = time.time()
            if now - self._last_alert_at < self._alert_cooldown:
                return
            self._last_alert_at = now
            if self.loop and self.loop.is_running():
                self.loop.call_soon_threadsafe(lambda: asyncio.create_task(self._send_error(rendered)))
        except Exception:
            self.handleError(record)

    def attach(self, loop):
        self.loop = loop
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(self)

    async def _send(self, subject, body):
        await asyncio.to_thread(self._send_sync, subject, body)

    def _send_sync(self, subject, body):
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, self.settings.email_from, self.settings.email_to
        msg.set_content(redact_sensitive_text(body))
        timeout = self.settings.smtp_timeout_seconds
        if self.settings.smtp_use_ssl:
            server = smtplib.SMTP_SSL(self.settings.smtp_host, self.settings.smtp_port, timeout=timeout)
        else:
            server = smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=timeout)
        try:
            if self.settings.smtp_starttls and not self.settings.smtp_use_ssl:
                server.starttls()
            if self.settings.smtp_username:
                server.login(self.settings.smtp_username, self.settings.smtp_password)
            server.send_message(msg)
        finally:
            server.quit()

    async def _send_error(self, rendered):
        try:
            await self._send(f"[bzhd_bot] Ошибка на сервере {socket.gethostname()}",
                             "Зафиксирована ошибка приложения.\n\n" + rendered[-12000:])
        except Exception:
            logger.exception("Не удалось отправить email-уведомление об ошибке")

    async def run_daily(self):
        if not self.enabled:
            logger.info("Email-отчёты отключены: SMTP/email не настроены")
            return
        tz = ZoneInfo(self.settings.report_timezone)
        logger.info("Email-отчёты включены: время=%02d:%02d, timezone=%s",
                    self.settings.report_hour, self.settings.report_minute,
                    self.settings.report_timezone)
        while True:
            now = datetime.now(tz)
            target = now.replace(hour=self.settings.report_hour, minute=self.settings.report_minute, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            await asyncio.sleep(max(1, (target-now).total_seconds()))
            errors = list(self.errors)
            uptime = int(time.time()-self.started_at)
            if errors:
                status = f"ОБНАРУЖЕНЫ ОШИБКИ: {len(errors)} за время работы процесса."
                details = "\n\n".join(item[1] for item in errors[-30:])
            else:
                status = "КРИТИЧЕСКИХ ОШИБОК НЕ ЗАФИКСИРОВАНО."
                details = "За время работы процесса ошибок уровня ERROR/CRITICAL не зарегистрировано."
            body = (f"Ежедневный отчёт bzhd_bot\nВремя: {datetime.now(tz).isoformat()}\n"
                    f"Сервер: {socket.gethostname()}\nСтатус: {status}\n"
                    f"Время работы текущего процесса: {uptime} сек.\n\nОшибки:\n{details}\n\n"
                    "Отсутствие ошибок в логах не гарантирует доступность внешнего сайта или успешное выполнение всех операций.")
            try:
                await self._send("[bzhd_bot] Ежедневный отчёт", body)
                self.errors.clear()
                logger.info("Ежедневный email-отчёт отправлен")
            except Exception:
                logger.exception("Не удалось отправить ежедневный email-отчёт")
