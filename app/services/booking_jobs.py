"""
Сервис долговременных задач автоматического бронирования.

В PostgreSQL:
- несекретные параметры хранятся в обычных колонках и JSONB;
- конфиденциальные данные хранятся только в зашифрованном sensitive_payload;
- lease_owner и lease_until защищают задачу от одновременной обработки
  несколькими экземплярами приложения.

Важно:
Захват задачи со статусом submitting или order_unknown не означает,
что заказ можно отправлять повторно. Для таких задач требуется
отдельная проверка результата на сайте.
"""

import asyncio
import logging
import uuid
from html import escape
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from sqlalchemy import or_, select

from app.database.engine import SessionFactory
from app.database.models.booking_job import BookingJob
from app.services.crypto import (
    decrypt_sensitive_data,
    encrypt_sensitive_data,
)
from app.services.monitor_profiles import MonitorProfileService


# Статусы, из которых задача может быть восстановлена.
RECOVERABLE_STATUSES = frozenset({
    "waiting",
    "running",
    "submitting",
    "order_unknown",
})

# Завершённые статусы. Конфиденциальные данные для них не сохраняются.
logger = logging.getLogger(__name__)

TERMINAL_STATUSES = frozenset({
    "order_created",
    "cancelled",
    "expired",
    "failed",
})

