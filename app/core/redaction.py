"""Минимальная фильтрация секретов и явно помеченных персональных данных в логах."""
from __future__ import annotations
import re

_PATTERNS = (
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b"), "[REDACTED_TELEGRAM_TOKEN]"),
    (re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|telegram[_-]?token|booking[_-]?encryption[_-]?key|smtp[_-]?password)\b\s*[:=]\s*)([^\s,;]+)"), r"\1[REDACTED]"),
    (re.compile(r"(://[^:/\s]+:)[^@/\s]+(@)"), r"\1[REDACTED]\2"),
    (re.compile(r"(?i)(\b(?:passport|document[_ -]?(?:number|no)|phone|email|birth[_ -]?date|first[_ -]?name|last[_ -]?name|middle[_ -]?name)\b\s*[:=]\s*)([^\s,;]+)"), r"\1[REDACTED]"),
)

def redact_sensitive_text(value: object) -> str:
    text = str(value)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
