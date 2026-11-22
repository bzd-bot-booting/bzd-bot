
import asyncio
import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from html import escape
from typing import Any

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
)
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from sqlalchemy import select, func, text, update
from sqlalchemy.dialects.postgresql import insert

from app.config import Settings
from app.services.rw_parser import RWParser, ScheduleUnavailable, is_departure_time_passed
from app.database.engine import SessionFactory, engine
from app.services.booking_jobs import BookingJobsService
from app.services.monitor_profiles import MonitorProfileService
from app.database.models import (
    Monitor as DBMonitor,
    Subscription as DBSubscription,
    Notification as DBNotification,
)

logger = logging.getLogger(__name__)


def _to_price(value: Any) -> Decimal:
    """Преобразует цену в Decimal с точностью до копеек."""
    price = Decimal(str(value).replace(",", "."))
    if not price.is_finite() or price <= 0:
        raise ValueError("Цена должна быть положительным числом.")
    return price.quantize(Decimal("0.01"))


def _fare_key(option: dict) -> str:
    """Создаёт стабильный ключ тарифа: категория, вагон, цена."""
    name = str(option.get("name") or "Места").strip()
    car_type = str(
        option.get("car_type")
        or option.get("type")
        or ""
    ).strip()
    price = _to_price(option["price"])

    return json.dumps(
        [name, car_type, format(price, ".2f")],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _plan_supports_single_car_type(plan: list[dict], tickets_count: int) -> bool:
    """Проверяет, что выделенный FIFO-план содержит нужное число мест одного типа."""
    seats_by_type: dict[str, int] = {}
    for item in plan:
        code = str(item.get("car_type") or "").strip()
        if not code:
            continue
        seats_by_type[code] = seats_by_type.get(code, 0) + max(
            0, int(item.get("seats", 0) or 0)
        )
    return max(seats_by_type.values(), default=0) >= int(tickets_count)


def _fare_inventory(train: dict) -> dict[str, dict]:
    """
    Объединяет одинаковые тарифы.
    Результат содержит ключ, название, тип вагона, цену и количество.
    """
    inventory: dict[str, dict] = {}

    for option in train.get("fare_options", []) or []:
        try:
            price = _to_price(option.get("price"))
            seats = max(0, int(option.get("seats", 0) or 0))
            if seats <= 0:
                continue

            key = _fare_key(option)
            name = str(option.get("name") or "Места").strip()
            car_type = str(
                option.get("car_type")
                or option.get("type")
                or ""
            ).strip()

            if key not in inventory:
                inventory[key] = {
                    "key": key,
                    "name": name,
                    "car_type": car_type,
                    "price": price,
                    "seats": 0,
                }

            inventory[key]["seats"] += seats

        except (KeyError, TypeError, ValueError, InvalidOperation):
            logger.warning(
                "Пропущен некорректный тариф поезда %s: %r",
                train.get("number"),
                option,
            )

    return inventory


@dataclass
class Subscription:
    chat_id: int
    tickets_count: int
    created_order: int
    max_price: Decimal


@dataclass
class Monitor:
    key: str
    from_name: str
    to_name: str
    travel_date: str
    train: dict[str, Any]
    url: str

    queue: list[Subscription] = field(default_factory=list)
    task: asyncio.Task | None = None
    allocated_seats: int = 0
    allocated_fares: dict[str, int] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class MonitorService:
    """Мониторинги поездов с PostgreSQL и FIFO-очередью."""

    def __init__(
        self,
        bot: Bot,
        parser: RWParser,
        settings: Settings,
    ):
        self.bot = bot
        self.parser = parser
        self.settings = settings
        self.monitors: dict[str, Monitor] = {}
        self._lock = asyncio.Lock()
        self._shutting_down = False
        self.auto_booking_enabled = True

    # =========================================================
    # Блокировки PostgreSQL
    # =========================================================

    async def _lock_queue(self, session, key: str) -> None:
        await session.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended(:lock_name, 0)"
                ")"
            ),
            {"lock_name": f"queue:{key}"},
        )

    async def _acquire_monitor_lock(self, key: str):
        connection = await engine.connect()
        lock_name = f"monitor:{key}"
        acquired = False

        try:
            while not self._shutting_down:
                result = await connection.execute(
                    text(
                        "SELECT pg_try_advisory_lock("
                        "hashtextextended(:lock_name, 0)"
                        ")"
                    ),
                    {"lock_name": lock_name},
                )

                acquired = bool(result.scalar())
                await connection.commit()

                if acquired:
                    logger.info(
                        "Получена блокировка мониторинга: %s",
                        key,
                    )
                    return connection

                await asyncio.sleep(
                    max(5, self.settings.check_interval)
                )

            return None

        except BaseException:
            if acquired:
                try:
                    await connection.execute(
                        text(
                            "SELECT pg_advisory_unlock("
                            "hashtextextended(:lock_name, 0)"
                            ")"
                        ),
                        {"lock_name": lock_name},
                    )
                    await connection.commit()
                except Exception:
                    logger.exception(
                        "Не удалось освободить блокировку %s",
                        key,
                    )

            await connection.close()
            raise

        finally:
            if not acquired:
                await connection.close()

    async def _release_monitor_lock(
        self,
        connection,
        key: str,
    ) -> None:
        if connection is None:
            return

        try:
            await connection.execute(
                text(
                    "SELECT pg_advisory_unlock("
                    "hashtextextended(:lock_name, 0)"
                    ")"
                ),
                {"lock_name": f"monitor:{key}"},
            )
            await connection.commit()

            logger.info(
                "Блокировка мониторинга освобождена: %s",
                key,
            )

        except Exception:
            logger.exception(
                "Ошибка освобождения блокировки: %s",
                key,
            )

        finally:
            await connection.close()

    # =========================================================
    # Очередь
    # =========================================================

    async def _load_queue(self, key: str) -> list[Subscription]:
        async with SessionFactory() as session:
            result = await session.scalars(
                select(DBSubscription)
                .where(
                    DBSubscription.monitor_key == key,
                    DBSubscription.is_active.is_(True),
                    DBSubscription.max_price.is_not(None),
                )
                .order_by(DBSubscription.created_order)
            )
            rows = result.all()

        return [
            Subscription(
                chat_id=row.chat_id,
                tickets_count=row.tickets_count,
                created_order=row.created_order,
                max_price=Decimal(row.max_price).quantize(
                    Decimal("0.01")
                ),
            )
            for row in rows
        ]

    async def _refresh_queue(self, monitor: Monitor) -> None:
        monitor.queue = await self._load_queue(monitor.key)

    async def _ensure_task(self, monitor: Monitor) -> None:
        if self._shutting_down:
            return

        if monitor.task is None or monitor.task.done():
            monitor.task = asyncio.create_task(
                self._run(monitor),
                name=f"monitor:{monitor.key}",
            )

    # =========================================================
    # Создание или обновление подписки
    # =========================================================

    async def subscribe(
        self,
        key: str,
        from_name: str,
        to_name: str,
        travel_date: str,
        train: dict,
        url: str,
        chat_id: int,
        tickets_count: int,
        max_price: Decimal | str | float,
    ) -> tuple[int, bool]:

        if self._shutting_down:
            raise RuntimeError(
                "Сервис мониторинга завершает работу."
            )

        if tickets_count < 1:
            raise ValueError("Количество билетов должно быть больше нуля.")

        price = _to_price(max_price)

        async with self._lock:
            async with SessionFactory() as session:
                async with session.begin():
                    await self._lock_queue(session, key)

                    db_monitor = await session.get(DBMonitor, key)
                    created = db_monitor is None

                    if db_monitor is None:
                        db_monitor = DBMonitor(
                            key=key,
                            from_name=from_name,
                            to_name=to_name,
                            travel_date=travel_date,
                            train=train,
                            url=url,
                            allocated_seats=0,
                            allocated_fares={},
                            is_active=True,
                        )
                        session.add(db_monitor)
                    else:
                        db_monitor.from_name = from_name
                        db_monitor.to_name = to_name
                        db_monitor.travel_date = travel_date
                        db_monitor.train = train
                        db_monitor.url = url
                        db_monitor.is_active = True

                    existing = await session.scalar(
                        select(DBSubscription).where(
                            DBSubscription.monitor_key == key,
                            DBSubscription.chat_id == chat_id,
                        )
                    )

                    if existing is not None and existing.is_active:
                        # Активная заявка остаётся на прежнем месте FIFO.
                        existing.tickets_count = tickets_count
                        existing.max_price = price

                        position = await session.scalar(
                            select(func.count())
                            .select_from(DBSubscription)
                            .where(
                                DBSubscription.monitor_key == key,
                                DBSubscription.is_active.is_(True),
                                DBSubscription.created_order
                                <= existing.created_order,
                            )
                        )

                    else:
                        max_order = await session.scalar(
                            select(func.max(DBSubscription.created_order))
                            .where(DBSubscription.monitor_key == key)
                        )
                        next_order = (max_order or 0) + 1

                        if existing is None:
                            existing = DBSubscription(
                                monitor_key=key,
                                chat_id=chat_id,
                                tickets_count=tickets_count,
                                max_price=price,
                                created_order=next_order,
                                is_active=True,
                            )
                            session.add(existing)
                        else:
                            # Повторная заявка после отмены — в конец FIFO.
                            existing.tickets_count = tickets_count
                            existing.max_price = price
                            existing.created_order = next_order
                            existing.is_active = True

                        position = await session.scalar(
                            select(func.count())
                            .select_from(DBSubscription)
                            .where(
                                DBSubscription.monitor_key == key,
                                DBSubscription.is_active.is_(True),
                            )
                        )

                    position = int(position or 1)
                    allocated_seats = int(
                        db_monitor.allocated_seats or 0
                    )
                    allocated_fares = dict(
                        db_monitor.allocated_fares or {}
                    )

            monitor = self.monitors.get(key)

            if monitor is None:
                monitor = Monitor(
                    key=key,
                    from_name=from_name,
                    to_name=to_name,
                    travel_date=travel_date,
                    train=train,
                    url=url,
                    allocated_seats=allocated_seats,
                    allocated_fares=allocated_fares,
                )
                self.monitors[key] = monitor
            else:
                monitor.from_name = from_name
                monitor.to_name = to_name
                monitor.travel_date = travel_date
                monitor.train = train
                monitor.url = url
                monitor.allocated_seats = allocated_seats
                monitor.allocated_fares = allocated_fares

            await self._refresh_queue(monitor)
            await self._ensure_task(monitor)

            logger.info(
                "Подписка сохранена: train=%s chat_id=%s "
                "position=%s max_price=%s",
                train.get("number"),
                chat_id,
                position,
                price,
            )

            return position, created

    # =========================================================
    # Отмена подписок
    # =========================================================

    async def unsubscribe_user(self, chat_id: int) -> int:
        async with self._lock:
            async with SessionFactory() as session:
                async with session.begin():
                    result = await session.scalars(
                        select(DBSubscription).where(
                            DBSubscription.chat_id == chat_id,
                            DBSubscription.is_active.is_(True),
                        )
                    )
                    subscriptions = result.all()

                    keys = sorted({
                        sub.monitor_key for sub in subscriptions
                    })

                    for key in keys:
                        await self._lock_queue(session, key)

                    for sub in subscriptions:
                        sub.is_active = False

                    for key in keys:
                        active_count = await session.scalar(
                            select(func.count())
                            .select_from(DBSubscription)
                            .where(
                                DBSubscription.monitor_key == key,
                                DBSubscription.is_active.is_(True),
                            )
                        )

                        if not active_count:
                            db_monitor = await session.get(
                                DBMonitor,
                                key,
                            )
                            if db_monitor:
                                db_monitor.is_active = False

            removed = len(subscriptions)

            for key in keys:
                monitor = self.monitors.get(key)
                if monitor is None:
                    continue

                await self._refresh_queue(monitor)

                if not monitor.queue:
                    if monitor.task and not monitor.task.done():
                        monitor.task.cancel()
                    self.monitors.pop(key, None)

            logger.info(
                "Пользователь %s отменил %s подписок",
                chat_id,
                removed,
            )
            return removed

    async def unsubscribe_from(
        self,
        key: str,
        chat_id: int,
    ) -> bool:
        async with self._lock:
            async with SessionFactory() as session:
                async with session.begin():
                    await self._lock_queue(session, key)

                    subscription = await session.scalar(
                        select(DBSubscription).where(
                            DBSubscription.monitor_key == key,
                            DBSubscription.chat_id == chat_id,
                            DBSubscription.is_active.is_(True),
                        )
                    )

                    if subscription is None:
                        return False

                    subscription.is_active = False

                    active_count = await session.scalar(
                        select(func.count())
                        .select_from(DBSubscription)
                        .where(
                            DBSubscription.monitor_key == key,
                            DBSubscription.is_active.is_(True),
                        )
                    )

                    if not active_count:
                        db_monitor = await session.get(DBMonitor, key)
                        if db_monitor:
                            db_monitor.is_active = False

            monitor = self.monitors.get(key)
            if monitor:
                await self._refresh_queue(monitor)

                if not monitor.queue:
                    if monitor.task and not monitor.task.done():
                        monitor.task.cancel()
                    self.monitors.pop(key, None)

            return True

    async def delete_subscription(self, key: str, chat_id: int) -> bool:
        """Физически удаляет подписку только указанного пользователя."""
        async with self._lock:
            async with SessionFactory() as session:
                async with session.begin():
                    await self._lock_queue(session, key)

                    subscription = await session.scalar(
                        select(DBSubscription).where(
                            DBSubscription.monitor_key == key,
                            DBSubscription.chat_id == chat_id,
                        )
                    )

                    if subscription is None:
                        return False

                    await session.delete(subscription)

                    active_count = await session.scalar(
                        select(func.count())
                        .select_from(DBSubscription)
                        .where(
                            DBSubscription.monitor_key == key,
                            DBSubscription.is_active.is_(True),
                        )
                    )

                    if not active_count:
                        db_monitor = await session.get(DBMonitor, key)
                        if db_monitor:
                            db_monitor.is_active = False

            monitor = self.monitors.get(key)
            if monitor:
                await self._refresh_queue(monitor)

                if not monitor.queue:
                    if monitor.task and not monitor.task.done():
                        monitor.task.cancel()
                    self.monitors.pop(key, None)

            return True

    async def queue_position(
        self,
        key: str,
        chat_id: int,
    ) -> int | None:
        queue = await self._load_queue(key)

        for index, subscription in enumerate(queue, start=1):
            if subscription.chat_id == chat_id:
                return index

        return None

    async def _deactivate_subscription(
        self,
        monitor: Monitor,
        chat_id: int,
    ) -> None:
        async with SessionFactory() as session:
            async with session.begin():
                await self._lock_queue(session, monitor.key)

                subscription = await session.scalar(
                    select(DBSubscription).where(
                        DBSubscription.monitor_key == monitor.key,
                        DBSubscription.chat_id == chat_id,
                        DBSubscription.is_active.is_(True),
                    )
                )

                if subscription:
                    subscription.is_active = False

                active_count = await session.scalar(
                    select(func.count())
                    .select_from(DBSubscription)
                    .where(
                        DBSubscription.monitor_key == monitor.key,
                        DBSubscription.is_active.is_(True),
                    )
                )

                if not active_count:
                    db_monitor = await session.get(
                        DBMonitor,
                        monitor.key,
                    )
                    if db_monitor:
                        db_monitor.is_active = False

    async def resume_subscription_from_booking(self, monitor_key: str, chat_id: int) -> bool:
        """Повторно включает подписку после истечения срока неоплаченного заказа."""
        async with SessionFactory() as session:
            db_monitor = await session.get(DBMonitor, monitor_key)
            subscription = await session.scalar(
                select(DBSubscription).where(
                    DBSubscription.monitor_key == monitor_key,
                    DBSubscription.chat_id == chat_id,
                )
            )
            if db_monitor is None or subscription is None or subscription.max_price is None:
                return False
            if subscription.is_active:
                return True
            values = {
                "from_name": db_monitor.from_name,
                "to_name": db_monitor.to_name,
                "travel_date": db_monitor.travel_date,
                "train": dict(db_monitor.train or {}),
                "url": db_monitor.url,
                "tickets_count": int(subscription.tickets_count),
                "max_price": Decimal(subscription.max_price),
            }
        await self.subscribe(
            key=monitor_key,
            from_name=values["from_name"],
            to_name=values["to_name"],
            travel_date=values["travel_date"],
            train=values["train"],
            url=values["url"],
            chat_id=chat_id,
            tickets_count=values["tickets_count"],
            max_price=values["max_price"],
        )
        logger.info(
            "Мониторинг автоматически возобновлён после срока оплаты: key=%s chat_id=%s",
            monitor_key, chat_id,
        )
        return True

    async def finish_automatic_booking(self, job, outcome: str) -> None:
        """Останавливает конкретную подписку после подтверждения/неопределённости."""
        preferences = dict(job.preferences or {})
        monitor_key = preferences.get("monitor_key")
        if not monitor_key:
            return

        async with SessionFactory() as session:
            async with session.begin():
                await self._lock_queue(session, str(monitor_key))
                subscription = await session.scalar(
                    select(DBSubscription).where(
                        DBSubscription.monitor_key == str(monitor_key),
                        DBSubscription.chat_id == job.chat_id,
                        DBSubscription.is_active.is_(True),
                    ).with_for_update()
                )
                if subscription:
                    subscription.is_active = False

                active_count = await session.scalar(
                    select(func.count()).select_from(DBSubscription).where(
                        DBSubscription.monitor_key == str(monitor_key),
                        DBSubscription.is_active.is_(True),
                    )
                )
                if not active_count:
                    db_monitor = await session.get(DBMonitor, str(monitor_key))
                    if db_monitor:
                        db_monitor.is_active = False

        monitor = self.monitors.get(str(monitor_key))
        if monitor is not None:
            await self._refresh_queue(monitor)
            if not monitor.queue:
                if monitor.task and not monitor.task.done():
                    monitor.task.cancel()
                self.monitors.pop(str(monitor_key), None)

    async def _set_notification_status(
        self,
        idempotency_key: str,
        status: str,
    ) -> None:
        async with SessionFactory() as session:
            async with session.begin():
                notification = await session.scalar(
                    select(DBNotification).where(
                        DBNotification.idempotency_key == idempotency_key
                    )
                )
                if notification:
                    notification.status = status

    # =========================================================
    # Распределение тарифов и отправка уведомлений
    # =========================================================

    async def _notify_available(
        self,
        monitor: Monitor,
        train: dict,
        inventory: dict[str, dict],
    ) -> int:
        """
        Распределяет места по тарифам в строгом FIFO.

        Учитывает индивидуальный max_price каждой подписки.
        Уже распределённые места никогда не возвращаются в пул.
        """

        async with monitor.lock:
            await self._refresh_queue(monitor)

            used_total = 0

            while monitor.queue:
                first = monitor.queue[0]

                # Синхронизируем накопленное распределение с БД.
                async with SessionFactory() as session:
                    db_monitor = await session.get(
                        DBMonitor,
                        monitor.key,
                    )
                    allocated = (
                        dict(db_monitor.allocated_fares or {})
                        if db_monitor
                        else {}
                    )

                monitor.allocated_fares = allocated
                monitor.allocated_seats = sum(
                    int(value) for value in allocated.values()
                )

                # Подбираем тарифы по возрастанию цены.
                eligible = sorted(
                    (
                        option
                        for option in inventory.values()
                        if option["price"] <= first.max_price
                    ),
                    key=lambda option: (
                        option["price"],
                        option["name"],
                        option["car_type"],
                    ),
                )

                plan: list[dict] = []
                needed = first.tickets_count

                for option in eligible:
                    key = option["key"]
                    current = int(option["seats"])
                    already_allocated = int(allocated.get(key, 0))

                    available = max(
                        0,
                        current - already_allocated,
                    )

                    if available <= 0:
                        continue

                    take = min(needed, available)

                    plan.append({
                        "key": key,
                        "name": option["name"],
                        "car_type": option["car_type"],
                        "price": option["price"],
                        "seats": take,
                    })

                    needed -= take

                    if needed == 0:
                        break

                # Строгий FIFO: не пропускаем первую заявку,
                # даже если следующая могла бы получить места.
                if needed > 0:
                    logger.info(
                        "FIFO ожидает: поезд=%s chat_id=%s "
                        "нужно=%s, доступно=%s, лимит=%s",
                        train.get("number"),
                        first.chat_id,
                        first.tickets_count,
                        first.tickets_count - needed,
                        first.max_price,
                    )
                    break

                # Автоматическое бронирование: вместо уведомления создаём
                # долговременную job с зашифрованным профилем. Заявка остаётся
                # активной до подтверждения заказа или ручной сверки.
                if self.auto_booking_enabled:
                    # Браузерный сценарий оформляет один заказ с одним типом
                    # вагона; не запускаем job, если требуемое число мест
                    # доступно только суммарно в разных типах.
                    # Проверяем именно выделенный FIFO-план: в нём уже учтены
                    # индивидуальный лимит цены и ранее распределённые места.
                    if not _plan_supports_single_car_type(
                        plan, first.tickets_count
                    ):
                        break

                    try:
                        profile = await MonitorProfileService().get(first.chat_id)
                    except Exception:
                        logger.exception("Не удалось прочитать профиль мониторинга chat_id=%s", first.chat_id)
                        profile = None

                    if not profile or len(profile.get("passengers", [])) < first.tickets_count:
                        await self.bot.send_message(
                            first.chat_id,
                            "⚠️ Для автоматического бронирования не хватает сохранённых данных. "
                            "Сначала выполните обычный вход на pass.rw.by и заполните данные всех пассажиров, "
                            "затем запустите мониторинг заново.",
                        )
                        await self._deactivate_subscription(monitor, first.chat_id)
                        await self._refresh_queue(monitor)
                        continue

                    job, created = await BookingJobsService().create_monitor_job_once(
                        chat_id=first.chat_id,
                        from_name=monitor.from_name,
                        to_name=monitor.to_name,
                        travel_date=monitor.travel_date,
                        train_number=str(train.get("number", monitor.train.get("number", ""))),
                        url=monitor.url,
                        train_data=train,
                        monitor_key=monitor.key,
                        subscription_order=first.created_order,
                        tickets_count=first.tickets_count,
                        max_price=first.max_price,
                        sensitive_data=profile,
                    )
                    logger.info(
                        "Автозаказ %s для chat_id=%s: job_id=%s status=%s",
                        "создан" if created else "уже существует",
                        first.chat_id,
                        job.id,
                        job.status,
                    )

                    # Отправляем уведомление только при фактическом создании
                    # новой job. Повторные циклы мониторинга не дублируют его.
                    if created:
                        fare_lines = []
                        for item in plan:
                            fare_label = escape(str(item.get("name") or "Места"))
                            car_type = str(item.get("car_type") or "").strip()
                            if car_type:
                                fare_label += f" ({escape(car_type)})"
                            fare_lines.append(
                                f"• {fare_label}: <b>{int(item['seats'])}</b> шт. "
                                f"по <b>{Decimal(str(item['price'])):.2f} BYN</b>"
                            )

                        auto_message = (
                            "🚨 <b>Мониторинг нашёл подходящие места!</b>\n\n"
                            f"🚆 Поезд: <b>№ {escape(str(train.get('number') or monitor.train.get('number') or '—'))}</b>\n"
                            f"📍 Маршрут: <b>{escape(monitor.from_name)}</b> → "
                            f"<b>{escape(monitor.to_name)}</b>\n"
                            f"📅 Дата: <b>{escape(str(monitor.travel_date))}</b>\n"
                            f"🕒 Отправление: <b>{escape(str(train.get('time') or '—'))}</b>\n"
                            f"🕒 Прибытие: <b>{escape(str(train.get('arr_time') or '—'))}</b>\n\n"
                            "🎟 <b>Выделенные места:</b>\n"
                            + "\n".join(fare_lines)
                            + "\n\n⏳ <b>Запущено автоматическое бронирование.</b>\n"
                            "Места ещё не считаются забронированными. Я пришлю отдельное сообщение, "
                            "когда сайт подтвердит оформление заказа."
                        )
                        try:
                            await self.bot.send_message(
                                first.chat_id,
                                auto_message,
                                reply_markup=InlineKeyboardMarkup(
                                    inline_keyboard=[[
                                        InlineKeyboardButton(
                                            text="Открыть поезд на pass.rw.by",
                                            url=monitor.url,
                                        )
                                    ]]
                                ),
                            )
                        except Exception:
                            logger.exception(
                                "Не удалось отправить уведомление о найденных местах "
                                "chat_id=%s job_id=%s",
                                first.chat_id,
                                job.id,
                            )

                    if not created and job.status in {
                        "submitting", "order_unknown", "order_created", "failed"
                    }:
                        # Восстановление после падения между обновлением job
                        # и отключением подписки: не запускаем новый заказ.
                        await self._deactivate_subscription(monitor, first.chat_id)
                        await self._refresh_queue(monitor)
                    # Не обрабатываем повторно ту же заявку в этом цикле.
                    return used_total

                idempotency_key = (
                    f"{monitor.key}:"
                    f"{first.chat_id}:"
                    f"{first.created_order}"
                )

                allocation_lines = []
                for item in plan:
                    label = escape(item["name"])
                    if item["car_type"]:
                        label += f" ({escape(item['car_type'])})"

                    allocation_lines.append(
                        f"• {label}: <b>{item['seats']}</b> шт. "
                        f"по <b>{item['price']:.2f}</b> BYN"
                    )

                message_text = (
                    f"🚨 <b>На поезд № {escape(str(train.get('number', '?')))} "
                    "появились подходящие места!</b>\n\n"
                    f"📍 Маршрут: <b>{escape(monitor.from_name)}</b> → "
                    f"<b>{escape(monitor.to_name)}</b>\n"
                    f"📅 Дата: <b>{escape(str(monitor.travel_date))}</b>\n"
                    f"🕒 Отправление: <b>{escape(str(train.get('time', '—')))}</b>\n"
                    f"🕒 Прибытие: <b>{escape(str(train.get('arr_time', '—')))}</b>\n\n"
                    f"🎟 Выделено по вашей заявке:\n"
                    + "\n".join(allocation_lines)
                    + f"\n\nВсего выделено: <b>{first.tickets_count}</b> "
                    f"из требуемых билетов.\n"
                    f"💰 Ваш лимит: <b>{first.max_price:.2f} BYN</b> "
                    "за билет.\n\n"
                    "Перейдите на сайт и проверьте наличие ещё раз. "
                    "Уведомление не резервирует места."
                )

                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="🎫 Перейти к выбору мест",
                                url=monitor.url,
                            )
                        ]
                    ]
                )

                should_send = False
                already_sent = False
                subscription_missing = False

                # Создаём или повторно используем idempotency-запись.
                async with SessionFactory() as session:
                    async with session.begin():
                        await self._lock_queue(session, monitor.key)

                        subscription = await session.scalar(
                            select(DBSubscription).where(
                                DBSubscription.monitor_key == monitor.key,
                                DBSubscription.chat_id == first.chat_id,
                                DBSubscription.is_active.is_(True),
                            )
                        )

                        if subscription is None:
                            subscription_missing = True
                        else:
                            notification = await session.scalar(
                                select(DBNotification).where(
                                    DBNotification.idempotency_key
                                    == idempotency_key
                                )
                            )

                            if notification is not None:
                                if notification.status == "sent":
                                    subscription.is_active = False
                                    already_sent = True
                                else:
                                    notification.status = "pending"
                                    should_send = True
                            else:
                                await session.execute(
                                    insert(DBNotification)
                                    .values(
                                        monitor_key=monitor.key,
                                        chat_id=first.chat_id,
                                        event_type="seats_available",
                                        idempotency_key=idempotency_key,
                                        status="pending",
                                    )
                                    .on_conflict_do_nothing(
                                        index_elements=["idempotency_key"]
                                    )
                                )

                                notification = await session.scalar(
                                    select(DBNotification).where(
                                        DBNotification.idempotency_key
                                        == idempotency_key
                                    )
                                )

                                if notification:
                                    if notification.status == "sent":
                                        subscription.is_active = False
                                        already_sent = True
                                    else:
                                        notification.status = "pending"
                                        should_send = True

                if already_sent or subscription_missing or not should_send:
                    await self._refresh_queue(monitor)
                    continue

                # Отправляем сообщение.
                try:
                    await self.bot.send_message(
                        first.chat_id,
                        message_text,
                        reply_markup=keyboard,
                    )

                except TelegramForbiddenError:
                    logger.warning(
                        "Пользователь %s заблокировал бота",
                        first.chat_id,
                    )
                    await self._deactivate_subscription(
                        monitor,
                        first.chat_id,
                    )
                    await self._set_notification_status(
                        idempotency_key,
                        "failed",
                    )
                    await self._refresh_queue(monitor)
                    continue

                except TelegramBadRequest:
                    logger.exception(
                        "Telegram отклонил уведомление chat_id=%s",
                        first.chat_id,
                    )
                    await self._deactivate_subscription(
                        monitor,
                        first.chat_id,
                    )
                    await self._set_notification_status(
                        idempotency_key,
                        "failed",
                    )
                    await self._refresh_queue(monitor)
                    continue

                except TelegramNetworkError:
                    logger.exception(
                        "Сетевая ошибка отправки уведомления chat_id=%s",
                        first.chat_id,
                    )
                    await self._set_notification_status(
                        idempotency_key,
                        "failed",
                    )
                    # Неизвестно, принял ли Telegram сообщение.
                    # Не переходим к следующей заявке.
                    break

                except Exception:
                    logger.exception(
                        "Ошибка отправки уведомления chat_id=%s",
                        first.chat_id,
                    )
                    await self._set_notification_status(
                        idempotency_key,
                        "failed",
                    )
                    break

                # Telegram подтвердил отправку.
                # Фиксируем notification и распределение в одной транзакции.
                async with SessionFactory() as session:
                    async with session.begin():
                        await self._lock_queue(session, monitor.key)

                        notification = await session.scalar(
                            select(DBNotification).where(
                                DBNotification.idempotency_key
                                == idempotency_key
                            )
                        )

                        db_monitor = await session.get(
                            DBMonitor,
                            monitor.key,
                        )

                        if notification is None or db_monitor is None:
                            logger.error(
                                "Не найдена запись уведомления или "
                                "мониторинга после отправки: %s",
                                idempotency_key,
                            )
                            break

                        if notification.status != "sent":
                            current_allocated = dict(
                                db_monitor.allocated_fares or {}
                            )

                            for item in plan:
                                key = item["key"]
                                current_allocated[key] = (
                                    int(current_allocated.get(key, 0))
                                    + int(item["seats"])
                                )

                            db_monitor.allocated_fares = current_allocated
                            db_monitor.allocated_seats = sum(
                                int(value)
                                for value in current_allocated.values()
                            )
                            notification.status = "sent"

                            subscription = await session.scalar(
                                select(DBSubscription).where(
                                    DBSubscription.monitor_key == monitor.key,
                                    DBSubscription.chat_id == first.chat_id,
                                    DBSubscription.is_active.is_(True),
                                )
                            )

                            if subscription:
                                subscription.is_active = False

                            active_count = await session.scalar(
                                select(func.count())
                                .select_from(DBSubscription)
                                .where(
                                    DBSubscription.monitor_key == monitor.key,
                                    DBSubscription.is_active.is_(True),
                                )
                            )

                            if not active_count:
                                db_monitor.is_active = False

                            monitor.allocated_fares = current_allocated
                            monitor.allocated_seats = int(
                                db_monitor.allocated_seats
                            )

                used_total += first.tickets_count

                logger.info(
                    "Уведомление отправлено: chat_id=%s train=%s "
                    "tickets=%s allocated_fares=%s",
                    first.chat_id,
                    train.get("number"),
                    first.tickets_count,
                    plan,
                )

                await self._refresh_queue(monitor)

            return used_total

    # =========================================================
    # Завершение мониторинга
    # =========================================================

    @staticmethod
    def _finish_event_type(message_text: str) -> str:
        lowered = message_text.casefold()
        if "отправился" in lowered:
            return "monitor_finished_departed"
        if "больше не найден" in lowered:
            return "monitor_finished_schedule_missing"
        return "monitor_finished_technical_error"

    @staticmethod
    def _finish_message(event_type: str, train_number: str) -> str:
        if event_type == "monitor_finished_departed":
            return f"🛑 Поезд {train_number} отправился. Мониторинг остановлен."
        if event_type == "monitor_finished_schedule_missing":
            return (
                f"⚠️ Поезд {train_number} больше не найден в расписании. "
                "Мониторинг остановлен."
            )
        return (
            "⚠️ Мониторинг остановлен из-за технической ошибки. "
            "Попробуйте запустить поиск заново через /book."
        )

    async def _finish(
        self,
        monitor: Monitor,
        message_text: str,
    ) -> None:
        """Атомарно завершает подписки и ставит Telegram-уведомления в очередь.

        Используется существующая таблица notifications с уникальным
        idempotency_key; дополнительная миграция БД не требуется.
        """
        await self._refresh_queue(monitor)
        event_type = self._finish_event_type(message_text)
        pending: list[tuple[int, str]] = []

        logger.info(
            "Завершение мониторинга: key=%s active_subscriptions=%s event=%s",
            monitor.key, len(monitor.queue), event_type,
            extra={"event": "monitor_finish", "monitor_key": monitor.key},
        )

        # Создание заданий уведомления и деактивация подписок выполняются
        # в одной транзакции. Повторный вызов не создаст новые задания.
        async with SessionFactory() as session:
            async with session.begin():
                await self._lock_queue(session, monitor.key)

                result = await session.scalars(
                    select(DBSubscription).where(
                        DBSubscription.monitor_key == monitor.key,
                        DBSubscription.is_active.is_(True),
                    )
                )
                subscriptions = result.all()

                for subscription in subscriptions:
                    idempotency_key = (
                        f"monitor_finish:{monitor.key}:{subscription.chat_id}:"
                        f"{subscription.created_order}"
                    )
                    await session.execute(
                        insert(DBNotification)
                        .values(
                            monitor_key=monitor.key,
                            chat_id=subscription.chat_id,
                            event_type=event_type,
                            idempotency_key=idempotency_key,
                            status="pending",
                        )
                        .on_conflict_do_nothing(
                            index_elements=["idempotency_key"]
                        )
                    )

                    notification = await session.scalar(
                        select(DBNotification).where(
                            DBNotification.idempotency_key == idempotency_key
                        )
                    )
                    if notification and notification.status != "sent":
                        # Если причина изменилась при повторном вызове,
                        # сохраняем актуальный тип события.
                        notification.event_type = event_type
                        notification.status = "pending"
                        pending.append((subscription.chat_id, idempotency_key))

                await session.execute(
                    update(DBSubscription)
                    .where(
                        DBSubscription.monitor_key == monitor.key,
                        DBSubscription.is_active.is_(True),
                    )
                    .values(is_active=False)
                )

                db_monitor = await session.get(DBMonitor, monitor.key)
                if db_monitor:
                    db_monitor.is_active = False

        monitor.queue.clear()

        for chat_id, idempotency_key in pending:
            try:
                await self.bot.send_message(chat_id, message_text)
            except Exception:
                logger.exception(
                    "Не удалось отправить уведомление о завершении мониторинга chat_id=%s",
                    chat_id,
                )
                await self._set_notification_status(idempotency_key, "failed")
            else:
                await self._set_notification_status(idempotency_key, "sent")

    async def retry_pending_finish_notifications(self) -> int:
        """Повторно отправляет незавершённые уведомления после перезапуска."""
        async with SessionFactory() as session:
            result = await session.execute(
                select(DBNotification, DBMonitor.train)
                .join(DBMonitor, DBMonitor.key == DBNotification.monitor_key)
                .where(
                    DBNotification.event_type.like("monitor_finished_%"),
                    DBNotification.status.in_(["pending", "failed"]),
                )
                .order_by(DBNotification.created_at)
            )
            rows = result.all()

        sent_count = 0
        for notification, train_data in rows:
            train_number = str((train_data or {}).get("number") or "?")
            message_text = self._finish_message(
                notification.event_type, train_number
            )
            try:
                await self.bot.send_message(notification.chat_id, message_text)
            except Exception:
                logger.exception(
                    "Не удалось повторно отправить уведомление о завершении "
                    "мониторинга chat_id=%s key=%s",
                    notification.chat_id, notification.idempotency_key,
                )
                await self._set_notification_status(
                    notification.idempotency_key, "failed"
                )
            else:
                await self._set_notification_status(
                    notification.idempotency_key, "sent"
                )
                sent_count += 1

        if rows:
            logger.info(
                "Обработаны незавершённые уведомления о мониторинге: %s/%s",
                sent_count, len(rows),
                extra={"event": "monitor_finish_notifications_retried"},
            )
        return sent_count

    # =========================================================
    # Основной цикл
    # =========================================================

    async def _run(self, monitor: Monitor) -> None:
        playwright = None
        browser = None
        lock_connection = None
        terminal = False

        try:
            lock_connection = await self._acquire_monitor_lock(
                monitor.key
            )

            if lock_connection is None or self._shutting_down:
                return

            playwright, browser, page = (
                await self.parser.open_monitor_browser()
            )

            while not self._shutting_down:
                await self._refresh_queue(monitor)

                if not monitor.queue:
                    logger.info(
                        "Мониторинг завершён: активных подписок нет",
                        extra={"event": "monitor_empty", "monitor_key": monitor.key},
                    )
                    return

                # Независимая проверка по сохранённым параметрам мониторинга.
                # Не зависит от того, вернул ли сайт строку поезда в расписании.
                saved_departure = monitor.train.get("time")
                if is_departure_time_passed(monitor.travel_date, saved_departure):
                    logger.info(
                        "Поезд уже отправился по сохранённому расписанию: train=%s date=%s time=%s",
                        monitor.train.get("number"), monitor.travel_date, saved_departure,
                        extra={"event": "monitor_departed", "monitor_key": monitor.key},
                    )
                    await self._finish(
                        monitor,
                        f"🛑 Поезд {monitor.train.get('number', '?')} отправился. Мониторинг остановлен.",
                    )
                    terminal = True
                    return

                try:
                    # Читаем все тарифы: лимит у каждой подписки свой.
                    train = await self.parser.read_train(
                        page,
                        monitor.url,
                        monitor.travel_date,
                        monitor.train["number"],
                    )

                    if train is None:
                        await self._finish(
                            monitor,
                            f"⚠️ Поезд {monitor.train['number']} "
                            "больше не найден в расписании. "
                            "Мониторинг остановлен.",
                        )
                        terminal = True
                        return

                    logger.debug(
                        "Получены данные расписания: train=%s date=%s departure=%s departed=%s",
                        train.get("number"), monitor.travel_date, train.get("time"), train.get("departed"),
                        extra={"event": "monitor_schedule_checked", "monitor_key": monitor.key},
                    )

                    if train["departed"]:
                        logger.info(
                            "Сайт подтвердил отправление поезда %s",
                            train.get("number"),
                            extra={"event": "monitor_departed", "monitor_key": monitor.key},
                        )
                        await self._finish(
                            monitor,
                            f"🛑 Поезд {monitor.train['number']} "
                            "отправился. Мониторинг остановлен.",
                        )
                        terminal = True
                        return

                    inventory = _fare_inventory(train)

                    # Загружаем актуальные кумулятивные счётчики.
                    async with SessionFactory() as session:
                        db_monitor = await session.get(
                            DBMonitor,
                            monitor.key,
                        )
                        if db_monitor:
                            monitor.allocated_fares = dict(
                                db_monitor.allocated_fares or {}
                            )
                            monitor.allocated_seats = sum(
                                int(value)
                                for value in monitor.allocated_fares.values()
                            )

                    if inventory:
                        await self._notify_available(
                            monitor,
                            train,
                            inventory,
                        )

                    logger.info(
                        "Проверка поезда %s: тарифов=%s, "
                        "распределено=%s, очередь=%s",
                        train["number"],
                        len(inventory),
                        monitor.allocated_seats,
                        len(monitor.queue),
                    )

                except ScheduleUnavailable:
                    logger.warning(
                        "Расписание временно недоступно. "
                        "Заявки сохранены: поезд=%s",
                        monitor.train.get("number"),
                    )

                await asyncio.sleep(
                    self.settings.check_interval
                )

        except asyncio.CancelledError:
            logger.info(
                "Задача мониторинга отменена: %s",
                monitor.train.get("number"),
            )
            raise

        except Exception:
            logger.exception(
                "Критическая ошибка мониторинга %s",
                monitor.train.get("number"),
            )

            try:
                await self._finish(
                    monitor,
                    "⚠️ Мониторинг остановлен из-за технической ошибки. "
                    "Попробуйте запустить поиск заново через /book.",
                )
            except Exception:
                logger.exception(
                    "Не удалось завершить мониторинг в PostgreSQL"
                )

            terminal = True


        finally:
            if browser:
                try:
                    await browser.close()
                except Exception as exc:
                    logger.warning(
                        "Браузер поезда %s уже закрыт "
                        "или соединение с Playwright потеряно: %s",
                        monitor.train.get("number"),
                        exc,
                    )

            if playwright:
                try:
                    await playwright.stop()
                except Exception as exc:
                    logger.warning(
                        "Playwright для поезда %s уже остановлен: %s",
                        monitor.train.get("number"),
                        exc,
                    )

            await self._release_monitor_lock(
                lock_connection,
                monitor.key,
            )

            async with self._lock:
                if self.monitors.get(monitor.key) is monitor:
                    if terminal or self._shutting_down:
                        self.monitors.pop(monitor.key, None)
                    else:
                        await self._refresh_queue(monitor)
                        if not monitor.queue:
                            self.monitors.pop(monitor.key, None)

    # =========================================================
    # Восстановление после перезапуска
    # =========================================================

    async def restore_active_monitors(self) -> int:
        restored = 0

        async with self._lock:
            async with SessionFactory() as session:
                async with session.begin():
                    result = await session.scalars(
                        select(DBMonitor).where(
                            DBMonitor.is_active.is_(True)
                        )
                    )
                    rows = result.all()

                    for row in rows:
                        subscriptions_result = await session.scalars(
                            select(DBSubscription)
                            .where(
                                DBSubscription.monitor_key == row.key,
                                DBSubscription.is_active.is_(True),
                            )
                            .order_by(DBSubscription.created_order)
                        )
                        queue_rows = subscriptions_result.all()

                        # Активные записи без max_price недопустимы.
                        for sub in queue_rows:
                            if sub.max_price is None:
                                sub.is_active = False

                        queue_rows = [
                            sub for sub in queue_rows
                            if sub.is_active and sub.max_price is not None
                        ]

                        if not queue_rows:
                            row.is_active = False
                            continue

                        allocated_fares = dict(
                            row.allocated_fares or {}
                        )

                        monitor = Monitor(
                            key=row.key,
                            from_name=row.from_name,
                            to_name=row.to_name,
                            travel_date=row.travel_date,
                            train=row.train,
                            url=row.url,
                            allocated_seats=sum(
                                int(value)
                                for value in allocated_fares.values()
                            ),
                            allocated_fares=allocated_fares,
                            queue=[
                                Subscription(
                                    chat_id=sub.chat_id,
                                    tickets_count=sub.tickets_count,
                                    created_order=sub.created_order,
                                    max_price=Decimal(
                                        sub.max_price
                                    ).quantize(Decimal("0.01")),
                                )
                                for sub in queue_rows
                            ],
                        )

                        self.monitors[row.key] = monitor
                        restored += 1

            for monitor in self.monitors.values():
                await self._ensure_task(monitor)

        logger.info(
            "Восстановлено активных мониторингов: %s",
            restored,
        )
        return restored

    # =========================================================
    # Остановка сервиса
    # =========================================================

    async def shutdown(self) -> None:
        self._shutting_down = True
        tasks = []

        for monitor in list(self.monitors.values()):
            if monitor.task and not monitor.task.done():
                monitor.task.cancel()
                tasks.append(monitor.task)

        if tasks:
            await asyncio.gather(
                *tasks,
                return_exceptions=True,
            )

        self.monitors.clear()

        logger.info(
            "Мониторинги остановлены. "
            "Активные заявки сохранены в PostgreSQL."
        )