ALLOWED_STATUSES = RECOVERABLE_STATUSES | TERMINAL_STATUSES | {
    "draft",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _validate_status(status: str) -> None:
    if status not in ALLOWED_STATUSES:
        raise ValueError(f"Недопустимый статус задачи: {status}")


def _booking_confirmation_message(
    job: BookingJob,
    payload: dict | None,
    order_number: str | None,
    order_expires_at: datetime | None,
) -> str:
    """Формирует личное уведомление по подтверждённому сайтом заказу."""
    train_data = dict(job.train_data or {})
    selection = dict(train_data.get("booking_selection") or {})
    seats = list(selection.get("seats") or [])
    passengers = list((payload or {}).get("passengers") or [])

    safe = lambda value, fallback="—": escape(str(value if value not in (None, "") else fallback))
    lines = [
        "✅ <b>Заказ подтверждён сайтом pass.rw.by!</b>",
        "",
        "<b>Информация о поездке</b>",
        f"🚆 Поезд: <b>№ {safe(job.train_number)}</b>",
        f"📍 Маршрут: <b>{safe(job.from_name)}</b> → <b>{safe(job.to_name)}</b>",
        f"📅 Дата: <b>{safe(job.travel_date)}</b>",
        f"🕒 Отправление: <b>{safe(train_data.get('time'))}</b>",
        f"🕒 Прибытие: <b>{safe(train_data.get('arr_time'))}</b>",
        "",
        "<b>Информация о билете</b>",
        f"🚃 Тип вагона: <b>{safe(selection.get('car_type_name') or selection.get('car_type'))}</b>",
    ]

    for index, seat in enumerate(seats, start=1):
        carriage = safe(seat.get("carriage_number"))
        seat_number = safe(seat.get("seat_number"))
        lines.append(
            f"🎟 Билет {index}: вагон <b>{carriage}</b>, место <b>{seat_number}</b>"
        )

    lines.extend(["", "<b>Данные пассажиров</b>"])
    for index, passenger in enumerate(passengers, start=1):
        full_name = " ".join(
            str(passenger.get(key) or "").strip()
            for key in ("last_name", "first_name", "middle_name")
            if str(passenger.get(key) or "").strip()
        )
        lines.extend([
            f"<b>Пассажир {index}</b>",
            f"ФИО: <b>{safe(full_name)}</b>",
            f"Тип документа: <b>{safe(passenger.get('document_type'))}</b>",
            f"Номер документа: <b>{safe(passenger.get('document_number'))}</b>",
        ])

    lines.extend([
        "",
        f"Номер заказа: <b>{safe(order_number)}</b>",
    ])
    if order_expires_at is not None:
        expiry = order_expires_at
        if expiry.tzinfo is not None:
            expiry = expiry.astimezone()
        lines.append(
            f"⏰ Оплатить необходимо до <b>{expiry:%d.%m.%Y %H:%M}</b>."
        )
    else:
        lines.append(
            "⏰ Обычно на оплату остаётся около <b>20 минут</b>. "
            "Точный срок проверьте в личном кабинете pass.rw.by."
        )
    if (job.preferences or {}).get("source") == "monitor":
        lines.extend([
            "",
            "🗑 <b>Если вы оплатили билет, удалите мониторинг кнопкой ниже.</b>",
            "Если мониторинг не удалить, через 20 минут после срока бронирования поиск возобновится автоматически.",
        ])
    return "\n".join(lines)


class BookingJobsService:
    """Работа с долговременными задачами бронирования."""

    async def create_job(
        self,
        *,
        chat_id: int,
        from_name: str,
        to_name: str,
        travel_date: str,
        train_number: str,
        url: str,
        train_data: dict[str, Any] | None = None,
        preferences: dict[str, Any] | None = None,
        sensitive_data: dict[str, Any] | None = None,
        status: str = "draft",
        current_step: str = "created",
    ) -> BookingJob:
        """
        Создаёт задачу.

        sensitive_data шифруется до обращения к БД.
        В train_data и preferences нельзя передавать пароли,
        номера документов и другие конфиденциальные сведения.
        """
        _validate_status(status)

        if status in TERMINAL_STATUSES and sensitive_data:
            raise ValueError(
                "Нельзя создавать завершённую задачу с конфиденциальными данными."
            )

        encrypted_payload = (
            encrypt_sensitive_data(sensitive_data)
            if sensitive_data is not None
            else None
        )

        job = BookingJob(
            chat_id=chat_id,
            from_name=from_name,
            to_name=to_name,
            travel_date=travel_date,
            train_number=train_number,
            train_data=train_data or {},
            url=url,
            status=status,
            current_step=current_step,
            preferences=preferences or {},
            sensitive_payload=encrypted_payload,
        )

        async with SessionFactory() as session:
            async with session.begin():
                session.add(job)
                await session.flush()

        return job

    async def create_monitor_job_once(
        self,
        *,
        chat_id: int,
        from_name: str,
        to_name: str,
        travel_date: str,
        train_number: str,
        url: str,
        train_data: dict[str, Any],
        monitor_key: str,
        subscription_order: int,
        tickets_count: int,
        max_price: Decimal,
        sensitive_data: dict[str, Any],
    ) -> tuple[BookingJob, bool]:
        """Создаёт не более одной активной автозадачи для заявки мониторинга."""
        from sqlalchemy import text

        identity = {
            "source": "monitor",
            "monitor_key": monitor_key,
            "subscription_order": int(subscription_order),
        }

        async with SessionFactory() as session:
            async with session.begin():
                # Межпроцессная блокировка для защиты от двух одинаковых job.
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                    {"k": f"monitor-booking:{monitor_key}:{chat_id}:{subscription_order}"},
                )
                existing = await session.scalar(
                    select(BookingJob)
                    .where(
                        BookingJob.chat_id == chat_id,
                        BookingJob.preferences.contains(identity),
                    )
                    .order_by(BookingJob.created_at.desc())
                    .limit(1)
                    .with_for_update()
                )
                if existing is not None and existing.status not in {"cancelled", "expired"}:
                    return existing, False

                encrypted = encrypt_sensitive_data(sensitive_data)
                job = BookingJob(
                    chat_id=chat_id,
                    from_name=from_name,
                    to_name=to_name,
                    travel_date=travel_date,
                    train_number=train_number,
                    train_data=train_data or {},
                    url=url,
                    status="waiting",
                    current_step="monitor_triggered",
                    preferences={
                        **identity,
                        "tickets_count": int(tickets_count),
                        "max_price": str(max_price),
                    },
                    sensitive_payload=encrypted,
                )
                session.add(job)
                await session.flush()
                return job, True

    async def claim_monitor_jobs(
        self,
        worker_id: str,
        *,
        limit: int = 3,
        lease_seconds: int = 600,
    ) -> list[BookingJob]:
        """Захватывает только monitor jobs, которые ещё безопасно выполнять."""
        now = _utcnow()
        async with SessionFactory() as session:
            async with session.begin():
                query = (
                    select(BookingJob)
                    .where(
                        BookingJob.preferences.contains({"source": "monitor"}),
                        BookingJob.status.in_({"waiting", "running", "submitting", "order_unknown"}),
                        or_(BookingJob.lease_until.is_(None), BookingJob.lease_until <= now),
                        or_(BookingJob.next_attempt_at.is_(None), BookingJob.next_attempt_at <= now),
                    )
                    .order_by(BookingJob.created_at.asc())
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
                result = await session.execute(query)
                jobs = list(result.scalars().all())
                for job in jobs:
                    job.lease_owner = worker_id
                    job.lease_until = now + timedelta(seconds=lease_seconds)
                    job.attempt_count += 1
                    if job.status in {"waiting", "running"}:
                        job.status = "running"
                        job.current_step = "automatic_booking"
                    else:
                        # submitting/order_unknown всегда идут только на сверку сайта.
                        job.current_step = "reconcile_order"
                    job.updated_at = now
                return jobs

    async def mark_order_rejected_for_retry(
        self,
        job_id: uuid.UUID,
        worker_id: str,
        *,
        retry_after_seconds: int = 15,
    ) -> None:
        """Разрешает повтор только после явного отказа сайта создать заказ."""
        async with SessionFactory() as session:
            async with session.begin():
                job = await session.scalar(
                    select(BookingJob).where(BookingJob.id == job_id).with_for_update()
                )
                if job is None:
                    raise LookupError("Задача бронирования не найдена.")
                if job.status != "submitting":
                    raise ValueError("Повтор разрешён только после явного отказа из submitting.")
                if job.lease_owner != worker_id:
                    raise RuntimeError("Задача не принадлежит текущему worker.")
                job.status = "waiting"
                job.current_step = "retry_after_site_rejection"
                job.last_error = "Сайт явно отклонил создание заказа; повтор разрешён."
                job.lease_owner = None
                job.lease_until = None
                job.next_attempt_at = _utcnow() + timedelta(seconds=max(5, retry_after_seconds))
                job.updated_at = _utcnow()

    async def mark_failed(
        self,
        job_id: uuid.UUID,
        *,
        error: str,
    ) -> None:
        """Фиксирует окончательную ошибку до отправки заказа и очищает секреты."""
        async with SessionFactory() as session:
            async with session.begin():
                job = await session.scalar(
                    select(BookingJob).where(BookingJob.id == job_id).with_for_update()
                )
                if job is None:
                    raise LookupError("Задача бронирования не найдена.")
                if job.status in {"submitting", "order_unknown", "order_created"}:
                    raise ValueError("Нельзя пометить задачу ошибочной без сверки результата заказа.")
                job.status = "failed"
                job.current_step = "failed"
                job.last_error = str(error)[:2000]
                job.sensitive_payload = None
                job.lease_owner = None
                job.lease_until = None
                job.next_attempt_at = None
                job.completed_at = _utcnow()
                job.updated_at = _utcnow()

    async def get_job(
        self,
        job_id: uuid.UUID,
        *,
        chat_id: int | None = None,
    ) -> BookingJob | None:
        """Получает задачу. При передаче chat_id проверяет владельца."""
        async with SessionFactory() as session:
            query = select(BookingJob).where(
                BookingJob.id == job_id
            )

            if chat_id is not None:
                query = query.where(BookingJob.chat_id == chat_id)

            result = await session.execute(query)
            return result.scalar_one_or_none()

    async def save_sensitive_payload(
        self,
        job_id: uuid.UUID,
        data: dict[str, Any],
        *,
        chat_id: int | None = None,
    ) -> None:
        """Шифрует и сохраняет конфиденциальные данные существующей задачи."""
        encrypted_payload = encrypt_sensitive_data(data)

        async with SessionFactory() as session:
            async with session.begin():
                query = (
                    select(BookingJob)
                    .where(BookingJob.id == job_id)
                    .with_for_update()
                )

                if chat_id is not None:
                    query = query.where(BookingJob.chat_id == chat_id)

                result = await session.execute(query)
                job = result.scalar_one_or_none()

                if job is None:
                    raise LookupError("Задача бронирования не найдена.")

                if job.status not in {"draft", "waiting", "running"}:
                    raise ValueError(
                        "Нельзя изменять конфиденциальные данные "
                        "на текущем этапе бронирования."
                    )

                job.sensitive_payload = encrypted_payload
                job.updated_at = _utcnow()

    async def get_sensitive_payload(
        self,
        job_id: uuid.UUID,
        *,
        chat_id: int | None = None,
    ) -> dict[str, Any] | None:
        """
        Получает и расшифровывает конфиденциальные данные.

        Расшифровка выполняется после чтения шифротекста из БД.
        """
        async with SessionFactory() as session:
            query = select(BookingJob.sensitive_payload).where(
                BookingJob.id == job_id
            )

            if chat_id is not None:
                query = query.where(BookingJob.chat_id == chat_id)

            result = await session.execute(query)
            encrypted_payload = result.scalar_one_or_none()

        if encrypted_payload is None:
            return None

        return decrypt_sensitive_data(bytes(encrypted_payload))

    async def update_job_state(
        self,
        job_id: uuid.UUID,
        *,
        status: str | None = None,
        current_step: str | None = None,
        train_data: dict[str, Any] | None = None,
        preferences: dict[str, Any] | None = None,
        next_attempt_at: datetime | None = None,
        last_error: str | None = None,
        chat_id: int | None = None,
    ) -> None:
        """
        Обновляет состояние и несекретные параметры задачи.

        Не предназначен для перевода задачи в submitting или
        order_created: для этих переходов есть отдельные методы.
        """
        if status is not None:
            _validate_status(status)

            if status not in {"draft", "waiting", "running"}:
                raise ValueError(
                    "Через update_job_state разрешены только статусы "
                    "draft, waiting и running."
                )

        async with SessionFactory() as session:
            async with session.begin():
                query = (
                    select(BookingJob)
                    .where(BookingJob.id == job_id)
                    .with_for_update()
                )

                if chat_id is not None:
                    query = query.where(BookingJob.chat_id == chat_id)

                result = await session.execute(query)
                job = result.scalar_one_or_none()

                if job is None:
                    raise LookupError("Задача бронирования не найдена.")

                if job.status in TERMINAL_STATUSES:
                    raise ValueError("Задача уже завершена.")

                if (
                    job.status in {"submitting", "order_unknown"}
                    and status is not None
                ):
                    raise ValueError(
                        "Сначала необходимо проверить результат заказа "
                        "на сайте перевозчика."
                    )

                if status is not None:
                    job.status = status

                if current_step is not None:
                    job.current_step = current_step

                if train_data is not None:
                    job.train_data = train_data

                if preferences is not None:
                    job.preferences = preferences

                if next_attempt_at is not None:
                    job.next_attempt_at = next_attempt_at

                if last_error is not None:
                    # Не передавать сюда исключения, содержащие пароли,
                    # номера документов или содержимое форм сайта.
                    job.last_error = last_error[:2000]

                job.updated_at = _utcnow()

    async def claim_resumable_jobs(
        self,
        worker_id: str,
        *,
        limit: int = 10,
        lease_seconds: int = 120,
    ) -> list[BookingJob]:
        """
        Атомарно захватывает задачи для восстановления.

        Используется SELECT FOR UPDATE SKIP LOCKED:
        параллельные экземпляры приложения не должны получить
        одну и ту же задачу одновременно.

        submitting и order_unknown возвращаются для сверки результата,
        но их нельзя повторно отправлять без проверки сайта.
        """
        if not worker_id or len(worker_id) > 128:
            raise ValueError("Некорректный worker_id.")

        if limit < 1 or limit > 100:
            raise ValueError("limit должен быть от 1 до 100.")

        if lease_seconds < 10:
            raise ValueError("lease_seconds не может быть меньше 10.")

        now = _utcnow()
        lease_until = now + timedelta(seconds=lease_seconds)

        async with SessionFactory() as session:
            async with session.begin():
                query = (
                    select(BookingJob)
                    .where(
                        BookingJob.status.in_(RECOVERABLE_STATUSES),
                        or_(
                            BookingJob.lease_until.is_(None),
                            BookingJob.lease_until <= now,
                        ),
                        or_(
                            BookingJob.next_attempt_at.is_(None),
                            BookingJob.next_attempt_at <= now,
                        ),
                    )
                    .order_by(
                        BookingJob.created_at.asc(),
                        BookingJob.id.asc(),
                    )
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )

                result = await session.execute(query)
                jobs = list(result.scalars().all())

                for job in jobs:
                    job.lease_owner = worker_id
                    job.lease_until = lease_until
                    job.attempt_count += 1
                    job.updated_at = now

            # Возвращаем ORM-объекты после фиксации транзакции.
            return jobs

    async def release_lease(
        self,
        job_id: uuid.UUID,
        worker_id: str,
        *,
        retry_after_seconds: int | None = None,
    ) -> bool:
        """Освобождает блокировку, принадлежащую указанному worker."""
        async with SessionFactory() as session:
            async with session.begin():
                query = (
                    select(BookingJob)
                    .where(
                        BookingJob.id == job_id,
                        BookingJob.lease_owner == worker_id,
                    )
                    .with_for_update()
                )

                result = await session.execute(query)
                job = result.scalar_one_or_none()

                if job is None:
                    return False

                job.lease_owner = None
                job.lease_until = None

                if retry_after_seconds is not None:
                    job.next_attempt_at = (
                        _utcnow()
                        + timedelta(seconds=max(0, retry_after_seconds))
                    )

                job.updated_at = _utcnow()

                return True

    async def mark_submitting(
        self,
        job_id: uuid.UUID,
        *,
        worker_id: str | None = None,
    ) -> None:
        """
        Фиксирует намерение отправить заказ ДО нажатия кнопки submit.

        Повторный переход из submitting запрещён.
        """
        async with SessionFactory() as session:
            async with session.begin():
                query = (
                    select(BookingJob)
                    .where(BookingJob.id == job_id)
                    .with_for_update()
                )

                result = await session.execute(query)
                job = result.scalar_one_or_none()

                if job is None:
                    raise LookupError("Задача бронирования не найдена.")

                if job.status not in {"waiting", "running"}:
                    raise ValueError(
                        f"Нельзя отправить заказ из статуса {job.status}."
                    )

                if worker_id is not None and job.lease_owner != worker_id:
                    raise RuntimeError(
                        "Задача не принадлежит текущему рабочему процессу."
                    )

                job.status = "submitting"
                job.current_step = "submitting_order"
                job.updated_at = _utcnow()

    async def mark_order_unknown(
        self,
        job_id: uuid.UUID,
        *,
        error: str | None = None,
        retry_after_seconds: int | None = 120,
    ) -> None:
        """
        Отмечает неопределённый результат отправки.

        Конфиденциальные данные пока сохраняются: сначала нужно
        выяснить на сайте, был ли заказ создан.
        """
        async with SessionFactory() as session:
            async with session.begin():
                query = (
                    select(BookingJob)
                    .where(BookingJob.id == job_id)
                    .with_for_update()
                )

                result = await session.execute(query)
                job = result.scalar_one_or_none()

                if job is None:
                    raise LookupError("Задача бронирования не найдена.")

                if job.status not in {"submitting", "order_unknown"}:
                    raise ValueError(
                        "Неопределённый результат допустим только "
                        "после начала отправки заказа."
                    )

                job.status = "order_unknown"
                job.current_step = "reconcile_order"
                job.last_error = error[:2000] if error else None
                job.lease_owner = None
                job.lease_until = None
                if retry_after_seconds is not None:
                    job.next_attempt_at = _utcnow() + timedelta(
                        seconds=max(5, retry_after_seconds)
                    )
                job.updated_at = _utcnow()

    async def mark_order_created(
        self,
        job_id: uuid.UUID,
        *,
        order_number: str | None,
        order_expires_at: datetime | None,
    ) -> None:
        """
        Вызывается только после подтверждения создания заказа сайтом.

        Сохраняет несекретные сведения о заказе и удаляет шифротекст.
        """
        if order_expires_at is not None and order_expires_at.tzinfo is None:
            raise ValueError("order_expires_at должен содержать часовой пояс.")

        async with SessionFactory() as session:
            async with session.begin():
                query = (
                    select(BookingJob)
                    .where(BookingJob.id == job_id)
                    .with_for_update()
                )

                result = await session.execute(query)
                job = result.scalar_one_or_none()

                if job is None:
                    raise LookupError("Задача бронирования не найдена.")

                if job.status not in {
                    "submitting",
                    "order_unknown",
                }:
                    raise ValueError(
                        f"Нельзя подтвердить заказ из статуса {job.status}."
                    )

                job.status = "order_created"
                job.current_step = "order_confirmed"
                job.order_number = order_number
                job.order_expires_at = order_expires_at
                preferences = dict(job.preferences or {})
                if preferences.get("source") == "monitor":
                    resume_at = order_expires_at or (_utcnow() + timedelta(minutes=20))
                    preferences["monitor_resume_at"] = resume_at.astimezone(timezone.utc).isoformat()
                    preferences["monitor_resume_cancelled"] = False
                    preferences["monitor_resume_done"] = False
                    preferences.pop("monitor_resume_claimed_at", None)
                    job.preferences = preferences
                job.sensitive_payload = None
                job.lease_owner = None
                job.lease_until = None
                job.next_attempt_at = None
                job.last_error = None
                job.completed_at = _utcnow()
                job.updated_at = _utcnow()

    async def claim_due_monitor_resumes(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Захватывает наступившие отложенные возобновления мониторингов."""
        now = _utcnow()
        claimed: list[dict[str, Any]] = []
        async with SessionFactory() as session:
            async with session.begin():
                result = await session.execute(
                    select(BookingJob)
                    .where(
                        BookingJob.status == "order_created",
                        BookingJob.preferences.contains({"source": "monitor"}),
                    )
                    .order_by(BookingJob.updated_at.asc())
                    .limit(max(1, int(limit)))
                    .with_for_update(skip_locked=True)
                )
                for job in result.scalars().all():
                    prefs = dict(job.preferences or {})
                    if prefs.get("monitor_resume_cancelled") or prefs.get("monitor_resume_done"):
                        continue
                    raw_due = prefs.get("monitor_resume_at")
                    if not raw_due:
                        continue
                    try:
                        due_at = datetime.fromisoformat(str(raw_due))
                        if due_at.tzinfo is None:
                            due_at = due_at.replace(tzinfo=timezone.utc)
                    except (TypeError, ValueError):
                        continue
                    if due_at > now:
                        continue
                    raw_claim = prefs.get("monitor_resume_claimed_at")
                    if raw_claim:
                        try:
                            claim_at = datetime.fromisoformat(str(raw_claim))
                            if claim_at.tzinfo is None:
                                claim_at = claim_at.replace(tzinfo=timezone.utc)
                            if now - claim_at < timedelta(minutes=5):
                                continue
                        except (TypeError, ValueError):
                            pass
                    prefs["monitor_resume_claimed_at"] = now.isoformat()
                    job.preferences = prefs
                    claimed.append({
                        "job_id": job.id,
                        "chat_id": job.chat_id,
                        "monitor_key": str(prefs.get("monitor_key") or ""),
                    })
        return claimed

    async def complete_monitor_resume(self, job_id: uuid.UUID, *, success: bool) -> None:
        """Фиксирует возобновление либо назначает повтор через минуту."""
        async with SessionFactory() as session:
            async with session.begin():
                job = await session.scalar(
                    select(BookingJob).where(BookingJob.id == job_id).with_for_update()
                )
                if job is None:
                    return
                prefs = dict(job.preferences or {})
                prefs.pop("monitor_resume_claimed_at", None)
                if success:
                    prefs["monitor_resume_done"] = True
                elif not prefs.get("monitor_resume_cancelled"):
                    prefs["monitor_resume_at"] = (_utcnow() + timedelta(minutes=1)).isoformat()
                job.preferences = prefs
                job.updated_at = _utcnow()

    async def disable_monitor_resume(
        self, job_id: uuid.UUID, *, chat_id: int
    ) -> str | None:
        """Отменяет отложенное возобновление мониторинга владельцем заказа."""
        async with SessionFactory() as session:
            async with session.begin():
                job = await session.scalar(
                    select(BookingJob).where(
                        BookingJob.id == job_id,
                        BookingJob.chat_id == chat_id,
                    ).with_for_update()
                )
                if job is None or job.status != "order_created":
                    return None
                prefs = dict(job.preferences or {})
                if prefs.get("source") != "monitor":
                    return None
                prefs["monitor_resume_cancelled"] = True
                prefs["monitor_resume_done"] = True
                prefs.pop("monitor_resume_claimed_at", None)
                job.preferences = prefs
                job.updated_at = _utcnow()
                return str(prefs.get("monitor_key") or "") or None

    async def mark_order_rejected(
        self,
        job_id: uuid.UUID,
        *,
        error: str,
    ) -> None:
        """Фиксирует явный отказ сайта после однократной отправки формы."""
        async with SessionFactory() as session:
            async with session.begin():
                job = await session.scalar(
                    select(BookingJob)
                    .where(BookingJob.id == job_id)
                    .with_for_update()
                )
                if job is None:
                    raise LookupError("Задача бронирования не найдена.")
                if job.status != "submitting":
                    raise ValueError(
                        "Явный отказ можно зафиксировать только после начала отправки заказа."
                    )
                job.status = "failed"
                job.current_step = "order_rejected"
                job.last_error = str(error)[:2000]
                job.sensitive_payload = None
                job.lease_owner = None
                job.lease_until = None
                job.next_attempt_at = None
                job.completed_at = _utcnow()
                job.updated_at = _utcnow()


    async def cancel_job(
        self,
        job_id: uuid.UUID,
        *,
        chat_id: int | None = None,
    ) -> bool:
        """Отменяет задачу и очищает конфиденциальные данные."""
        async with SessionFactory() as session:
            async with session.begin():
                query = (
                    select(BookingJob)
                    .where(BookingJob.id == job_id)
                    .with_for_update()
                )

                if chat_id is not None:
                    query = query.where(BookingJob.chat_id == chat_id)

                result = await session.execute(query)
                job = result.scalar_one_or_none()

                if job is None:
                    return False

                if job.status == "order_created":
                    raise ValueError(
                        "Заказ уже создан. Его отмена должна выполняться "
                        "отдельно на сайте перевозчика."
                    )

                if job.status in {"submitting", "order_unknown"}:
                    raise ValueError(
                        "Нельзя отменить задачу до проверки результата "
                        "отправки заказа на сайте."
                    )

                if job.status in TERMINAL_STATUSES:
                    return False

                job.status = "cancelled"
                job.current_step = "cancelled"
                job.sensitive_payload = None
                job.lease_owner = None
                job.lease_until = None
                job.next_attempt_at = None
                job.completed_at = _utcnow()
                job.updated_at = _utcnow()

                return True

    async def expire_job(self, job_id: uuid.UUID) -> bool:
        """Помечает задачу истёкшей и очищает конфиденциальные данные."""
        async with SessionFactory() as session:
            async with session.begin():
                query = (
                    select(BookingJob)
                    .where(BookingJob.id == job_id)
                    .with_for_update()
                )

                result = await session.execute(query)
                job = result.scalar_one_or_none()

                if job is None:
                    return False

                if job.status == "order_created":
                    raise ValueError(
                        "Созданный заказ нельзя пометить как задачу бронирования."
                    )

                if job.status in {"submitting", "order_unknown"}:
                    raise ValueError(
                        "Нельзя завершить задачу до проверки результата "
                        "отправки заказа на сайте."
                    )

                if job.status in TERMINAL_STATUSES:
                    return False

                job.status = "expired"
                job.current_step = "expired"
                job.sensitive_payload = None
                job.lease_owner = None
                job.lease_until = None
                job.next_attempt_at = None
                job.completed_at = _utcnow()
                job.updated_at = _utcnow()

                return True


class AutomaticBookingWorker:
    """Фоновый исполнитель автозаказов, созданных мониторингом."""

    def __init__(self, bot, browser_service, monitor_service, *, check_interval: int = 15):
        self.bot = bot
        self.browser = browser_service
        self.monitor = monitor_service
        self.jobs = BookingJobsService()
        self.check_interval = max(5, int(check_interval))
        self.worker_id = f"monitor-booking-{uuid.uuid4()}"
        self._task: asyncio.Task | None = None
        self._stopping = False

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self._run(), name="automatic-booking-worker")

    async def shutdown(self) -> None:
        self._stopping = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self._process_due_monitor_resumes()
                jobs = await self.jobs.claim_monitor_jobs(self.worker_id)
                if not jobs:
                    await asyncio.sleep(self.check_interval)
                    continue
                for job in jobs:
                    if self._stopping:
                        break
                    await self._process(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Ошибка цикла автоматического бронирования")
                await asyncio.sleep(self.check_interval)

    async def _notify(self, chat_id: int, message: str, *, reply_markup=None) -> None:
        try:
            await self.bot.send_message(chat_id, message, reply_markup=reply_markup)
        except Exception:
            logger.exception("Не удалось отправить уведомление об автозаказе chat_id=%s", chat_id)

    async def _process_due_monitor_resumes(self) -> None:
        due = await self.jobs.claim_due_monitor_resumes()
        for item in due:
            job_id = item["job_id"]
            try:
                resumed = await self.monitor.resume_subscription_from_booking(
                    str(item["monitor_key"]), int(item["chat_id"])
                )
                await self.jobs.complete_monitor_resume(job_id, success=True)
                if resumed:
                    await self._notify(
                        int(item["chat_id"]),
                        "🔄 <b>Срок неоплаченного бронирования истёк.</b> "
                        "Мониторинг возобновлён — снова отслеживаю появление мест.",
                    )
            except Exception:
                logger.exception("Не удалось возобновить мониторинг после срока оплаты job_id=%s", job_id)
                await self.jobs.complete_monitor_resume(job_id, success=False)

    async def _retry(self, job, message: str) -> None:
        await self.jobs.update_job_state(
            job.id,
            status="waiting",
            current_step="waiting_for_inventory",
            last_error=message,
        )
        await self.jobs.release_lease(job.id, self.worker_id, retry_after_seconds=self.check_interval)

    async def _reconcile_existing_order(self, job: BookingJob, payload: dict | None) -> None:
        """После рестарта проверяет историю заказов; никогда не нажимает submit."""
        if not isinstance(payload, dict):
            if job.status == "submitting":
                await self.jobs.mark_order_unknown(
                    job.id,
                    error="Нет профиля для проверки уже отправленного заказа; повторная отправка запрещена.",
                    retry_after_seconds=300,
                )
            else:
                await self.jobs.release_lease(job.id, self.worker_id, retry_after_seconds=300)
            return

        login = str(payload.get("login") or "").strip()
        password = str(payload.get("password") or "")
        if not login or not password:
            await self.jobs.release_lease(job.id, self.worker_id, retry_after_seconds=120)
            return

        page = None
        try:
            page = await self.browser.open_auth_form(str(job.id), job.url)
            if await self.browser.detect_captcha(page):
                await self.jobs.release_lease(job.id, self.worker_id, retry_after_seconds=300)
                return
            auth = await self.browser.submit_credentials(str(job.id), login, password)
            if auth != "authorized":
                await self.jobs.release_lease(job.id, self.worker_id, retry_after_seconds=120)
                return

            # URL оплаты используется в обычном сценарии сайта этого проекта.
            await page.goto(
                "https://pass.rw.by/ru/order/payment/",
                wait_until="domcontentloaded",
                timeout=self.browser.page_timeout_ms,
            )
            await page.wait_for_timeout(1500)
            result = await self.browser.inspect_order_history(
                page,
                train_number=job.train_number,
                travel_date=job.travel_date,
            )
            if result.get("status") == "confirmed":
                confirmation_message = _booking_confirmation_message(
                    job,
                    payload,
                    result.get("order_number"),
                    result.get("order_expires_at"),
                )
                await self.jobs.mark_order_created(
                    job.id,
                    order_number=result.get("order_number"),
                    order_expires_at=result.get("order_expires_at"),
                )
                await self.monitor.finish_automatic_booking(job, "confirmed")
                delete_keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text="🗑 Удалить мониторинг",
                        callback_data=f"mon_del:{job.id}",
                    )
                ]])
                await self._notify(job.chat_id, confirmation_message, reply_markup=delete_keyboard)
                return

            # Отсутствие совпадения в истории не доказывает, что заказ не был создан.
            # Сохраняем order_unknown и проверяем позже; submit запрещён.
            if job.status == "submitting":
                await self.jobs.mark_order_unknown(
                    job.id,
                    error="После перезапуска проверена история заказов, но однозначное совпадение не найдено.",
                )
            else:
                await self.jobs.release_lease(job.id, self.worker_id, retry_after_seconds=120)
        except Exception:
            logger.exception("Не удалось сверить заказ на сайте job_id=%s", job.id)
            current = await self.jobs.get_job(job.id)
            if current and current.status == "submitting":
                await self.jobs.mark_order_unknown(
                    job.id,
                    error="Сверка после перезапуска не завершилась; повторная отправка запрещена.",
                )
            else:
                await self.jobs.release_lease(job.id, self.worker_id, retry_after_seconds=120)
        finally:
            try:
                await self.browser.close_session(str(job.id))
            except Exception:
                pass

    async def _process(self, job: BookingJob) -> None:
        job_id = str(job.id)
        payload = None
        try:
            payload = await self.jobs.get_sensitive_payload(job.id)
            if job.status in {"submitting", "order_unknown"}:
                await self._reconcile_existing_order(job, payload)
                return
            if not isinstance(payload, dict):
                await self.jobs.mark_failed(job.id, error="Не найден зашифрованный профиль пользователя.")
                await self.monitor.finish_automatic_booking(job, "failed")
                await self._notify(job.chat_id, "⚠️ Автобронирование остановлено: защищённый профиль не найден. Сохраните данные для pass.rw.by и запустите мониторинг заново.")
                return

            login = str(payload.get("login") or "").strip()
            password = str(payload.get("password") or "")
            passengers = payload.get("passengers") or []
            prefs = dict(job.preferences or {})
            count = int(prefs.get("tickets_count", 1))
            try:
                max_price = Decimal(str(prefs.get("max_price", "0")))
            except InvalidOperation:
                max_price = Decimal("0")

            if not login or not password or len(passengers) < count or max_price <= 0:
                await self.jobs.mark_failed(job.id, error="В профиле отсутствуют обязательные данные или не хватает пассажиров.")
                await self.monitor.finish_automatic_booking(job, "failed")
                await self._notify(job.chat_id, "⚠️ Автобронирование остановлено: проверьте сохранённые логин, пароль и данные всех пассажиров. Количество пассажиров должно совпадать с количеством билетов.")
                return

            page = await self.browser.open_auth_form(job_id, job.url)
            if await self.browser.detect_captcha(page):
                await self.jobs.mark_failed(job.id, error="На сайте обнаружена CAPTCHA; автоматизация остановлена.")
                await self.monitor.finish_automatic_booking(job, "captcha")
                await self._notify(job.chat_id, "🛑 На pass.rw.by появилась CAPTCHA. Автоматическое бронирование остановлено; решите проверку вручную и запустите мониторинг заново.")
                return

            auth_result = await self.browser.submit_credentials(job_id, login, password)
            if auth_result != "authorized":
                if auth_result == "auth_timeout":
                    await self._retry(job, "Не удалось подтвердить вход; повторная попытка безопасна, заказ ещё не отправлялся.")
                    return
                await self.jobs.mark_failed(job.id, error=f"Авторизация не пройдена: {auth_result}.")
                await self.monitor.finish_automatic_booking(job, "failed")
                await self._notify(job.chat_id, "⚠️ Автобронирование остановлено: сайт не подтвердил вход. Проверьте логин и пароль pass.rw.by.")
                return

            if await self.browser.detect_captcha(page):
                await self.jobs.mark_failed(job.id, error="CAPTCHA обнаружена после авторизации.")
                await self.monitor.finish_automatic_booking(job, "captcha")
                await self._notify(job.chat_id, "🛑 Сайт запросил CAPTCHA. Автоматическое бронирование остановлено.")
                return

            await self.browser.open_selected_train_places(job_id, job.train_data or {})
            available_types = await self.browser.get_available_car_types(job_id)
            available_by_code = {item.get("car_type"): item for item in available_types}

            grouped_fares: dict[str, dict[str, Any]] = {}
            for item in (job.train_data or {}).get("fare_options", []) or []:
                try:
                    price = Decimal(str(item.get("price")))
                    seats = int(item.get("seats", 0))
                except (InvalidOperation, TypeError, ValueError):
                    continue
                code = str(item.get("car_type") or "")
                if price > max_price or seats <= 0 or not code or code not in available_by_code:
                    continue
                current = grouped_fares.setdefault(
                    code,
                    {"price": price, "seats": 0},
                )
                current["price"] = min(current["price"], price)
                current["seats"] += seats

            candidates = sorted(
                (
                    (data["price"], code)
                    for code, data in grouped_fares.items()
                    if data["seats"] >= count
                ),
                key=lambda pair: pair[0],
            )

            selected_seats = []
            selected_type = None
            for price, code in candidates:
                await self.browser.select_car_type(job_id, code)
                carriages = await self.browser.scan_free_seats(job_id, "Любое")
                free = [
                    (carriage, seat)
                    for carriage in carriages
                    for seat in carriage.get("seats", [])
                ]
                if len(free) < count:
                    continue
                selected_type = code
                selected_seats = free[:count]
                break

            if not selected_seats:
                await self._retry(job, "Свободных мест подходящей категории и цены пока недостаточно.")
                return

            selected_ticket_details = []
            for carriage, seat in selected_seats:
                selected_result = await self.browser.click_selected_seat(
                    job_id,
                    carriage["panel_id"],
                    str(seat["number"]),
                    requested_position="Любое",
                )
                selected_ticket_details.append({
                    "carriage_number": str(
                        selected_result.get("carriage_number")
                        or carriage.get("carriage_number")
                        or ""
                    ),
                    "carriage_title": str(
                        selected_result.get("carriage") or carriage.get("title") or ""
                    ),
                    "seat_number": str(seat.get("number") or ""),
                })

            # Сохраняем только несекретные сведения о выбранных местах.
            # Паспортные данные и ФИО остаются в зашифрованном payload.
            updated_train_data = dict(job.train_data or {})
            selected_type_info = available_by_code.get(selected_type, {})
            updated_train_data["booking_selection"] = {
                "car_type": str(selected_type or ""),
                "car_type_name": str(
                    selected_type_info.get("name") or selected_type or ""
                ),
                "seats": selected_ticket_details,
            }
            await self.jobs.update_job_state(
                job.id,
                train_data=updated_train_data,
            )
            job.train_data = updated_train_data

            await self.browser.open_passenger_data_form(page)
            try:
                live_document_options = await self.browser.get_document_options(
                    page, passenger_index=1
                )
                if live_document_options:
                    await MonitorProfileService().save_document_options(
                        job.chat_id, live_document_options
                    )
            except Exception:
                logger.warning(
                    "Не удалось сохранить список типов документов из автоматического бронирования",
                    exc_info=True,
                )

            bedding_preference = bool(
                payload.get("bedding", passengers[0].get("bedding", False))
            )
            for index in range(1, count + 1):
                passenger = passengers[index - 1]
                document_type = str(passenger.get("document_type") or "").strip()
                result = await self.browser.prepare_passenger_form(
                    page,
                    passenger,
                    document_type=document_type,
                    bedding=bedding_preference,
                    passenger_index=index,
                )
                if not result.get("valid"):
                    raise RuntimeError("Форма одного из пассажиров не прошла проверку.")

            await self.browser.prepare_order_submission(page, count)

            # Критическая точка: до клика сохраняем submitting. После этого
            # повторная отправка запрещена даже при сетевом сбое/перезапуске.
            await self.jobs.mark_submitting(job.id, worker_id=self.worker_id)
            outcome = await self.browser.click_order_submit_once(
                page,
                expected_train_number=job.train_number,
                expected_travel_date=job.travel_date,
            )

            if outcome.get("status") == "confirmed":
                # Формируем сообщение до mark_order_created: этот метод
                # удаляет зашифрованные данные пассажиров из БД.
                confirmation_message = _booking_confirmation_message(
                    job,
                    payload,
                    outcome.get("order_number"),
                    outcome.get("order_expires_at"),
                )
                await self.jobs.mark_order_created(
                    job.id,
                    order_number=outcome.get("order_number"),
                    order_expires_at=outcome.get("order_expires_at"),
                )
                await self.monitor.finish_automatic_booking(job, "confirmed")
                delete_keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text="🗑 Удалить мониторинг",
                        callback_data=f"mon_del:{job.id}",
                    )
                ]])
                await self._notify(job.chat_id, confirmation_message, reply_markup=delete_keyboard)
                return

            if outcome.get("status") == "rejected":
                # Сайт явно сообщил, что заказ не создан: только теперь повтор допустим.
                await self.jobs.mark_order_rejected_for_retry(
                    job.id,
                    self.worker_id,
                    retry_after_seconds=self.check_interval,
                )
                return

            if outcome.get("status") == "captcha":
                await self.jobs.mark_order_unknown(job.id, error="После нажатия оформления обнаружена CAPTCHA; требуется ручная сверка заказа.")
                await self.monitor.finish_automatic_booking(job, "unknown")
                await self._notify(job.chat_id, "🛑 После отправки формы сайт показал CAPTCHA. Я не буду нажимать оформление повторно. Проверьте раздел заказов на pass.rw.by.")
                return

            await self.jobs.mark_order_unknown(
                job.id,
                error="Кнопка оформления была нажата, но сайт не предоставил однозначного подтверждения. Повторная отправка заблокирована.",
                retry_after_seconds=120,
            )
            await self.monitor.finish_automatic_booking(job, "unknown")
            await self._notify(job.chat_id, "⚠️ Кнопка оформления была нажата, но сайт не подтвердил и не отклонил заказ однозначно. Повторно отправлять его опасно — возможен дубль. Проверьте личный кабинет pass.rw.by; автоматический повтор заблокирован.")

        except asyncio.CancelledError:
            # Если status уже submitting, он останется таковым и не будет
            # повторно отправлен после рестарта.
            raise
        except Exception as exc:
            current = await self.jobs.get_job(job.id)
            if "Поезд найден, но pass.rw.by сейчас показывает" in str(exc) or "кнопка выбора мест пока недоступна" in str(exc):
                logger.warning(
                    "Автозаказ ожидает появления свободных мест: job_id=%s reason=%s",
                    job_id, str(exc),
                )
                if not current or current.status not in {"submitting", "order_unknown"}:
                    await self._retry(job, str(exc))
                    return
            else:
                logger.exception("Ошибка автозаказа job_id=%s", job_id)
            if current and current.status in {"submitting", "order_unknown"}:
                if current.status == "submitting":
                    await self.jobs.mark_order_unknown(
                        job.id,
                        error="Сбой после фиксации отправки; требуется сверка заказа на сайте.",
                        retry_after_seconds=120,
                    )
                await self.monitor.finish_automatic_booking(job, "unknown")
                await self._notify(job.chat_id, "⚠️ Во время отправки заказа произошёл сбой. Я не буду повторять отправку без проверки сайта, чтобы не создать дубль. Проверьте раздел заказов pass.rw.by.")
            else:
                await self._retry(job, f"Техническая ошибка до отправки заказа: {type(exc).__name__}.")
        finally:
            # Не оставляем открытые сессии и персональные данные в браузере.
            try:
                await self.browser.close_session(job_id)
            except Exception:
                logger.warning("Не удалось закрыть браузер автозаказа job_id=%s", job_id)
            payload = None
