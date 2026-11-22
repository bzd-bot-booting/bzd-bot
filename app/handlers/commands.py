
import logging
import uuid
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram_dialog import DialogManager, StartMode, ShowMode
from aiogram_dialog.widgets.kbd import Button
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.states import BookingSG
from app.database.models import BookingJob, Notification
from app.services.monitor_profiles import MonitorProfileService
from app.services.booking_jobs import BookingJobsService
from decimal import Decimal, InvalidOperation
from datetime import datetime
from app.database.engine import SessionFactory
from app.database.models import User, Monitor, Subscription
from app.middlewares.message_cleanup import clear_chat_messages, active_dialog_message_id

logger = logging.getLogger(__name__)

router = Router(name="commands")


def _force_edit_mode(dialog_manager: DialogManager) -> None:
    """Force editing the existing dialog message after text input."""
    dialog_manager.show_mode = ShowMode.EDIT


async def _show_command_input_error(dialog_manager: DialogManager, text: str) -> None:
    dialog_manager.dialog_data["monitor_input_error"] = f"⚠️ {text}"
    await dialog_manager.show()


def _clear_command_input_error(dialog_manager: DialogManager) -> None:
    dialog_manager.dialog_data.pop("monitor_input_error", None)


async def _reset_for_new_search(dialog_manager: DialogManager, chat_id: int) -> None:
    """Не переносит параметры предыдущего поиска в новый поиск."""
    data = dialog_manager.dialog_data
    task = data.get("search_task")
    if task and not task.done():
        task.cancel()

    job_id = data.get("booking_job_id")
    if job_id:
        jobs = BookingJobsService()
        try:
            old_job = await jobs.get_job(uuid.UUID(str(job_id)), chat_id=chat_id)
        except Exception:
            old_job = None
        # Не закрываем браузер и не трогаем задачу после начала отправки заказа:
        # ей нужна сверка на сайте, чтобы исключить повторное оформление.
        if old_job and old_job.status not in {"submitting", "order_unknown", "order_created"}:
            browser = dialog_manager.middleware_data.get("booking_browser_service")
            if browser:
                try:
                    await browser.close_session(str(job_id))
                except Exception:
                    logger.warning("Не удалось закрыть старую сессию бронирования", exc_info=True)
            try:
                await jobs.cancel_job(uuid.UUID(str(job_id)), chat_id=chat_id)
            except Exception:
                logger.info("Старая задача бронирования не отменена", exc_info=True)
        elif old_job:
            logger.warning(
                "Пользователь начал новый поиск при задаче %s со статусом %s; сверка старой задачи продолжится отдельно",
                job_id,
                old_job.status,
            )

    data.clear()


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура главного меню."""

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔎 Найти билеты",
                    callback_data="menu:book",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📋 Активные мониторинги",
                    callback_data="menu:monitors",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🛑 Остановить мониторинги",
                    callback_data="menu:stop",
                )
            ],
            [
                InlineKeyboardButton(
                    text="ℹ️ Помощь",
                    callback_data="menu:help",
                )
            ],
        ]
    )


def main_menu_text(first_name: str | None = None) -> str:
    """Текст главного меню."""

    greeting = (
        f"Привет, <b>{escape(first_name)}</b>!"
        if first_name
        else "Добро пожаловать!"
    )

    return (
        "🚆 <b>БЖД БИЛЕТЫ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"{greeting}\n\n"
        "Я помогу тебе найти билеты на поезда "
        "Белорусской железной дороги и отследить "
        "появление свободных мест.\n\n"
        "Выбери нужное действие в меню ниже 👇"
    )


async def save_user(message: Message) -> None:
    """
    Создаёт пользователя в PostgreSQL или обновляет его данные.
    Идентификатором пользователя служит Telegram user ID.
    """

    telegram_user = message.from_user

    if telegram_user is None:
        logger.warning(
            "Не удалось сохранить пользователя: from_user отсутствует"
        )
        return

    values = {
        "telegram_id": telegram_user.id,
        "username": telegram_user.username,
        "first_name": telegram_user.first_name,
        "last_name": telegram_user.last_name,
        "language_code": telegram_user.language_code,
        "is_active": True,
    }

    statement = (
        insert(User)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[User.telegram_id],
            set_={
                "username": telegram_user.username,
                "first_name": telegram_user.first_name,
                "last_name": telegram_user.last_name,
                "language_code": telegram_user.language_code,
                "is_active": True,
                "last_seen_at": func.now(),
            },
        )
    )

    async with SessionFactory() as session:
        async with session.begin():
            await session.execute(statement)

    logger.info(
        "Пользователь Telegram %s сохранён в PostgreSQL",
        telegram_user.id,
    )


async def _clear_previous_chat_messages(bot, chat_id: int, dialog_manager: DialogManager) -> None:
    """Delete all previously tracked bot messages, retaining the active dialog anchor."""
    keep_id = active_dialog_message_id(dialog_manager)
    deleted = await clear_chat_messages(bot, chat_id, keep_message_id=keep_id)
    logger.info("Очистка чата %s перед командой: удалено сообщений бота=%s", chat_id, deleted)


async def cmd_start(message: Message, dialog_manager: DialogManager):
    await _clear_previous_chat_messages(message.bot, message.chat.id, dialog_manager)
    if dialog_manager.has_context():
        await _reset_for_new_search(dialog_manager, message.chat.id)
    try:
        await save_user(message)
    except Exception:
        logger.exception(
            "Не удалось сохранить пользователя при выполнении /start"
        )

    first_name = (
        message.from_user.first_name
        if message.from_user
        else None
    )

    await dialog_manager.start(
        BookingSG.main_menu,
        mode=StartMode.RESET_STACK,
    )


async def cmd_book(
    message: Message,
    dialog_manager: DialogManager,
):
    await _clear_previous_chat_messages(message.bot, message.chat.id, dialog_manager)
    if dialog_manager.has_context():
        await _reset_for_new_search(dialog_manager, message.chat.id)
        await dialog_manager.switch_to(BookingSG.station_from)
    else:
        await dialog_manager.start(
            BookingSG.station_from,
            mode=StartMode.RESET_STACK,
        )


async def cb_book(
    callback: CallbackQuery,
    dialog_manager: DialogManager,
):
    await _clear_previous_chat_messages(callback.bot, callback.from_user.id, dialog_manager)
    await callback.answer()

    if dialog_manager.has_context():
        await _reset_for_new_search(dialog_manager, callback.from_user.id)
        await dialog_manager.switch_to(BookingSG.station_from)
    else:
        await dialog_manager.start(
            BookingSG.station_from,
            mode=StartMode.RESET_STACK,
        )


async def cmd_stop(message: Message, monitor_service, dialog_manager: DialogManager):
    await _clear_previous_chat_messages(message.bot, message.chat.id, dialog_manager)
    count = await monitor_service.unsubscribe_user(message.chat.id)

    if count:
        text = f"🛑 Остановлено ваших мониторингов: <b>{count}</b>."
    else:
        text = "ℹ️ У вас нет активных мониторингов."

    if dialog_manager.has_context():
        dialog_manager.dialog_data["menu_notice"] = text
        await dialog_manager.switch_to(BookingSG.main_menu)
    else:
        await message.answer(
            text,
            reply_markup=main_menu_keyboard(),
            parse_mode="HTML",
        )


async def cb_stop(callback: CallbackQuery, monitor_service, dialog_manager: DialogManager):
    if not isinstance(callback.message, Message):
        return
    await _clear_previous_chat_messages(callback.bot, callback.message.chat.id, dialog_manager)

    count = await monitor_service.unsubscribe_user(
        callback.message.chat.id
    )

    if count:
        text = (
            "🛑 <b>Мониторинги остановлены</b>\n\n"
            f"Количество отменённых заявок: <b>{count}</b>.\n\n"
            "Ты больше не будешь получать уведомления "
            "по этим заявкам."
        )
    else:
        text = (
            "ℹ️ <b>Активных мониторингов нет</b>\n\n"
            "Сейчас бот ничего не отслеживает."
        )

    await callback.answer(text[:180], show_alert=False)
    if dialog_manager.has_context():
        await dialog_manager.switch_to(BookingSG.main_menu)
    else:
        await dialog_manager.start(
            BookingSG.main_menu,
            mode=StartMode.RESET_STACK,
        )


async def cb_monitors(
    callback: CallbackQuery,
    dialog_manager: DialogManager,
):
    """Открывает список мониторингов как из старой, так и из новой клавиатуры."""
    await callback.answer()
    if dialog_manager.has_context():
        await dialog_manager.switch_to(BookingSG.my_monitors)
    else:
        await dialog_manager.start(
            BookingSG.my_monitors,
            mode=StartMode.RESET_STACK,
        )


async def get_main_menu_data(dialog_manager: DialogManager, **kwargs):
    user = getattr(dialog_manager.event, "from_user", None)
    first_name = getattr(user, "first_name", None)
    notice = dialog_manager.dialog_data.get("menu_notice")
    text = main_menu_text(first_name)
    if notice:
        text += "\n\n" + escape(str(notice))
        dialog_manager.dialog_data.pop("menu_notice", None)
    return {"main_menu_text": text}


async def on_menu_book(callback, button, dialog_manager):
    await _clear_previous_chat_messages(callback.bot, callback.from_user.id, dialog_manager)
    await _reset_for_new_search(dialog_manager, callback.from_user.id)
    await dialog_manager.switch_to(BookingSG.station_from)


async def on_menu_monitors(callback, button, dialog_manager):
    await callback.answer()
    await dialog_manager.switch_to(BookingSG.my_monitors)


async def on_menu_stop(callback, button, dialog_manager):
    await _clear_previous_chat_messages(callback.bot, callback.from_user.id, dialog_manager)
    service = dialog_manager.middleware_data.get("monitor_service")
    count = await service.unsubscribe_user(callback.from_user.id) if service else 0
    dialog_manager.dialog_data["menu_notice"] = f"Остановлено мониторингов: {count}"
    await callback.answer(dialog_manager.dialog_data["menu_notice"])
    await dialog_manager.switch_to(BookingSG.main_menu)


async def on_menu_help(callback, button, dialog_manager):
    await callback.answer(
        "Выбери поиск билетов или открой раздел «Мои мониторинги».",
        show_alert=True,
    )


# ---------------------------------------------------------------------
# Управление мониторингами в одном окне aiogram-dialog
# ---------------------------------------------------------------------

def _dialog_chat_id(dialog_manager: DialogManager) -> int:
    event = dialog_manager.event
    user = getattr(event, "from_user", None)
    if user is None:
        raise RuntimeError("Не удалось определить пользователя Telegram.")
    return int(user.id)


async def get_my_monitors_data(dialog_manager: DialogManager, **kwargs):
    """Загружает подписки пользователя; ошибка БД не обрывает весь диалог."""
    chat_id = _dialog_chat_id(dialog_manager)
    try:
        async with SessionFactory() as session:
            result = await session.execute(
                select(Subscription, Monitor)
                .join(Monitor, Subscription.monitor_key == Monitor.key)
                .where(
                    Subscription.chat_id == chat_id,
                )
                .order_by(Subscription.created_at.desc())
            )
            rows = result.all()
    except Exception:
        logger.exception(
            "Не удалось загрузить мониторинги пользователя chat_id=%s",
            chat_id,
        )
        return {
            "monitor_items": [],
            "monitor_list_text": (
                "📋 <b>Активные мониторинги</b>\n\n"
                "Не удалось загрузить список из базы данных. "
                "Нажми «Обновить» или попробуй позже."
            ),
        }

    items = []
    # Не помещаем monitor.key в callback_data: ключ содержит маршрут,
    # дату и номер поезда и часто превышает лимит Telegram в 64 байта.
    # В callback передаётся короткий индекс, а настоящий ключ хранится
    # в dialog_data текущего пользователя.
    monitor_key_by_item_id = {}
    for index, (sub, mon) in enumerate(rows):
        status = "🟢 Активен" if sub.is_active and mon.is_active else "⏸ Приостановлен"
        train_no = str((mon.train or {}).get("number") or "не указан")
        label = (
            f"{status} · {mon.from_name} → {mon.to_name} · "
            f"{mon.travel_date} · №{train_no}"
        )
        item_id = str(index)
        monitor_key_by_item_id[item_id] = str(mon.key)
        items.append((label[:90], item_id))

    dialog_manager.dialog_data["monitor_key_by_item_id"] = monitor_key_by_item_id

    return {
        "monitor_items": items,
        "monitor_list_text": (
            "📋 <b>Активные мониторинги</b>\n\n"
            "Выбери мониторинг, чтобы посмотреть его параметры, "
            "данные для оформления и историю."
            if items else
            "📋 <b>Активные мониторинги</b>\n\n"
            "У тебя пока нет сохранённых мониторингов."
        ),
    }


async def on_my_monitor_selected(callback, widget, dialog_manager, item_id):
    # item_id — короткий индекс, а не длинный ключ маршрута.
    key_map = dialog_manager.dialog_data.get("monitor_key_by_item_id") or {}
    monitor_key = key_map.get(str(item_id))
    if not monitor_key:
        await callback.answer(
            "Список мониторингов устарел. Нажми «Обновить».",
            show_alert=True,
        )
        await dialog_manager.switch_to(BookingSG.my_monitors)
        return

    dialog_manager.dialog_data["selected_monitor_key"] = str(monitor_key)
    await dialog_manager.switch_to(BookingSG.monitor_details)


async def _get_selected_monitor(chat_id: int, key: str):
    async with SessionFactory() as session:
        result = await session.execute(
            select(Subscription, Monitor)
            .join(Monitor, Subscription.monitor_key == Monitor.key)
            .where(
                Subscription.chat_id == chat_id,
                Subscription.monitor_key == key,
            )
        )
        return result.first()


async def get_monitor_details_data(dialog_manager: DialogManager, **kwargs):
    chat_id = _dialog_chat_id(dialog_manager)
    key = dialog_manager.dialog_data.get("selected_monitor_key")
    row = await _get_selected_monitor(chat_id, key) if key else None
    if row is None:
        return {"monitor_details_text": "Мониторинг не найден."}

    sub, mon = row
    train = mon.train or {}
    status = "🟢 Активен" if sub.is_active and mon.is_active else "⏸ Приостановлен"
    auto_status = "включено" if sub.is_active and mon.is_active else "ожидает возобновления"
    price = f"{sub.max_price:.2f} BYN за билет" if sub.max_price is not None else "не задана"
    train_number = str(train.get("number") or "не указан")
    departure = str(train.get("time") or "—")
    arrival = str(train.get("arr_time") or "—")

    # Последнее задание, относящееся к этому мониторингу.
    async with SessionFactory() as session:
        jobs_result = await session.scalars(
            select(BookingJob)
            .where(
                BookingJob.chat_id == chat_id,
                BookingJob.preferences.contains({"monitor_key": str(key)}),
            )
            .order_by(BookingJob.created_at.desc())
            .limit(1)
        )
        last_job = jobs_result.first()
    job_status = {
        "draft": "черновик",
        "waiting": "ожидает выполнения",
        "running": "выполняется",
        "submitting": "отправка заказа",
        "order_unknown": "проверка результата заказа",
        "order_created": "заказ создан, ожидает оплаты",
        "cancelled": "отменено",
        "expired": "заказ истёк",
        "failed": "ошибка оформления",
    }
    last_status = job_status.get(last_job.status, last_job.status) if last_job else "попыток пока нет"
    last_order = ""
    if last_job:
        if last_job.order_number:
            last_order += f"\nНомер заказа: <b>{escape(last_job.order_number)}</b>"
        if last_job.order_expires_at:
            expires = last_job.order_expires_at.strftime("%d.%m.%Y %H:%M")
            last_order += f"\nСрок оплаты по данным сайта: <b>{expires}</b>"

    return {
        "monitor_details_text": (
            "🚆 <b>Карточка мониторинга</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"📍 Маршрут: <b>{escape(mon.from_name)} → {escape(mon.to_name)}</b>\n"
            f"📅 Дата: <b>{escape(mon.travel_date)}</b>\n"
            f"🚆 Поезд: <b>№{escape(train_number)}</b>\n"
            f"🕒 Время: <b>{escape(departure)} — {escape(arrival)}</b>\n"
            f"🎫 Количество билетов: <b>{sub.tickets_count}</b>\n"
            f"💰 Максимальная цена: <b>{price}</b>\n"
            f"📡 Статус: <b>{status}</b>\n"
            f"🤖 Автоматическое оформление: <b>{auto_status}</b>\n\n"
            f"🧾 Последняя попытка: <b>{escape(last_status)}</b>"
            f"{last_order}"
        ),
        "monitor_is_active": bool(sub.is_active),
    }


async def on_monitor_profile(callback, button, dialog_manager):
    await dialog_manager.switch_to(BookingSG.monitor_profile)


async def get_monitor_profile_data(dialog_manager: DialogManager, **kwargs):
    chat_id = _dialog_chat_id(dialog_manager)
    try:
        profile = await MonitorProfileService().get(chat_id)
    except Exception:
        logger.exception("Не удалось расшифровать профиль chat_id=%s", chat_id)
        profile = None

    if not profile:
        return {
            "monitor_profile_text": (
                "👤 <b>Данные для оформления</b>\n\n"
                "Сохранённый профиль не найден. Для автоматического "
                "оформления сначала нужно войти на pass.rw.by и заполнить "
                "данные пассажиров."
            )
        }

    login = escape(str(profile.get("login") or "не сохранён"))
    passengers = profile.get("passengers") or []
    passenger_items = []
    lines = []
    for index, passenger in enumerate(passengers, start=1):
        last_name = escape(str(passenger.get("last_name") or ""))
        first_name = escape(str(passenger.get("first_name") or ""))
        middle_name = escape(str(passenger.get("middle_name") or ""))
        doc_type = escape(str(passenger.get("document_type") or "не указан"))
        doc = str(passenger.get("document_number") or "")
        masked = ("•" * max(0, len(doc) - 3)) + doc[-3:] if doc else "не указан"
        birth = escape(str(passenger.get("birth_date") or passenger.get("date_of_birth") or "не указана"))
        bedding = profile.get("bedding", passenger.get("bedding"))
        bedding_text = "да" if bedding is True else "нет" if bedding is False else "не задано"
        passenger_items.append((
            f"Пассажир {index}: {last_name} {first_name}".strip(),
            str(index),
        ))
        lines.append(
            f"\n<b>Пассажир {index}</b>\n"
            f"ФИО: {last_name} {first_name} {middle_name}\n"
            f"Документ: {doc_type}\n"
            f"Номер: <code>{escape(masked)}</code>\n"
            f"Дата рождения: {birth}\n"
            f"Постельное бельё: {bedding_text}"
        )

    return {
        "monitor_profile_text": (
            "👤 <b>Данные для автоматического оформления</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"Логин pass.rw.by: <code>{login}</code>\n"
            "Пароль: <i>сохранён, не отображается</i>\n"
            + ("\n".join(lines) if lines else "\nПассажиры ещё не сохранены.")
        ),
        "passenger_items": passenger_items,
    }


async def on_monitor_passenger_edit(callback, button, dialog_manager):
    try:
        profile = await MonitorProfileService().get(_dialog_chat_id(dialog_manager))
    except Exception:
        logger.exception("Не удалось открыть редактор пассажиров")
        profile = None
    if not profile or not profile.get("passengers"):
        await callback.answer("Сначала сохраните данные пассажиров при оформлении.", show_alert=True)
        return
    await dialog_manager.switch_to(BookingSG.monitor_passenger_select)


async def get_monitor_passengers_data(dialog_manager: DialogManager, **kwargs):
    chat_id = _dialog_chat_id(dialog_manager)
    profile = await MonitorProfileService().get(chat_id)
    passengers = (profile or {}).get("passengers") or []
    items = []
    for index, passenger in enumerate(passengers, start=1):
        fio = " ".join(
            str(passenger.get(field) or "").strip()
            for field in ("last_name", "first_name", "middle_name")
            if str(passenger.get(field) or "").strip()
        ) or "ФИО не заполнено"
        items.append((f"{index}. {fio}"[:90], str(index)))
    return {
        "passenger_items": items,
        "passenger_select_text": (
            "✏️ <b>Редактирование пассажиров</b>\n\n"
            "Выбери пассажира, данные которого нужно изменить."
            if items else
            "✏️ <b>Редактирование пассажиров</b>\n\n"
            "Сохранённые пассажиры не найдены."
        ),
    }


async def on_monitor_passenger_selected(callback, widget, dialog_manager, item_id):
    try:
        index = int(item_id)
        profile = await MonitorProfileService().get(_dialog_chat_id(dialog_manager))
        passengers = (profile or {}).get("passengers") or []
        if index < 1 or index > len(passengers):
            raise ValueError("Пассажир не найден")
    except Exception:
        await callback.answer("Не удалось выбрать пассажира. Обнови список.", show_alert=True)
        return
    dialog_manager.dialog_data["monitor_passenger_index"] = index
    await dialog_manager.switch_to(BookingSG.monitor_passenger_edit)


_MONITOR_PASSENGER_FIELDS = [
    ("Фамилия", "last_name"),
    ("Имя", "first_name"),
    ("Отчество", "middle_name"),
    ("Тип документа", "document_type"),
    ("Номер документа", "document_number"),
    ("Дата рождения", "birth_date"),
    ("Постельное бельё", "bedding"),
]


async def get_monitor_passenger_edit_data(dialog_manager: DialogManager, **kwargs):
    chat_id = _dialog_chat_id(dialog_manager)
    index = int(dialog_manager.dialog_data.get("monitor_passenger_index", 0))
    profile = await MonitorProfileService().get(chat_id)
    passengers = (profile or {}).get("passengers") or []
    if index < 1 or index > len(passengers):
        return {
            "passenger_edit_text": "Пассажир не найден. Вернись к списку и выбери его снова.",
            "passenger_field_items": [],
        }
    passenger = passengers[index - 1]
    fio = " ".join(
        str(passenger.get(field) or "").strip()
        for field in ("last_name", "first_name", "middle_name")
        if str(passenger.get(field) or "").strip()
    ) or "—"
    notice = dialog_manager.dialog_data.pop("passenger_edit_notice", None)
    notice_text = f"\n\n✅ {escape(str(notice))}" if notice else ""
    bedding = profile.get("bedding", passenger.get("bedding"))
    bedding_label = (
        "требуется" if bedding is True
        else "не требуется" if bedding is False
        else "не задано"
    )
    return {
        "passenger_edit_text": (
            f"✏️ <b>Пассажир {index}</b>\n\n"
            f"ФИО: <b>{escape(fio)}</b>\n"
            f"Постельное бельё: <b>{bedding_label}</b>\n"
            "Выбери поле для изменения. Номер документа в этом меню не отображается полностью."
            f"{notice_text}"
        ),
        "passenger_field_items": [
            (label, key) for label, key in _MONITOR_PASSENGER_FIELDS
        ],
    }


async def on_monitor_passenger_field_selected(callback, widget, dialog_manager, item_id):
    allowed = {key for _, key in _MONITOR_PASSENGER_FIELDS}
    field = str(item_id)
    if field not in allowed:
        await callback.answer("Неизвестное поле", show_alert=True)
        return

    dialog_manager.dialog_data["monitor_passenger_field"] = field
    if field == "bedding":
        await dialog_manager.switch_to(BookingSG.monitor_passenger_bedding)
        return

    if field == "document_type":
        chat_id = _dialog_chat_id(dialog_manager)
        profile = await MonitorProfileService().get(chat_id) or {}
        options = list(profile.get("document_options") or [])
        browser = dialog_manager.middleware_data.get("booking_browser_service")

        # Используем тот же источник и тот же формат вариантов, что и при
        # автоматическом бронировании: реальные <option> из document_type_1.
        # Сначала проверяем текущую страницу заявки, затем страницы активных
        # сессий BookingBrowserService.
        if browser:
            live_options = []
            job_id = dialog_manager.dialog_data.get("booking_job_id")
            try:
                if job_id:
                    page = await browser.get_page(str(job_id))
                    if page is not None and not page.is_closed():
                        live_options = await browser.get_document_options(
                            page, passenger_index=1
                        )
                if not live_options:
                    live_options = await browser.get_document_options_from_active_sessions(
                        passenger_index=1
                    )
            except Exception:
                logger.warning(
                    "Не удалось обновить типы документов из формы pass.rw.by; "
                    "используется последний сохранённый список",
                    exc_info=True,
                )

            if live_options:
                options = live_options
                try:
                    await MonitorProfileService().save_document_options(
                        chat_id, live_options
                    )
                except Exception:
                    # Список всё равно остаётся доступен в текущем диалоге.
                    logger.warning(
                        "Не удалось обновить кэш типов документов профиля",
                        exc_info=True,
                    )

        normalized = []
        seen = set()
        for item in options:
            if not isinstance(item, dict):
                continue
            value = str(item.get("value") or "").strip()
            label = str(item.get("text") or "").strip()
            if value and label and value not in seen:
                normalized.append({"value": value, "text": label})
                seen.add(value)

        if not normalized:
            await callback.answer(
                "Список типов документов ещё не получен с pass.rw.by. "
                "Он появится после того, как автоматическое бронирование "
                "откроет форму пассажира и сохранит варианты сайта.",
                show_alert=True,
            )
            return

        # Сохраняем именно тот список, из которого будут построены кнопки.
        # Обработчик выбора ниже читает этот же список, а не другой кэш БД.
        dialog_manager.dialog_data["monitor_document_type_options"] = normalized
        dialog_manager.dialog_data["monitor_document_type_items"] = [
            (item["text"], item["value"]) for item in normalized
        ]
        await dialog_manager.switch_to(BookingSG.monitor_passenger_document_type)
        return

    await dialog_manager.switch_to(BookingSG.monitor_passenger_edit_value)


async def get_monitor_document_type_data(dialog_manager: DialogManager, **kwargs):
    chat_id = _dialog_chat_id(dialog_manager)
    index = int(dialog_manager.dialog_data.get("monitor_passenger_index", 0))
    profile = await MonitorProfileService().get(chat_id)
    passengers = (profile or {}).get("passengers") or []
    current = ""
    if 1 <= index <= len(passengers):
        current = str(passengers[index - 1].get("document_type") or "")
    return {
        "monitor_document_type_items": dialog_manager.dialog_data.get(
            "monitor_document_type_items", []
        ),
        "monitor_document_type_text": (
            f"📄 <b>Тип документа пассажира {index}</b>\n\n"
            f"Текущий тип: <b>{escape(current or 'не указан')}</b>\n"
            "Выберите тип документа из полного списка, полученного с pass.rw.by:"
        ),
    }


async def on_monitor_document_type_selected(callback, widget, dialog_manager, item_id):
    try:
        chat_id = _dialog_chat_id(dialog_manager)
        index = int(dialog_manager.dialog_data.get("monitor_passenger_index", 0))
        profile = await MonitorProfileService().get(chat_id)
        passengers = (profile or {}).get("passengers") or []
        options = dialog_manager.dialog_data.get(
            "monitor_document_type_options"
        ) or (profile or {}).get("document_options") or []
        if index < 1 or index > len(passengers):
            raise LookupError("Пассажир не найден.")
        selected = next(
            (item for item in options if str(item.get("value")) == str(item_id)),
            None,
        )
        if selected is None:
            raise ValueError("Выбранный тип документа отсутствует в актуальном списке сайта.")
        passengers[index - 1]["document_type"] = str(selected["text"])
        passengers[index - 1]["document_type_value"] = str(selected["value"])
        await MonitorProfileService().save_passengers(chat_id, passengers)
        # Синхронизируем список в зашифрованном профиле, если он пришёл
        # из текущей живой формы сайта.
        if dialog_manager.dialog_data.get("monitor_document_type_options"):
            try:
                await MonitorProfileService().save_document_options(
                    chat_id,
                    dialog_manager.dialog_data["monitor_document_type_options"],
                )
            except Exception:
                logger.warning("Не удалось сохранить список типов документов", exc_info=True)
        dialog_manager.dialog_data.pop("monitor_document_type_options", None)
        dialog_manager.dialog_data.pop("monitor_document_type_items", None)
        dialog_manager.dialog_data["passenger_edit_notice"] = "Тип документа сохранён."
        _clear_command_input_error(dialog_manager)
        await dialog_manager.switch_to(BookingSG.monitor_passenger_edit)
    except Exception:
        logger.exception("Не удалось изменить тип документа пассажира")
        await callback.answer("Не удалось сохранить тип документа. Обновите список и попробуйте ещё раз.", show_alert=True)
        return
    await callback.answer("Тип документа сохранён.")


async def _save_monitor_bedding_choice(callback, dialog_manager, enabled: bool):
    try:
        chat_id = _dialog_chat_id(dialog_manager)
        index = int(dialog_manager.dialog_data.get("monitor_passenger_index", 0))
        service = MonitorProfileService()
        profile = await service.get(chat_id)
        passengers = (profile or {}).get("passengers") or []
        if index < 1 or index > len(passengers):
            raise LookupError("Пассажир не найден.")
        await service.save_bedding_preference(chat_id, bool(enabled))
        dialog_manager.dialog_data["passenger_edit_notice"] = (
            "Постельное бельё: требуется." if enabled
            else "Постельное бельё: не требуется."
        )
        _clear_command_input_error(dialog_manager)
        await dialog_manager.switch_to(BookingSG.monitor_passenger_edit)
        await callback.answer("Настройка постельного белья сохранена.")
    except Exception:
        logger.exception("Не удалось изменить настройку постельного белья")
        await callback.answer("Не удалось сохранить настройку.", show_alert=True)


async def on_monitor_bedding_yes(callback, button, dialog_manager):
    await _save_monitor_bedding_choice(callback, dialog_manager, True)


async def on_monitor_bedding_no(callback, button, dialog_manager):
    await _save_monitor_bedding_choice(callback, dialog_manager, False)


async def get_monitor_passenger_edit_value_data(dialog_manager: DialogManager, **kwargs):
    field = dialog_manager.dialog_data.get("monitor_passenger_field")
    label = dict((key, name) for name, key in _MONITOR_PASSENGER_FIELDS).get(field, "данные")
    index = dialog_manager.dialog_data.get("monitor_passenger_index", "?")
    hint = (
        "Введи реальную дату в формате <code>ДД.ММ.ГГГГ</code>, например "
        "<code>07.04.1995</code>. Будущая дата не допускается.\n\n"
        if field == "birth_date" else ""
    )
    return {
        "passenger_edit_value_text": (
            f"✏️ <b>Изменение поля «{escape(label)}» пассажира {index}</b>\n\n"
            + hint
            + "Отправь новое значение одним сообщением. Для отчества или даты рождения можно отправить дефис <code>-</code>, чтобы очистить поле."
        ),
        "monitor_input_error": dialog_manager.dialog_data.get("monitor_input_error", ""),
    }


async def on_monitor_passenger_edit_value(message, message_input, dialog_manager):
    _force_edit_mode(dialog_manager)
    chat_id = message.from_user.id
    field = dialog_manager.dialog_data.get("monitor_passenger_field")
    index = int(dialog_manager.dialog_data.get("monitor_passenger_index", 0))
    allowed = {key for _, key in _MONITOR_PASSENGER_FIELDS}
    if field not in allowed or index < 1:
        await _show_command_input_error(dialog_manager, "Сессия редактирования устарела. Открой редактор заново.")
        return

    value = (message.text or "").strip()
    if value == "-" and field in {"middle_name", "birth_date"}:
        value = ""
    if len(value) > 100 or (not value and field not in {"middle_name", "birth_date"}):
        await _show_command_input_error(dialog_manager, "Значение пустое или слишком длинное. Введи корректные данные ещё раз.")
        return

    if field == "birth_date" and value:
        parsed_birth_date = None
        for date_format in ("%d.%m.%Y", "%Y-%m-%d"):
            try:
                parsed_birth_date = datetime.strptime(value, date_format).date()
                break
            except ValueError:
                continue
        if parsed_birth_date is None:
            await _show_command_input_error(
                dialog_manager,
                "Введи существующую дату рождения в формате ДД.ММ.ГГГГ (например, 07.04.1995) или ГГГГ-ММ-ДД.",
            )
            return
        if parsed_birth_date > datetime.now().date():
            await _show_command_input_error(dialog_manager, "Дата рождения не может быть в будущем.")
            return
        value = parsed_birth_date.isoformat()

    try:
        service = MonitorProfileService()
        profile = await service.get(chat_id)
        if not profile:
            raise LookupError("Сохранённый профиль не найден.")
        passengers = profile.get("passengers") or []
        if index > len(passengers):
            raise LookupError("Пассажир не найден.")
        passenger = passengers[index - 1]
        if field == "birth_date":
            # В старых профилях дата рождения могла храниться под другим
            # названием. Обновляем существующий ключ, не оставляя старое значение.
            if "date_of_birth" in passenger and "birth_date" not in passenger:
                passenger["date_of_birth"] = value
            else:
                passenger["birth_date"] = value
                if "date_of_birth" in passenger:
                    passenger["date_of_birth"] = value
        else:
            passenger[field] = value
        await service.save_passengers(chat_id, passengers)
        dialog_manager.dialog_data["passenger_edit_notice"] = (
            f"Поле «{dict((key, label) for label, key in _MONITOR_PASSENGER_FIELDS).get(field, field)}» сохранено."
        )
        _clear_command_input_error(dialog_manager)
        await dialog_manager.switch_to(BookingSG.monitor_passenger_edit)
    except Exception:
        logger.exception("Не удалось сохранить поле %s пассажира %s", field, index)
        await _show_command_input_error(dialog_manager, "Не удалось сохранить данные. Проверь введённое значение и попробуй ещё раз.")


async def on_monitor_history(callback, button, dialog_manager):
    await dialog_manager.switch_to(BookingSG.monitor_history)


async def get_monitor_history_data(dialog_manager: DialogManager, **kwargs):
    chat_id = _dialog_chat_id(dialog_manager)
    key = dialog_manager.dialog_data.get("selected_monitor_key")
    if not key:
        return {"monitor_history_text": "🧾 <b>История мониторинга</b>\n\nМониторинг не выбран."}

    async with SessionFactory() as session:
        jobs_result = await session.scalars(
            select(BookingJob)
            .where(
                BookingJob.chat_id == chat_id,
                BookingJob.preferences.contains({"monitor_key": str(key)}),
            )
            .order_by(BookingJob.created_at.desc())
            .limit(30)
        )
        jobs = list(jobs_result.all())
        notifications_result = await session.scalars(
            select(Notification)
            .where(
                Notification.chat_id == chat_id,
                Notification.monitor_key == str(key),
            )
            .order_by(Notification.created_at.desc())
            .limit(30)
        )
        notifications = list(notifications_result.all())

    job_labels = {
        "draft": "Черновик оформления",
        "waiting": "Ожидает автоматического оформления",
        "running": "Выполняется автоматическое оформление",
        "submitting": "Отправка заказа на сайт",
        "order_unknown": "Проверяется результат отправки заказа",
        "order_created": "Заказ создан, ожидает оплаты",
        "cancelled": "Оформление отменено",
        "expired": "Срок задачи истёк",
        "failed": "Ошибка оформления",
    }
    event_labels = {
        "seats_available": "Найдены свободные места",
    }
    status_labels = {
        "sent": "отправлено",
        "pending": "ожидает отправки",
        "failed": "ошибка отправки",
    }

    entries = []
    for job in jobs:
        created = job.created_at
        stamp = created.strftime("%d.%m.%Y %H:%M") if created else "—"
        label = job_labels.get(job.status, str(job.status))
        details = []
        if job.order_number:
            details.append(f"заказ №{escape(str(job.order_number))}")
        if job.order_expires_at:
            details.append(
                "оплатить до " + job.order_expires_at.strftime("%d.%m.%Y %H:%M")
            )
        suffix = ("; " + "; ".join(details)) if details else ""
        entries.append((created, f"• {stamp}: <b>{escape(label)}</b>{escape(suffix)}"))

    for item in notifications:
        created = item.created_at
        stamp = created.strftime("%d.%m.%Y %H:%M") if created else "—"
        event = event_labels.get(item.event_type, str(item.event_type))
        status = status_labels.get(item.status, str(item.status))
        explanation = (
            "Это уведомление о наличии, а не подтверждение бронирования."
            if item.event_type == "seats_available" and item.status == "sent"
            else ""
        )
        line = f"• {stamp}: <b>{escape(event)}</b> ({escape(status)})"
        if explanation:
            line += f"\n  <i>{escape(explanation)}</i>"
        entries.append((created, line))

    entries.sort(key=lambda item: item[0].timestamp() if item[0] else 0, reverse=True)
    lines = [line for _, line in entries[:40]]
    return {
        "monitor_history_text": (
            "🧾 <b>История мониторинга</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            + ("\n".join(lines) if lines else "История пока пуста.")
        )
    }


async def on_monitor_delete(callback, button, dialog_manager):
    """Удаляет выбранный мониторинг только из списка текущего пользователя."""
    key = dialog_manager.dialog_data.get("selected_monitor_key")
    service = dialog_manager.middleware_data.get("monitor_service")

    if not key:
        await callback.answer("Мониторинг не выбран", show_alert=True)
        return
    if service is None:
        await callback.answer("Сервис мониторинга недоступен", show_alert=True)
        return

    try:
        # Полностью удаляем подписку только текущего пользователя.
        deleted = await service.delete_subscription(
            str(key),
            callback.from_user.id,
        )
    except Exception:
        logger.exception(
            "Не удалось удалить мониторинг %s пользователя %s",
            key,
            callback.from_user.id,
        )
        await callback.answer("Не удалось удалить мониторинг. Попробуй ещё раз.", show_alert=True)
        return

    dialog_manager.dialog_data.pop("selected_monitor_key", None)
    await callback.answer(
        "Мониторинг удалён" if deleted else "Мониторинг уже удалён",
        show_alert=False,
    )
    await dialog_manager.switch_to(BookingSG.my_monitors)


async def on_monitor_stop(callback, button, dialog_manager):
    key = dialog_manager.dialog_data.get("selected_monitor_key")
    service = dialog_manager.middleware_data.get("monitor_service")
    if key and service:
        await service.unsubscribe_from(str(key), callback.from_user.id)
    await callback.answer("Мониторинг остановлен")
    await dialog_manager.switch_to(BookingSG.my_monitors)


async def on_monitor_resume(callback, button, dialog_manager):
    key = dialog_manager.dialog_data.get("selected_monitor_key")
    if not key:
        await callback.answer("Мониторинг не найден", show_alert=True)
        return
    row = await _get_selected_monitor(callback.from_user.id, str(key))
    if row is None:
        await callback.answer("Мониторинг не найден", show_alert=True)
        return
    sub, mon = row
    if sub.max_price is None:
        await callback.answer("Не задан максимальный лимит цены", show_alert=True)
        return
    service = dialog_manager.middleware_data.get("monitor_service")
    if service is None:
        await callback.answer("Сервис мониторинга недоступен", show_alert=True)
        return
    await service.subscribe(
        key=mon.key,
        from_name=mon.from_name,
        to_name=mon.to_name,
        travel_date=mon.travel_date,
        train=mon.train,
        url=mon.url,
        chat_id=callback.from_user.id,
        tickets_count=sub.tickets_count,
        max_price=sub.max_price,
    )
    await callback.answer("Мониторинг возобновлён")
    await dialog_manager.switch_to(BookingSG.monitor_details)


async def on_monitor_edit_price(callback, button, dialog_manager):
    await dialog_manager.switch_to(BookingSG.monitor_edit_price)


async def get_monitor_price_data(dialog_manager: DialogManager, **kwargs):
    return {
        "monitor_input_error": dialog_manager.dialog_data.get("monitor_input_error", "")
    }


async def on_monitor_price_input(message, message_input, dialog_manager):
    _force_edit_mode(dialog_manager)
    raw = (message.text or "").strip().replace(",", ".")
    try:
        price = Decimal(raw)
        if not price.is_finite() or price < Decimal("1") or price > Decimal("10000"):
            raise InvalidOperation
        price = price.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        await _show_command_input_error(
            dialog_manager,
            "Сумма должна быть от 1 до 10 000 BYN. Пример: 45 или 45.50.",
        )
        return

    key = dialog_manager.dialog_data.get("selected_monitor_key")
    if not key:
        await _show_command_input_error(dialog_manager, "Мониторинг не выбран. Вернитесь к списку.")
        return
    row = await _get_selected_monitor(message.from_user.id, str(key))
    if row is None:
        await _show_command_input_error(dialog_manager, "Мониторинг не найден.")
        return
    sub, mon = row
    async with SessionFactory() as session:
        async with session.begin():
            db_sub = await session.scalar(
                select(Subscription).where(
                    Subscription.id == sub.id,
                    Subscription.chat_id == message.from_user.id,
                ).with_for_update()
            )
            if db_sub is None:
                await _show_command_input_error(dialog_manager, "Подписка не найдена.")
                return
            db_sub.max_price = price

    service = dialog_manager.middleware_data.get("monitor_service")
    if service and str(key) in service.monitors:
        await service._refresh_queue(service.monitors[str(key)])

    _clear_command_input_error(dialog_manager)
    await dialog_manager.switch_to(BookingSG.monitor_details)


async def cb_help(callback: CallbackQuery):
    await callback.answer()

    if not isinstance(callback.message, Message):
        return

    text = (
        "ℹ️ <b>Как пользоваться ботом</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "🔎 <b>Найти билеты</b>\n"
        "Выбери станции отправления и назначения, "
        "дату, поезд и количество билетов. "
        "Затем укажи максимальную цену одного билета.\n\n"
        "📋 <b>Активные мониторинги</b>\n"
        "Здесь отображаются активные заявки, "
        "количество билетов и установленный ценовой лимит.\n\n"
        "🛑 <b>Остановить мониторинги</b>\n"
        "Отменяет все твои активные заявки.\n\n"
        "💡 <b>Важно:</b> бот сохраняет мониторинги "
        "в базе данных и восстанавливает их после перезапуска."
    )

    await callback.message.answer(
        text,
        reply_markup=main_menu_keyboard(),
        parse_mode="HTML",
    )


async def cb_menu(
    callback: CallbackQuery,
    dialog_manager: DialogManager,
):
    from app.handlers.booking import on_home_dialog
    await on_home_dialog(callback, None, dialog_manager)



async def cmd_cancel(
    message: Message,
    dialog_manager: DialogManager,
):
    await _clear_previous_chat_messages(message.bot, message.chat.id, dialog_manager)
    try:
        if not dialog_manager.has_context():
            await message.answer(
                "ℹ️ Сейчас нет активного диалога.\n\n"
                "Для поиска билетов нажми «🔎 Найти билеты» или используй /book.",
                reply_markup=main_menu_keyboard(),
            )
            return

        data = dialog_manager.dialog_data
        task = data.get("search_task")
        if task and not task.done():
            task.cancel()

        job_id = data.get("booking_job_id")
        if job_id:
            jobs = BookingJobsService()
            job = await jobs.get_job(uuid.UUID(str(job_id)), chat_id=message.chat.id)
            if job and job.status in {"submitting", "order_unknown"}:
                data["menu_notice"] = (
                    "⚠️ Заказ уже отправлен или ожидает проверки. "
                    "Не закрываю браузер и не отменяю задачу, чтобы не потерять результат. "
                    "Проверьте историю заказов pass.rw.by."
                )
                await dialog_manager.switch_to(BookingSG.main_menu)
                return

            if job and job.status not in {"order_created", "failed", "cancelled", "expired"}:
                await jobs.cancel_job(uuid.UUID(str(job_id)), chat_id=message.chat.id)

            browser = dialog_manager.middleware_data.get("booking_browser_service")
            if browser:
                try:
                    await browser.close_session(str(job_id))
                except Exception:
                    logger.warning("Не удалось закрыть браузер при /cancel", exc_info=True)
            data.pop("booking_job_id", None)
            data.pop("booking_login", None)

        key = data.get("monitor_key")
        if key:
            monitor_service = dialog_manager.middleware_data.get("monitor_service")
            if monitor_service:
                await monitor_service.unsubscribe_from(str(key), message.chat.id)
            data.pop("monitor_key", None)

        data["menu_notice"] = "Текущий шаг отменён. Сохранённые мониторинги не затронуты, если они не были выбраны в этом диалоге."
        await dialog_manager.switch_to(BookingSG.main_menu)

    except Exception:
        logger.exception("Ошибка при отмене диалога")
        if dialog_manager.has_context():
            dialog_manager.dialog_data["menu_notice"] = (
                "Не удалось безопасно отменить текущую операцию. Проверьте историю бронирования."
            )
            await dialog_manager.switch_to(BookingSG.main_menu)
        else:
            await message.answer(
                "ℹ️ Не удалось отменить текущий диалог. Попробуй использовать /book.",
                reply_markup=main_menu_keyboard(),
            )


async def on_monitor_delete_after_booking(callback: CallbackQuery, monitor_service=None):
    """Отключает отложенное возобновление после успешного автобронирования."""
    raw_id = (callback.data or "").partition(":")[2]
    try:
        job_id = uuid.UUID(raw_id)
    except (TypeError, ValueError):
        await callback.answer("Некорректная команда удаления", show_alert=True)
        return

    jobs = BookingJobsService()
    monitor_key = await jobs.disable_monitor_resume(
        job_id, chat_id=callback.from_user.id
    )
    if monitor_key is None:
        await callback.answer("Мониторинг уже удалён или срок возобновления отменён", show_alert=True)
        return

    if monitor_service is not None:
        try:
            await monitor_service.unsubscribe_from(monitor_key, callback.from_user.id)
        except Exception:
            logger.exception("Не удалось дополнительно отключить мониторинг после заказа %s", job_id)

    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    await callback.answer("Мониторинг удалён. Повторный поиск не запустится.", show_alert=True)


def register_commands(router: Router):
    router.message.register(cmd_start, Command("start"))
    router.message.register(cmd_book, Command("book"))
    router.message.register(cmd_stop, Command("stop"))
    router.message.register(cmd_cancel, Command("cancel"))

    router.callback_query.register(
        cb_book,
        F.data == "menu:book",
    )
    router.callback_query.register(
        cb_monitors,
        F.data == "menu:monitors",
    )
    router.callback_query.register(
        cb_stop,
        F.data == "menu:stop",
    )
    router.callback_query.register(
        cb_help,
        F.data == "menu:help",
    )
    router.callback_query.register(
        cb_menu,
        F.data == "menu:home",
    )
    router.callback_query.register(
        on_monitor_delete_after_booking,
        F.data.startswith("mon_del:"),
    )
