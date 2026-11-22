"""Единая JSON-система логирования для stdout и файла."""
from __future__ import annotations
import json
import logging
import os
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from app.core.redaction import redact_sensitive_text

class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "service_name": os.getenv("SERVICE_NAME", "bzhd_bot"),
            "logger": record.name,
            "message": redact_sensitive_text(record.getMessage()),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
            "process": record.process,
            "thread": record.threadName,
        }
        for key in ("event", "correlation_id", "booking_job_id", "monitor_key", "chat_id", "stage", "duration_ms", "exception_type"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = str(value) if key in {"booking_job_id", "monitor_key"} else value
        if record.exc_info:
            payload["exception"] = redact_sensitive_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)

def configure_logging() -> None:
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    formatter = JsonFormatter()
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)
    log_file = os.getenv("LOG_FILE", "/var/log/bzhd_bot/app.json.log")
    try:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(path, maxBytes=20 * 1024 * 1024, backupCount=7, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        root.exception("Не удалось настроить файловое логирование; остаётся stdout")
    logging.getLogger("aiogram").setLevel(os.getenv("AIOGRAM_LOG_LEVEL", "INFO"))
    logging.getLogger("sqlalchemy.engine").setLevel("WARNING")
    logging.getLogger("playwright").setLevel("WARNING")
