"""Зашифрованное хранилище учётных данных и пассажиров мониторинга."""

from typing import Any

from sqlalchemy import select

from app.database.models.monitor_profile import MonitorProfile
from app.services.crypto import decrypt_sensitive_data, encrypt_sensitive_data


class MonitorProfileService:
    """Один защищённый профиль на Telegram chat_id."""

    @staticmethod
    def _validate(
        payload: dict[str, Any],
        *,
        require_passengers: bool = False,
    ) -> None:
        login = str(payload.get("login") or "").strip()
        password = str(payload.get("password") or "")
        passengers = payload.get("passengers", [])

        if not login or len(login) > 200:
            raise ValueError("Некорректный логин pass.rw.by.")
        if not password or len(password) > 1024:
            raise ValueError("Некорректный пароль pass.rw.by.")
        if not isinstance(passengers, list):
            raise ValueError("Некорректный список пассажиров.")
        if require_passengers and not passengers:
            raise ValueError("Не сохранены данные пассажиров.")
        if len(passengers) > 10:
            raise ValueError("Допускается не более 10 пассажиров.")

        required = ("last_name", "first_name", "document_type", "document_number")
        for index, passenger in enumerate(passengers, start=1):
            if not isinstance(passenger, dict):
                raise ValueError(f"Некорректные данные пассажира №{index}.")
            if any(not str(passenger.get(key) or "").strip() for key in required):
                raise ValueError(f"Заполните обязательные поля пассажира №{index}.")

    async def save(self, chat_id: int, payload: dict[str, Any]) -> None:
        """Шифрует профиль до записи в PostgreSQL."""
        self._validate(payload)
        encrypted = encrypt_sensitive_data(payload)
        from app.database.engine import SessionFactory

        async with SessionFactory() as session:
            async with session.begin():
                row = await session.get(
                    MonitorProfile,
                    chat_id,
                    with_for_update=True,
                )
                if row is None:
                    row = MonitorProfile(
                        chat_id=chat_id,
                        encrypted_payload=encrypted,
                    )
                    session.add(row)
                else:
                    row.encrypted_payload = encrypted

    async def save_credentials(
        self,
        chat_id: int,
        login: str,
        password: str,
    ) -> None:
        """Сохраняет подтверждённые сайтом учётные данные, сохраняя пассажиров."""
        current = await self.get(chat_id) or {}
        current["login"] = login
        current["password"] = password
        current.setdefault("passengers", [])
        await self.save(chat_id, current)

    async def save_passengers(
        self,
        chat_id: int,
        passengers: list[dict[str, Any]],
    ) -> None:
        """Обновляет пассажиров, не перезаписывая сохранённые учётные данные."""
        current = await self.get(chat_id)
        if not current:
            raise LookupError(
                "Сначала выполните вход на pass.rw.by, чтобы сохранить профиль."
            )
        current["passengers"] = passengers
        if isinstance(current.get("bedding"), bool):
            for passenger in current["passengers"]:
                if isinstance(passenger, dict):
                    passenger["bedding"] = current["bedding"]
        await self.save(chat_id, current)

    async def save_bedding_preference(
        self,
        chat_id: int,
        enabled: bool,
    ) -> None:
        """Сохраняет общее предпочтение белья для заказа и синхронизирует пассажиров."""
        current = await self.get(chat_id)
        if not current:
            raise LookupError("Сначала сохраните данные пассажиров.")
        current["bedding"] = bool(enabled)
        for passenger in current.get("passengers") or []:
            if isinstance(passenger, dict):
                passenger["bedding"] = bool(enabled)
        await self.save(chat_id, current)

    async def save_document_options(
        self,
        chat_id: int,
        options: list[dict[str, Any]],
    ) -> None:
        """Сохраняет фактический список типов документов, полученный с сайта."""
        normalized = []
        seen = set()
        for item in options:
            if not isinstance(item, dict):
                continue
            value = str(item.get("value") or "").strip()
            text = str(item.get("text") or "").strip()
            if value and text and value not in seen:
                normalized.append({"value": value, "text": text})
                seen.add(value)
        if not normalized:
            raise ValueError("Сайт не предоставил доступные типы документов.")

        current = await self.get(chat_id)
        if not current:
            raise LookupError("Сначала сохраните учётные данные pass.rw.by.")
        current["document_options"] = normalized
        await self.save(chat_id, current)

    async def get(self, chat_id: int) -> dict[str, Any] | None:
        from app.database.engine import SessionFactory

        async with SessionFactory() as session:
            encrypted = await session.scalar(
                select(MonitorProfile.encrypted_payload).where(
                    MonitorProfile.chat_id == chat_id
                )
            )

        if encrypted is None:
            return None
        payload = decrypt_sensitive_data(bytes(encrypted))
        self._validate(payload)
        return payload

    async def delete(self, chat_id: int) -> bool:
        from app.database.engine import SessionFactory

        async with SessionFactory() as session:
            async with session.begin():
                row = await session.get(
                    MonitorProfile,
                    chat_id,
                    with_for_update=True,
                )
                if row is None:
                    return False
                await session.delete(row)
                return True
