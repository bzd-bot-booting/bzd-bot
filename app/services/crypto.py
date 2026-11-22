import json
import os
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv


def _get_fernet() -> Fernet:
    """
    Получает ключ из переменной окружения.
    Ключ никогда не записывается в БД и не выводится в лог.
    """
    load_dotenv()

    key = os.getenv("BOOKING_ENCRYPTION_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "Не задан BOOKING_ENCRYPTION_KEY. "
            "Невозможно безопасно обработать данные бронирования."
        )

    try:
        return Fernet(key.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            "BOOKING_ENCRYPTION_KEY имеет неверный формат."
        ) from exc


def encrypt_sensitive_data(data: dict[str, Any]) -> bytes:
    """
    Сериализует словарь и шифрует его.

    Результат предназначен для booking_jobs.sensitive_payload.
    """
    if not isinstance(data, dict):
        raise TypeError("Для шифрования ожидается словарь.")

    raw = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    return _get_fernet().encrypt(raw)


def decrypt_sensitive_data(encrypted: bytes) -> dict[str, Any]:
    """
    Расшифровывает данные и возвращает исходный словарь.
    """
    if not isinstance(encrypted, bytes):
        raise TypeError("Зашифрованные данные должны иметь тип bytes.")

    try:
        raw = _get_fernet().decrypt(encrypted)
    except InvalidToken as exc:
        raise ValueError(
            "Не удалось расшифровать данные: неверный ключ "
            "или повреждённый шифротекст."
        ) from exc

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            "Расшифрованные данные имеют неверный формат."
        ) from exc

    if not isinstance(data, dict):
        raise ValueError("Расшифрованный объект не является словарём.")

    return data
