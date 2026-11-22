from pathlib import Path
import asyncio
import logging
import shutil
import uuid

from app.services.booking_jobs import BookingJobsService
from app.services.monitor_profiles import MonitorProfileService
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html import escape
from typing import Any

from aiogram import F, Router
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)
from aiogram_dialog import DialogManager, ShowMode
from aiogram_dialog.widgets.kbd import Button

from app.config import Settings
from app.middlewares.message_cleanup import clear_chat_messages, cleanup_after_back
from app.services.monitoring import MonitorService
from app.services.rw_parser import RWParser, ScheduleUnavailable
from app.states import BookingSG
from app.stations import CITIES, normalize_station
from app.utils import (
    format_date,
    monitor_key,
    route_url,
    safe,
    validate_travel_date,
)

logger = logging.getLogger(__name__)
router = Router(name="booking")


def _force_edit_mode(manager: DialogManager) -> None:
    """Force editing the existing dialog message after text input."""
    manager.show_mode = ShowMode.EDIT


async def on_dialog_back_to(callback, button, manager, target_state):
    """Очищает старые сообщения и возвращает диалог на указанный шаг."""
    await cleanup_after_back(callback)
    manager.show_mode = ShowMode.EDIT
    await manager.switch_to(target_state)
    await callback.answer()


async def _show_input_error(manager: DialogManager, text: str) -> None:
    """Показывает ошибку в текущем окне диалога, не создавая новое сообщение."""
    manager.dialog_data["input_error"] = f"⚠️ {text}"
    await manager.show()


def _clear_input_error(manager: DialogManager) -> None:
    manager.dialog_data.pop("input_error", None)


def is_processing(manager: DialogManager) -> bool:
    if manager.dialog_data.get("processing"):
        return True

    manager.dialog_data["processing"] = True
    return False


def stop_processing(manager: DialogManager) -> None:
    manager.dialog_data["processing"] = False


def _cleanup_booking_screenshots(job_id: str) -> None:
    """Удаляет только каталог скриншотов указанной задачи бронирования."""
    try:
        safe_job_id = str(uuid.UUID(str(job_id)))
        root = (Path("data") / "booking_screenshots").resolve()
        target = (root / safe_job_id).resolve()

        # Защита от удаления файлов за пределами каталога скриншотов.
        if target.parent != root:
            raise ValueError("Некорректный путь каталога скриншотов")

        if target.is_dir():
            shutil.rmtree(target)
            logger.info(
                "Скриншоты задачи бронирования %s удалены",
                safe_job_id,
            )
    except FileNotFoundError:
        return
    except Exception:
        logger.exception(
            "Не удалось удалить скриншоты задачи бронирования %s",
            job_id,
        )


async def _delete_wagon_photo_messages(
    bot,
    chat_id: int,
    manager: DialogManager,
) -> None:
    """Удаляет все ранее отправленные фотографии вагонов из Telegram."""
    message_ids = manager.dialog_data.pop(
        "wagon_photo_message_ids",
        [],
    )
    for message_id in message_ids:
        try:
            await bot.delete_message(
                chat_id=chat_id,
                message_id=int(message_id),
            )
        except Exception:
            # Сообщение могло быть удалено пользователем или Telegram.
            logger.debug(
                "Не удалось удалить сообщение с вагоном: %s",
                message_id,
                exc_info=True,
            )


async def _send_wagon_screenshots(
    callback: CallbackQuery,
    manager: DialogManager,
    chat_id: int,
    carriages: list[dict],
) -> None:
    """Отправляет отдельное фото каждого вагона с кнопками свободных мест."""
    manager.dialog_data["wagon_photo_message_ids"] = []

    for carriage_index, carriage in enumerate(carriages):
        screenshot_path = Path(
            str(carriage.get("screenshot_path") or "")
        )
        if not screenshot_path.is_file():
            logger.warning(
                "Не найден скриншот вагона %s для отправки",
                carriage.get("carriage_number", "?"),
            )
            continue

        seats = carriage.get("seats") or []
        keyboard_rows = []
        current_row = []

        for seat_index, seat in enumerate(seats):
            seat_number = str(seat.get("number") or "").strip()
            if not seat_number:
                continue

            current_row.append(
                InlineKeyboardButton(
                    text=f"Место {seat_number}",
                    callback_data=(
                        f"booking_seat_scan:{carriage_index}:{seat_index}"
                    ),
                )
            )
            if len(current_row) == 3:
                keyboard_rows.append(current_row)
                current_row = []

        if current_row:
            keyboard_rows.append(current_row)

        # Навигация доступна непосредственно под каждой схемой вагона.
        # Возврат удаляет все схемы текущего сканирования, отмена —
        # безопасно отменяет текущую задачу бронирования.
        keyboard_rows.append([
            InlineKeyboardButton(
                text="◀️ Назад",
                callback_data="booking:seat_scan_back",
            ),
            InlineKeyboardButton(
                text="❌ Отмена",
                callback_data="booking:seat_scan_cancel",
            ),
        ])

        keyboard_rows.append([
            InlineKeyboardButton(
                text="🏠 Домой",
                callback_data="booking:home",
            ),
        ])

        reply_markup = (
            InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
            if keyboard_rows
            else None
        )

        caption = (
            f"🚃 <b>Вагон № {safe(carriage.get('carriage_number', '?'))}</b>\n"
            f"Свободных мест: <b>{len(seats)}</b>\n"
            "Выберите место на схеме вагона."
        )

        try:
            message = await callback.bot.send_photo(
                chat_id=chat_id,
                photo=FSInputFile(str(screenshot_path)),
                caption=caption,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
            manager.dialog_data["wagon_photo_message_ids"].append(
                message.message_id
            )
        except Exception:
            logger.exception(
                "Не удалось отправить фотографию вагона %s",
                carriage.get("carriage_number", "?"),
            )



def _format_price(price: float | int | str | Decimal) -> str:
    """Форматирует цену в белорусских рублях."""
    return f"{Decimal(str(price)):.2f}".replace(".", ",")


def _get_fare_text(train: dict) -> str:
    """Формирует текст с категориями мест и ценами."""
    options = train.get("fare_options", [])

    if not options:
        return "Цена не указана"

    lines = []

    for option in options:
        name = safe(option.get("name", "Места"))
        seats = int(option.get("seats", 0))
        price = option.get("price")

        if price is None:
            continue

        lines.append(
            f"{name}: {seats} мест — {_format_price(price)} BYN"
        )

    return "\n".join(lines) if lines else "Цена не указана"


def _passenger_summary_text(summary: dict) -> str:
    """Формирует сводку о пассажире для сообщения подтверждения."""
    if not summary:
        return ""

    fio_parts = [
        summary.get("last_name", ""),
        summary.get("first_name", ""),
        summary.get("middle_name", ""),
    ]
    fio = " ".join(p.strip() for p in fio_parts if p and p.strip()) or "—"

    return (
        f"🚃 Вагон: <b>{safe(summary.get('carriage_number') or '—')}</b>\n"
        f"🪑 Место: <b>{safe(summary.get('seat_number') or '—')}</b>\n"
        f"👤 ФИО: <b>{safe(fio)}</b>\n"
        f"📄 Документ: <b>{safe(summary.get('document_type') or '—')}</b>\n"
        f"🔢 Номер: <b>{safe(summary.get('document_number') or '—')}</b>"
    )


async def get_cities_data(**kwargs):
    return {"cities": CITIES}


async def on_city_selected(
    callback: CallbackQuery,
    widget: Any,
    manager: DialogManager,
    item_id: str,
):
    if is_processing(manager):
        await callback.answer("Подождите...")
        return

    try:
        station = normalize_station(item_id)

        if manager.current_context().state == BookingSG.station_from:
            manager.dialog_data["from_name"] = station
            await manager.switch_to(BookingSG.station_to)
        else:
            manager.dialog_data["to_name"] = station

            if station.casefold() == manager.dialog_data.get(
                "from_name", ""
            ).casefold():
                await callback.answer(
                    "Станции отправления и назначения должны различаться",
                    show_alert=True,
                )
                await manager.switch_to(BookingSG.station_to)
                return

            await manager.switch_to(BookingSG.travel_date)

        await callback.answer()

    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)

    finally:
        stop_processing(manager)


async def on_manual_from(
    message: Message,
    widget: Any,
    manager: DialogManager,
):
    _force_edit_mode(manager)
    try:
        manager.dialog_data["from_name"] = normalize_station(
            message.text or ""
        )
        _clear_input_error(manager)
        await manager.switch_to(BookingSG.station_to)

    except ValueError as exc:
        await _show_input_error(manager, str(exc))


async def on_manual_to(
    message: Message,
    widget: Any,
    manager: DialogManager,
):
    _force_edit_mode(manager)
    try:
        station = normalize_station(message.text or "")

        if station.casefold() == manager.dialog_data.get(
            "from_name", ""
        ).casefold():
            await _show_input_error(
                manager,
                "Станции отправления и назначения должны различаться. Введите другую станцию.",
            )
            return

        manager.dialog_data["to_name"] = station
        _clear_input_error(manager)
        await manager.switch_to(BookingSG.travel_date)

    except ValueError as exc:
        await _show_input_error(manager, str(exc))


async def _run_search(
    manager: DialogManager,
    chat_id: int,
    parser: RWParser,
):
    data = manager.dialog_data

    await manager.switch_to(BookingSG.searching)

    max_price = data.get("max_price")

    task = asyncio.create_task(
        parser.search_trains(
            data["from_name"],
            data["to_name"],
            data["travel_date"],
            max_price=max_price,
        )
    )

    data["search_task"] = task

    try:
        trains = await task

    except asyncio.CancelledError:
        logger.info("Поиск отменён пользователем")
        return

    except ScheduleUnavailable as exc:
        await manager.switch_to(BookingSG.travel_date)

        await manager.middleware_data["bot"].send_message(
            chat_id,
            f"Не удалось получить расписание: {safe(str(exc))}",
        )
        return

    except Exception:
        logger.exception("Непредвиденная ошибка поиска расписания")

        await manager.switch_to(BookingSG.travel_date)

        await manager.middleware_data["bot"].send_message(
            chat_id,
            "Не удалось выполнить поиск. "
            "Проверьте подключение и попробуйте ещё раз.",
        )
        return

    finally:
        if data.get("search_task") is task:
            data.pop("search_task", None)

    if not trains:
        await manager.switch_to(BookingSG.travel_date)

        await manager.middleware_data["bot"].send_message(
            chat_id,
            "По выбранному маршруту и дате не найдено поездов "
            "с местами в пределах указанной максимальной цены "
            f"{_format_price(max_price)} BYN за билет.",
        )
        return

    data["trains"] = trains

    data["base_link"] = route_url(
        data["from_name"],
        data["to_name"],
        data["travel_date"],
    )

    data["travel_date_fmt"] = format_date(data["travel_date"])

    await manager.switch_to(BookingSG.choose_train)


async def on_date_selected(
    callback: CallbackQuery,
    widget: Any,
    manager: DialogManager,
    item_id: str,
):
    try:
        validate_travel_date(item_id)

        manager.dialog_data["travel_date"] = item_id
        _clear_input_error(manager)

        await callback.answer()
        await manager.switch_to(BookingSG.ticket_count)

    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)


async def on_manual_date(
    message: Message,
    widget: Any,
    manager: DialogManager,
):
    _force_edit_mode(manager)
    try:
        parsed = validate_travel_date(message.text or "")

        manager.dialog_data["travel_date"] = parsed.isoformat()
        _clear_input_error(manager)

        await manager.switch_to(BookingSG.ticket_count)

    except ValueError as exc:
        await _show_input_error(manager, str(exc))


async def get_trains_data(
    dialog_manager: DialogManager,
    **kwargs,
):
    data = dialog_manager.dialog_data
    items = []

    for train in data.get("trains", []):
        if train["departed"]:
            status = "🛑 Поезд отправился"

        elif train["seats"] > 0:
            details = ", ".join(
                f"{safe(k)} ({v})"
                for k, v in train["details"].items()
            )
            status = f"🟢 {details or 'Есть места'}"

        else:
            status = "🔴 Нет мест"

        min_price = train.get("min_price")

        price_text = (
            f" | от {_format_price(min_price)} BYN"
            if min_price is not None
            else ""
        )

        label = (
            f"{train['index']}. {safe(train['number'])} "
            f"({train['time']} — {train['arr_time']}) "
            f"{status}{price_text}"
        )

        items.append((label, str(train["index"])))

    return {
        "from_name": safe(data.get("from_name", "—")),
        "to_name": safe(data.get("to_name", "—")),
        "travel_date_fmt": data.get("travel_date_fmt", "—"),
        "train_items": items,
        "input_error": data.get("input_error", ""),
    }


async def get_monitoring_info_data(
    dialog_manager: DialogManager,
    **kwargs,
):
    data = dialog_manager.dialog_data

    return {
        "monitor_text": data.get("monitor_text", ""),
        "alt_items": data.get("alt_items", []),
    }


async def get_car_types_data(
    dialog_manager: DialogManager,
    **kwargs,
):
    available_types = dialog_manager.dialog_data.get(
        "available_car_types", []
    )

    items = []
    car_type_id_map = {}

    for index, item in enumerate(available_types):
        short_id = f"ct{index}"
        name = safe(item.get("name", "Тип вагона"))
        seats = safe(item.get("seats", ""))
        label = f"{name} — мест: {seats or 'не указано'}"

        car_type_id_map[short_id] = str(item["car_type"])
        items.append((label, short_id))

    dialog_manager.dialog_data["car_type_id_map"] = car_type_id_map

    return {"car_type_items": items}


def _car_type_keyboard(available_types: list) -> InlineKeyboardMarkup:
    """Кнопки типов вагонов для явной отправки."""
    rows = []
    for index, item in enumerate(available_types):
        name = safe(item.get("name", "Тип вагона"))
        seats = safe(item.get("seats", ""))
        label = f"{name} — мест: {seats or '?'}"
        if len(label) > 60:
            label = label[:57] + "..."
        rows.append([
            InlineKeyboardButton(
                text=label,
                callback_data=f"booking_car_type:{index}",
            )
        ])
    rows.append([
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data="booking_car_type_cancel",
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _seat_positions_for_type(type_name: str) -> list[str]:
    name = str(type_name or "").lower().replace("ё", "е").strip()
    if "сидяч" in name:
        return ["Любое"]
    if "плац" in name:
        return [
            "Любое",
            "Нижнее",
            "Верхнее",
            "Нижнее боковое",
            "Верхнее боковое",
        ]
    if "купе" in name:
        return ["Любое", "Нижнее", "Верхнее"]
    return ["Любое"]


def _seat_position_keyboard(type_name: str) -> InlineKeyboardMarkup:
    rows = []
    for option in _seat_positions_for_type(type_name):
        rows.append([
            InlineKeyboardButton(
                text=option,
                callback_data=f"booking_seat_pos:{option}",
            )
        ])
    rows.append([
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data="booking_car_type_cancel",
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def get_train_result_data(
    dialog_manager: DialogManager,
    **kwargs,
):
    data = dialog_manager.dialog_data

    return {
        "result_text": data.get("result_text", ""),
        "base_link": data.get("base_link", "https://pass.rw.by"),
        "input_error": data.get("input_error", ""),
    }


def _alternative_items(
    trains: list[dict],
    excluded_number: str,
) -> list[tuple[str, str]]:
    result = []

    for train in trains:
        if (
            train["number"] == excluded_number
            or train["departed"]
            or train["seats"] <= 0
        ):
            continue

        details = ", ".join(
            f"{safe(k)} ({v})"
            for k, v in train["details"].items()
        )

        min_price = train.get("min_price")
        price_text = (
            f" | от {_format_price(min_price)} BYN"
            if min_price is not None
            else ""
        )

        result.append(
            (
                f"{train['index']}. {safe(train['number'])} "
                f"({train['time']} — {train['arr_time']}) "
                f"🟢 {details}{price_text}",
                str(train["index"]),
            )
        )

    return result


async def _start_monitoring(
    manager: DialogManager,
    count: int,
    chat_id: int,
):
    settings: Settings = manager.middleware_data["settings"]

    if not 1 <= count <= settings.max_tickets_per_request:
        await _show_input_error(
            manager,
            f"Количество билетов должно быть от 1 до {settings.max_tickets_per_request}.",
        )
        return

    data = manager.dialog_data

    try:
        max_price = Decimal(
            str(data.get("max_price", "")).replace(",", ".")
        )
        if not max_price.is_finite() or max_price <= 0:
            raise ValueError
        max_price = max_price.quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if max_price > Decimal("99999999.99"):
            raise ValueError
    except (InvalidOperation, ValueError, TypeError):
        manager.dialog_data["input_error"] = (
            "⚠️ Не удалось определить максимальную цену. "
            "Введите максимальную цену за один билет."
        )
        await manager.switch_to(BookingSG.max_price)
        return

    selected_train = data.get("selected_train")
    if not selected_train:
        manager.dialog_data["input_error"] = (
            "⚠️ Не удалось определить выбранный поезд. "
            "Вернитесь к списку поездов и выберите его повторно."
        )
        await manager.switch_to(BookingSG.choose_train)
        return

    monitor_service: MonitorService = manager.middleware_data["monitor_service"]
    from_name = data["from_name"]
    to_name = data["to_name"]
    travel_date = data["travel_date"]
    train_number = selected_train["number"]
    key = monitor_key(from_name, to_name, travel_date, train_number)
    url = route_url(from_name, to_name, travel_date)

    try:
        position, created = await monitor_service.subscribe(
            key=key,
            from_name=from_name,
            to_name=to_name,
            travel_date=travel_date,
            train=selected_train,
            url=url,
            chat_id=chat_id,
            tickets_count=count,
            max_price=max_price,
        )
    except Exception:
        logger.exception(
            "Не удалось создать мониторинг поезда %s", train_number
        )
        await _show_input_error(
            manager,
            "Не удалось запустить мониторинг. Попробуйте ещё раз.",
        )
        return

    _clear_input_error(manager)
    data["monitor_key"] = key
    data["tickets_count"] = count
    data["max_price"] = str(max_price)

    status_text = (
        "Мониторинг успешно запущен."
        if created
        else "Существующая заявка обновлена."
    )
    if monitor_service.auto_booking_enabled:
        action_text = (
            "Когда появятся подходящие места, бот автоматически попробует "
            "оформить заказ по сохранённым данным pass.rw.by. После "
            "подтверждения сайта бот пришлёт отдельное уведомление с номером "
            "заказа и сроком оплаты."
        )
    else:
        action_text = (
            "Когда появятся подходящие места, бот отправит отдельное "
            "уведомление. Оно не резервирует билеты."
        )

    data["monitor_text"] = (
        f"🔔 <b>Мониторинг поезда {safe(train_number)}</b>\n\n"
        f"🚉 Отправление: <b>{safe(from_name)}</b>\n"
        f"🏙️ Назначение: <b>{safe(to_name)}</b>\n"
        f"📅 Дата: <b>{format_date(travel_date)}</b>\n"
        f"🎟 Билетов требуется: <b>{count}</b>\n"
        f"💰 Максимальная цена за билет: <b>{_format_price(max_price)} BYN</b>\n\n"
        f"✅ {status_text}\n"
        f"📋 Ваша позиция в очереди: <b>{position}</b>\n\n"
        f"{action_text}\n\n"
        "⚠️ Наличие мест может измениться до оформления. "
        "Билет считается оформленным только после подтверждения сайта."
    )
    await manager.switch_to(BookingSG.monitoring_info)


async def on_train_selected(
    callback: CallbackQuery,
    widget: Any,
    manager: DialogManager,
    item_id: str,
):
    if is_processing(manager):
        await callback.answer("Подождите...")
        return

    try:
        trains = manager.dialog_data.get("trains", [])

        try:
            selected = next(
                t for t in trains
                if t["index"] == int(item_id)
            )

        except (ValueError, StopIteration):
            await callback.answer(
                "Поезд не найден",
                show_alert=True,
            )
            return

        if selected["departed"]:
            await callback.answer(
                "Этот поезд уже отправился",
                show_alert=True,
            )
            return

        manager.dialog_data["selected_train"] = selected

        if selected["seats"] > 0:
            _set_result(manager, selected)

            await manager.switch_to(BookingSG.train_result)
            await callback.answer()
            return

        manager.dialog_data["alt_items"] = _alternative_items(
            trains,
            selected["number"],
        )

        tickets_count = manager.dialog_data.get("tickets_count")

        logger.info(
            "Выбран поезд %s. Сохранённое количество билетов: %s",
            selected["number"],
            tickets_count,
        )

        await callback.answer()

        if tickets_count is not None:
            chat_id = (
                callback.message.chat.id
                if callback.message
                else callback.from_user.id
            )

            await _start_monitoring(
                manager=manager,
                count=int(tickets_count),
                chat_id=chat_id,
            )

        else:
            await manager.switch_to(
                BookingSG.monitor_ticket_count
            )

    finally:
        stop_processing(manager)


def _set_result(
    manager: DialogManager,
    train: dict,
) -> None:
    data = manager.dialog_data

    details = ", ".join(
        f"{safe(k)} ({v})"
        for k, v in train["details"].items()
    ) or "Наличие подтверждено"

    fare_text = _get_fare_text(train)

    max_price = data.get("max_price")
    max_price_text = (
        f"\n💰 Максимальная цена: "
        f"<b>{_format_price(max_price)} BYN</b>"
        if max_price is not None
        else ""
    )

    data["result_text"] = (
        f"🎯 Выбран поезд <b>{safe(train['number'])}</b> "
        f"({train['time']} — {train['arr_time']}).\n"
        f"✅ Отправление: <b>{safe(data['from_name'])}</b>\n"
        f"✅ Назначение: <b>{safe(data['to_name'])}</b>\n"
        f"📅 Дата: <b>{data['travel_date_fmt']}</b>\n\n"
        f"🟢 Подходящие места:\n{fare_text}\n\n"
        f"Всего мест в пределах лимита: <b>{train['seats']}</b>"
        f"{max_price_text}\n\n"
        "Наличие может измениться до оформления заказа."
    )


async def on_ticket_count(
    message: Message,
    widget: Any,
    manager: DialogManager,
):
    _force_edit_mode(manager)
    settings: Settings = manager.middleware_data["settings"]

    try:
        count = int((message.text or "").strip())

        if not 1 <= count <= settings.max_tickets_per_request:
            raise ValueError

    except ValueError:
        await _show_input_error(
            manager,
            f"Введите целое число от 1 до {settings.max_tickets_per_request}.",
        )
        return

    _clear_input_error(manager)
    manager.dialog_data["tickets_count"] = count

    logger.info(
        "Пользователь указал количество билетов: %s",
        count,
    )

    await manager.switch_to(BookingSG.max_price)


async def on_monitor_ticket_count(
    message: Message,
    widget: Any,
    manager: DialogManager,
):
    _force_edit_mode(manager)
    settings: Settings = manager.middleware_data["settings"]

    try:
        count = int((message.text or "").strip())

        if not 1 <= count <= settings.max_tickets_per_request:
            raise ValueError

    except ValueError:
        await _show_input_error(
            manager,
            f"Введите целое число от 1 до {settings.max_tickets_per_request}.",
        )
        return

    _clear_input_error(manager)
    manager.dialog_data["tickets_count"] = count

    await _start_monitoring(
        manager=manager,
        count=count,
        chat_id=message.chat.id,
    )


async def on_max_price(
    message: Message,
    widget: Any,
    manager: DialogManager,
):
    _force_edit_mode(manager)
    try:
        price = Decimal(
            (message.text or "").strip().replace(",", ".")
        )

        if not price.is_finite() or price < Decimal("1") or price > Decimal("10000"):
            raise ValueError

        price = price.quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )

        if price > Decimal("99999999.99"):
            raise ValueError

    except (InvalidOperation, ValueError):
        await _show_input_error(
            manager,
            "Введите сумму за один билет от 1 до 10 000 BYN, например: 35 или 35.50.",
        )
        return

    _clear_input_error(manager)
    manager.dialog_data["max_price"] = str(price)

    await _run_search(
        manager,
        message.chat.id,
        manager.middleware_data["rw_parser"],
    )


async def on_alt_selected(
    callback: CallbackQuery,
    widget: Any,
    manager: DialogManager,
    item_id: str,
):
    try:
        selected = next(
            t for t in manager.dialog_data.get("trains", [])
            if t["index"] == int(item_id)
        )

    except (ValueError, StopIteration):
        await callback.answer(
            "Поезд не найден",
            show_alert=True,
        )
        return

    if selected["departed"] or selected["seats"] <= 0:
        await callback.answer(
            "Этот поезд уже недоступен",
            show_alert=True,
        )
        return

    manager.dialog_data["selected_train"] = selected

    _set_result(manager, selected)

    await manager.switch_to(BookingSG.train_result)
    await callback.answer()


async def on_back_from_search(
    callback: CallbackQuery,
    button: Button,
    manager: DialogManager,
):
    task = manager.dialog_data.get("search_task")

    if task and not task.done():
        task.cancel()

    await cleanup_after_back(callback)
    manager.show_mode = ShowMode.EDIT
    await manager.switch_to(BookingSG.travel_date)


async def on_back_from_monitoring(
    callback: CallbackQuery,
    button: Button,
    manager: DialogManager,
):
    key = manager.dialog_data.get("monitor_key")

    if key:
        await manager.middleware_data[
            "monitor_service"
        ].unsubscribe_from(
            key,
            callback.message.chat.id,
        )

    await cleanup_after_back(callback)
    manager.show_mode = ShowMode.EDIT
    await manager.switch_to(BookingSG.choose_train)


async def on_back_dialog(
    callback: CallbackQuery,
    button: Button,
    manager: DialogManager,
):
    """Надёжно возвращает пользователя на предыдущий шаг диалога."""
    state = manager.current_context().state
    state_name = getattr(state, "state", state)

    previous_states = {
        BookingSG.monitor_ticket_count.state: BookingSG.choose_train,
        BookingSG.booking_login.state: BookingSG.train_result,
        BookingSG.booking_password.state: BookingSG.booking_login,
        BookingSG.booking_choose_car_type.state: BookingSG.train_result,
        BookingSG.booking_choose_seat_position.state: BookingSG.booking_choose_car_type,
        BookingSG.booking_choose_seat.state: BookingSG.booking_choose_seat_position,
        BookingSG.booking_passenger_last_name.state: BookingSG.booking_choose_seat_position,
        BookingSG.booking_passenger_first_name.state: BookingSG.booking_passenger_last_name,
        BookingSG.booking_passenger_middle_name.state: BookingSG.booking_passenger_first_name,
        BookingSG.booking_passenger_document_type.state: BookingSG.booking_passenger_middle_name,
        BookingSG.booking_passenger_document_number.state: BookingSG.booking_passenger_document_type,
        BookingSG.booking_bedding.state: BookingSG.booking_passenger_document_number,
        BookingSG.booking_passenger_confirmation.state: BookingSG.booking_bedding,
    }

    if state_name == BookingSG.station_from.state:
        await cleanup_after_back(callback)
        manager.show_mode = ShowMode.EDIT
        await manager.switch_to(BookingSG.main_menu)
        await callback.answer()
        return

    target = previous_states.get(state_name)
    if target is None:
        await callback.answer(
            "Для этого шага возврат недоступен.",
            show_alert=True,
        )
        return

    await cleanup_after_back(callback)
    manager.show_mode = ShowMode.EDIT
    await manager.switch_to(target)
    await callback.answer()


async def on_back_from_seat_scan(
    callback: CallbackQuery,
    button: Button,
    manager: DialogManager,
):
    """Возвращает к выбору расположения и очищает старые схемы вагонов."""
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    job_id = manager.dialog_data.get("booking_job_id")

    await _delete_wagon_photo_messages(callback.bot, chat_id, manager)
    if job_id:
        _cleanup_booking_screenshots(str(job_id))

    manager.dialog_data["booking_seat_scan"] = []
    manager.dialog_data["seat_scan_notice"] = ""
    manager.dialog_data.pop("picked_seats", None)
    await cleanup_after_back(callback)
    manager.show_mode = ShowMode.EDIT
    await manager.switch_to(BookingSG.booking_choose_seat_position)
    await callback.answer()


async def on_back_to_seat_position(
    callback: CallbackQuery,
    button: Button,
    manager: DialogManager,
):
    """Возвращает со ввода ФИО к выбору места только после возврата браузера."""
    job_id = manager.dialog_data.get("booking_job_id")
    browser_service = manager.middleware_data.get("booking_browser_service")
    if not job_id or browser_service is None:
        await callback.answer(
            "Не удалось восстановить страницу выбора места.",
            show_alert=True,
        )
        return

    try:
        await browser_service.return_to_seat_selection(str(job_id))
    except Exception:
        logger.exception("Не удалось вернуться к выбору места на сайте")
        await callback.answer(
            "Сайт не позволил вернуться к выбору места. Текущий ввод пассажира сохранён.",
            show_alert=True,
        )
        return

    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    await _delete_wagon_photo_messages(callback.bot, chat_id, manager)
    _cleanup_booking_screenshots(str(job_id))
    manager.dialog_data["booking_seat_scan"] = []
    manager.dialog_data["seat_scan_notice"] = ""
    manager.dialog_data.pop("selected_carriage", None)
    manager.dialog_data.pop("selected_seat_number", None)
    manager.dialog_data.pop("current_passenger_summary", None)
    await cleanup_after_back(callback)
    manager.show_mode = ShowMode.EDIT
    await manager.switch_to(BookingSG.booking_choose_seat_position)
    await callback.answer()


async def on_cancel_dialog(
    callback: CallbackQuery,
    button: Button,
    manager: DialogManager,
):
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    keep_message_id = None
    try:
        keep_message_id = manager.current_stack().last_message_id
    except Exception:
        pass
    await clear_chat_messages(
        callback.bot,
        chat_id,
        keep_message_id=int(keep_message_id) if keep_message_id else None,
    )
    await _delete_wagon_photo_messages(callback.bot, chat_id, manager)

    task = manager.dialog_data.get("search_task")
    if task and not task.done():
        task.cancel()

    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    booking_job_id = manager.dialog_data.get("booking_job_id")
    if booking_job_id:
        jobs = BookingJobsService()
        try:
            job = await jobs.get_job(uuid.UUID(str(booking_job_id)), chat_id=chat_id)
        except Exception:
            logger.exception("Не удалось проверить состояние задачи перед отменой")
            await callback.answer(
                "Не удалось проверить состояние заказа. Не закрываю браузер, чтобы не потерять результат отправки.",
                show_alert=True,
            )
            return

        if job and job.status in {"submitting", "order_unknown"}:
            await callback.answer(
                "Заказ уже отправлен или ожидает проверки. Отмена заблокирована, чтобы не создать повторный заказ. Проверьте историю заказов на pass.rw.by.",
                show_alert=True,
            )
            return

        if job and job.status not in {"order_created", "failed", "cancelled", "expired"}:
            try:
                await jobs.cancel_job(uuid.UUID(str(booking_job_id)), chat_id=chat_id)
            except Exception:
                logger.exception("Не удалось отменить задачу %s", booking_job_id)
                await callback.answer(
                    "Не удалось безопасно отменить задачу. Проверьте её статус в истории.",
                    show_alert=True,
                )
                return

        browser_service = manager.middleware_data.get("booking_browser_service")
        if browser_service:
            try:
                await browser_service.close_session(str(booking_job_id))
            except Exception:
                logger.warning("Не удалось закрыть браузер заявки", exc_info=True)

        manager.dialog_data.pop("booking_job_id", None)
        manager.dialog_data.pop("booking_login", None)

    key = manager.dialog_data.get("monitor_key")
    if key:
        monitor_service = manager.middleware_data.get("monitor_service")
        if monitor_service:
            try:
                await monitor_service.unsubscribe_from(str(key), chat_id)
            except Exception:
                logger.exception("Не удалось остановить мониторинг при отмене диалога")
        manager.dialog_data.pop("monitor_key", None)

    _clear_input_error(manager)
    await manager.switch_to(BookingSG.main_menu)
    await callback.answer("Текущий шаг отменён.")


async def on_back_to_trains(
    callback: CallbackQuery,
    button: Button,
    manager: DialogManager,
):
    await cleanup_after_back(callback)
    manager.show_mode = ShowMode.EDIT
    await manager.switch_to(BookingSG.choose_train)
    await callback.answer()


async def on_start_auto_booking(
    callback: CallbackQuery,
    widget: Any,
    manager: DialogManager,
):
    if is_processing(manager):
        await callback.answer("Подождите...")
        return

    try:
        data = manager.dialog_data
        train = data.get("selected_train")

        if not train or train.get("departed") or train.get("seats", 0) <= 0:
            await callback.answer(
                "Этот поезд больше недоступен для оформления.",
                show_alert=True,
            )
            return

        settings: Settings = manager.middleware_data["settings"]
        count = int(data.get("tickets_count") or 0)

        if not 1 <= count <= settings.max_tickets_per_request:
            await callback.answer(
                "Не удалось определить количество билетов. "
                "Начните поиск заново.",
                show_alert=True,
            )
            return

        await callback.answer("Начинаю оформление...")

        chat_id = (
            callback.message.chat.id
            if callback.message
            else callback.from_user.id
        )

        browser_service = manager.middleware_data.get(
            "booking_browser_service"
        )
        if browser_service is None:
            raise RuntimeError("Сервис браузера не подключён.")

        jobs = BookingJobsService()
        existing_id = data.get("booking_job_id")
        job = None

        if existing_id:
            try:
                job = await jobs.get_job(
                    uuid.UUID(str(existing_id)),
                    chat_id=chat_id,
                )
            except (ValueError, TypeError):
                job = None

        if job and job.status in {"running", "waiting"}:
            preferences = dict(job.preferences or {})
            saved_count = int(
                preferences.get("ticket_count") or count
            )

            if not 1 <= saved_count <= settings.max_tickets_per_request:
                raise ValueError("Некорректное количество билетов.")

            preferences["ticket_count"] = saved_count
            preferences.setdefault("orders_target", saved_count)
            preferences.setdefault("orders_completed", 0)

            await jobs.update_job_state(
                uuid.UUID(str(job.id)),
                current_step="ticket_count_selected",
                preferences=preferences,
                chat_id=chat_id,
            )

            count = saved_count

        else:
            from_name = data["from_name"]
            to_name = data["to_name"]
            travel_date = data["travel_date"]

            job = await jobs.create_job(
                chat_id=chat_id,
                from_name=from_name,
                to_name=to_name,
                travel_date=travel_date,
                train_number=str(train["number"]),
                url=route_url(
                    from_name,
                    to_name,
                    travel_date,
                ),
                train_data=train,
                preferences={
                    "source": "telegram",
                    "booking_mode": "automatic",
                    "ticket_count": count,
                    "orders_target": count,
                    "orders_completed": 0,
                },
                status="running",
                current_step="ticket_count_selected",
            )

            data["booking_job_id"] = str(job.id)

        data["booking_train_data"] = dict(job.train_data or {})
        data["booking_travel_date"] = str(job.travel_date or "")
        data["booking_from_name"] = str(job.from_name or "")
        data["booking_to_name"] = str(job.to_name or "")

        await browser_service.open_auth_form(
            str(job.id),
            job.url,
        )

        await manager.switch_to(BookingSG.booking_login)

    except Exception:
        logger.exception(
            "Не удалось начать автоматическое оформление"
        )
        await _show_input_error(
            manager,
            "Не удалось начать оформление. Проверьте браузер и попробуйте ещё раз.",
        )

    finally:
        stop_processing(manager)


async def on_booking_login(
    message: Message,
    widget: Any,
    manager: DialogManager,
):
    _force_edit_mode(manager)
    login = (message.text or "").strip()

    if not login or len(login) > 200:
        await _show_input_error(manager, "Введите корректный логин.")
        return

    _clear_input_error(manager)
    manager.dialog_data["booking_login"] = login
    await manager.switch_to(BookingSG.booking_password)


async def on_booking_password(
    message: Message,
    widget: Any,
    manager: DialogManager,
):
    _force_edit_mode(manager)
    password = message.text or ""
    try:
        await message.delete()
    except Exception:
        logger.debug("Не удалось немедленно удалить сообщение с паролем", exc_info=True)

    if not password:
        await _show_input_error(manager, "Пароль пуст. Введите его ещё раз.")
        return

    login = manager.dialog_data.get("booking_login")
    job_id = manager.dialog_data.get("booking_job_id")

    if not login or not job_id:
        await _show_input_error(manager, "Не найдены данные заявки. Начните оформление заново.")
        return

    browser_service = manager.middleware_data.get(
        "booking_browser_service"
    )

    if browser_service is None:
        await _show_input_error(manager, "Браузерный сервис недоступен.")
        return

    credential_password = password
    try:
        auth_result = await browser_service.submit_credentials(
            str(job_id),
            login,
            password,
        )
    except Exception:
        logger.warning(
            "Не удалось отправить форму авторизации",
            exc_info=False,
        )
        credential_password = ""
        await _show_input_error(
            manager,
            "Не удалось выполнить авторизацию. Попробуйте ещё раз или начните оформление заново.",
        )
        return
    finally:
        password = ""

    if auth_result == "user_not_found":
        credential_password = ""
        manager.dialog_data.pop("booking_login", None)
        manager.dialog_data["input_error"] = (
            "❌ Сайт сообщил: «Пользователь не найден». Проверьте логин и введите его ещё раз."
        )
        await manager.switch_to(BookingSG.booking_login)
        return

    if auth_result == "invalid_credentials":
        credential_password = ""
        await _show_input_error(
            manager,
            "❌ Сайт сообщил: «Неверное имя пользователя или пароль». Проверьте пароль и отправьте его ещё раз.",
        )
        return

    if auth_result != "authorized":
        credential_password = ""
        await _show_input_error(
            manager,
            "❌ Сайт не подтвердил авторизацию автоматически. Проверьте данные и попробуйте повторить вход позже.",
        )
        return

    # Сохраняем только те учётные данные, которые сайт уже принял.
    # Пароль не помещается в FSM/dialog_data и не выводится в журнал.
    try:
        await MonitorProfileService().save_credentials(
            message.chat.id,
            login,
            credential_password,
        )
    except Exception:
        logger.warning(
            "Не удалось сохранить зашифрованный профиль pass.rw.by",
            exc_info=True,
        )
    finally:
        credential_password = ""
        password = ""

    manager.dialog_data.pop("booking_login", None)
    _clear_input_error(manager)

    try:
        chat_id = message.chat.id
        jobs = BookingJobsService()

        job = await jobs.get_job(
            uuid.UUID(str(job_id)),
            chat_id=chat_id,
        )

        if job is None:
            raise RuntimeError("Заявка не найдена.")

        train_data = job.train_data or {}

        if not train_data:
            train_data = (
                manager.dialog_data.get("selected_train") or {}
            )

        await browser_service.open_selected_train_places(
            str(job_id),
            train_data,
        )

        available_types = await browser_service.get_available_car_types(
            str(job_id)
        )

        if not available_types:
            raise RuntimeError("Сайт не вернул типы вагонов.")

        manager.dialog_data["available_car_types"] = available_types
        manager.dialog_data["car_type_id_map"] = {
            f"ct{i}": str(item["car_type"])
            for i, item in enumerate(available_types)
        }

        manager.dialog_data["picked_seats"] = []
        manager.dialog_data["passenger_count"] = 1
        manager.dialog_data["passenger_index"] = 1

        await jobs.update_job_state(
            uuid.UUID(str(job_id)),
            status="running",
            current_step="car_types_loaded",
            chat_id=chat_id,
        )

        try:
            await manager.switch_to(BookingSG.booking_choose_car_type)
        except Exception:
            logger.exception("Не удалось показать окно выбора типа вагона")
            await _show_input_error(
                manager,
                "Авторизация выполнена, но не удалось открыть выбор типа вагона. Повторите попытку.",
            )

    except Exception as exc:
        logger.warning(
            "Не удалось перейти к выбору мест: %s",
            type(exc).__name__,
            exc_info=False,
        )

        await _show_input_error(
            manager,
            "Авторизация выполнена, но не удалось загрузить варианты вагонов. Проверьте страницу Chromium.",
        )


@router.callback_query(F.data.startswith("booking_car_type:"))
async def on_booking_car_type_button(
    callback: CallbackQuery,
    dialog_manager: DialogManager,
):
    """Явные кнопки типа вагона."""
    manager = dialog_manager

    if not callback.data or not callback.message:
        await callback.answer("Некорректный запрос.", show_alert=True)
        return

    try:
        index = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный тип вагона.", show_alert=True)
        return

    available_types = manager.dialog_data.get("available_car_types") or []
    if not (0 <= index < len(available_types)):
        await callback.answer(
            "Тип вагона больше недоступен. Начните /book заново.",
            show_alert=True,
        )
        return

    selected = available_types[index]
    actual_car_type = str(selected.get("car_type", ""))
    type_name = str(selected.get("name", ""))

    manager.dialog_data["selected_car_type"] = {
        "name": type_name,
        "car_type": actual_car_type,
        "seats": selected.get("seats", ""),
    }
    manager.dialog_data.setdefault("car_type_id_map", {})
    manager.dialog_data["car_type_id_map"][f"ct{index}"] = actual_car_type

    try:
        await callback.answer("Выбираю тип вагона...")
    except Exception:
        pass

    job_id = manager.dialog_data.get("booking_job_id")
    browser_service = manager.middleware_data.get("booking_browser_service")

    try:
        await manager.switch_to(
            BookingSG.booking_choose_seat_position,
        )
    except Exception:
        logger.exception("Не удалось переключить диалог на выбор места")

    if job_id and browser_service is not None:
        try:
            confirmed = await browser_service.select_car_type(
                str(job_id),
                actual_car_type,
            )
            manager.dialog_data["selected_car_type"] = confirmed
        except Exception:
            logger.exception(
                "Не удалось выбрать тип вагона на сайте: %s",
                actual_car_type,
            )
            await _show_input_error(
                manager,
                "На сайте тип вагона пока не подтверждён. При поиске бот попробует выбрать его повторно.",
            )


@router.callback_query(F.data == "booking_car_type_cancel")
async def on_booking_car_type_cancel(
    callback: CallbackQuery,
    dialog_manager: DialogManager,
):
    job_id = dialog_manager.dialog_data.get("booking_job_id")
    browser_service = dialog_manager.middleware_data.get("booking_browser_service")
    if job_id and browser_service:
        try:
            await browser_service.close_session(str(job_id))
        except Exception:
            pass

    try:
        await dialog_manager.done()
    except Exception:
        pass
    await callback.answer("Оформление отменено. Для нового поиска нажмите /book.")


async def on_car_type_selected(
    callback: CallbackQuery,
    widget: Any,
    manager: DialogManager,
    item_id: str,
):
    job_id = manager.dialog_data.get("booking_job_id")

    if not job_id:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return

    available_types = manager.dialog_data.get(
        "available_car_types", []
    )

    car_type_id_map = manager.dialog_data.get(
        "car_type_id_map", {}
    )
    actual_car_type = car_type_id_map.get(str(item_id))

    selected = next(
        (
            item for item in available_types
            if str(item.get("car_type")) == str(actual_car_type)
        ),
        None,
    )

    if selected is None:
        await callback.answer(
            "Этот тип вагона больше не доступен.",
            show_alert=True,
        )
        return

    browser_service = manager.middleware_data.get(
        "booking_browser_service"
    )

    if browser_service is None:
        await callback.answer(
            "Браузерный сервис недоступен.",
            show_alert=True,
        )
        return

    manager.dialog_data["selected_car_type"] = {
        "name": selected.get("name", ""),
        "car_type": str(actual_car_type),
        "seats": selected.get("seats", ""),
    }

    try:
        await callback.answer("Выбираю тип вагона...")
    except Exception:
        pass

    try:
        await BookingJobsService().update_job_state(
            uuid.UUID(str(job_id)),
            current_step="car_type_selected",
            chat_id=callback.message.chat.id,
        )
    except Exception:
        logger.exception("Не удалось обновить состояние задачи")

    try:
        await manager.switch_to(
            BookingSG.booking_choose_seat_position,
        )
    except Exception:
        logger.exception("Не удалось переключить диалог на выбор места")

    try:
        confirmed = await browser_service.select_car_type(
            str(job_id),
            str(actual_car_type),
        )
        manager.dialog_data["selected_car_type"] = confirmed
    except Exception:
        logger.exception(
            "Не удалось выбрать тип вагона на сайте: job_id=%s, car_type=%s",
            job_id,
            item_id,
        )
        await _show_input_error(
            manager,
            "На сайте тип вагона пока не подтверждён. При поиске бот попробует выбрать его повторно.",
        )


async def get_seat_position_data(dialog_manager, **kwargs):
    selected_type = dialog_manager.dialog_data.get(
        "selected_car_type", {}
    )

    type_name = (
        str(selected_type.get("name", ""))
        .lower()
        .replace("ё", "е")
        .strip()
    )

    if "сидяч" in type_name:
        options = ["Любое"]
    elif "плац" in type_name:
        options = [
            "Любое",
            "Нижнее",
            "Верхнее",
            "Нижнее боковое",
            "Верхнее боковое",
        ]
    elif "купе" in type_name:
        options = ["Любое", "Нижнее", "Верхнее"]
    else:
        options = ["Любое"]

    return {
        "seat_position_items": [
            (option, option)
            for option in options
        ],
        "selected_car_type_name": safe(
            selected_type.get("name", "не указан")
        ),
        "seat_scan_notice": dialog_manager.dialog_data.get("seat_scan_notice", ""),
        "input_error": dialog_manager.dialog_data.get("input_error", ""),
    }


@router.callback_query(F.data.startswith("booking_seat_pos:"))
async def on_booking_seat_pos_button(
    callback: CallbackQuery,
    dialog_manager: DialogManager,
):
    """Явные кнопки расположения места (купе / плацкарт)."""
    manager = dialog_manager

    if not callback.data:
        await callback.answer("Некорректный запрос.", show_alert=True)
        return

    position = callback.data.split(":", 1)[1]
    allowed = {
        "Любое",
        "Нижнее",
        "Верхнее",
        "Нижнее боковое",
        "Верхнее боковое",
    }
    if position not in allowed:
        await callback.answer("Некорректное расположение.", show_alert=True)
        return

    await on_seat_position_selected(callback, None, manager, position)


async def on_seat_position_selected(
    callback: CallbackQuery,
    widget: Any,
    manager: DialogManager,
    item_id: str,
):
    job_id = manager.dialog_data.get("booking_job_id")
    if not job_id:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return

    # Блокируем повторный запуск до первой асинхронной операции.
    if manager.dialog_data.get("seat_scan_in_progress"):
        await callback.answer("Поиск мест уже выполняется.")
        return

    manager.dialog_data["seat_scan_in_progress"] = True

    try:
        position_data = await get_seat_position_data(manager)
        available_positions = {
            item[1] for item in position_data["seat_position_items"]
        }

        if item_id not in available_positions:
            await callback.answer(
                "Этот вариант расположения недоступен.",
                show_alert=True,
            )
            return

        browser_service = manager.middleware_data.get(
            "booking_browser_service"
        )
        if browser_service is None:
            await callback.answer(
                "Браузерный сервис недоступен.",
                show_alert=True,
            )
            return

        chat_id = (
            callback.message.chat.id
            if callback.message
            else callback.from_user.id
        )
        selected_type = manager.dialog_data.get("selected_car_type") or {}

        # Удаляем фотографии предыдущего сканирования перед новым поиском.
        await _delete_wagon_photo_messages(
            callback.bot,
            chat_id,
            manager,
        )
        _cleanup_booking_screenshots(str(job_id))

        manager.dialog_data["selected_seat_position"] = item_id
        manager.dialog_data["picked_seats"] = []
        manager.dialog_data["booking_seat_scan"] = []
        manager.dialog_data["seat_scan_notice"] = (
            f"🔍 Ищу свободные места: "
            f"<b>{safe(selected_type.get('name', ''))}</b>, "
            f"расположение <b>{safe(item_id)}</b>.\n"
            "Поиск может занять до минуты. Окно обновится автоматически."
        )
        _clear_input_error(manager)

        await callback.answer("Ищу свободные места...")
        await manager.show()

        await BookingJobsService().update_job_state(
            uuid.UUID(str(job_id)),
            current_step="scanning_free_seats",
            chat_id=chat_id,
        )

        car_type_code = selected_type.get("car_type")
        if car_type_code:
            confirmed = await browser_service.select_car_type(
                str(job_id),
                str(car_type_code),
            )
            manager.dialog_data["selected_car_type"] = confirmed

        carriages = await browser_service.scan_free_seats(
            str(job_id),
            requested_position=item_id,
        )
        manager.dialog_data["booking_seat_scan"] = carriages

        if not carriages:
            await BookingJobsService().update_job_state(
                uuid.UUID(str(job_id)),
                current_step="no_matching_seats",
                chat_id=chat_id,
            )
            manager.dialog_data["seat_scan_notice"] = (
                "Свободных мест с выбранным расположением не найдено. "
                "Выберите другое расположение или повторите поиск позже."
            )
            await manager.show()
            return

        await BookingJobsService().update_job_state(
            uuid.UUID(str(job_id)),
            current_step="waiting_for_seat_selection",
            chat_id=chat_id,
        )
        manager.dialog_data["seat_scan_notice"] = (
            f"✅ <b>Поиск завершён.</b>\n"
            f"Тип вагона: "
            f"{safe(position_data['selected_car_type_name'])}\n"
            f"Расположение: <b>{safe(item_id)}</b>\n"
            f"Вагонов со свободными местами: <b>{len(carriages)}</b>\n\n"
            "Выберите место на схеме вагона."
        )
        await manager.switch_to(BookingSG.booking_choose_seat)
        await _send_wagon_screenshots(
            callback,
            manager,
            chat_id,
            carriages,
        )

    except Exception as exc:
        logger.exception(
            "Ошибка поиска свободных мест для задачи бронирования"
        )
        manager.dialog_data["seat_scan_notice"] = ""
        await _show_input_error(
            manager,
            f"Не удалось выполнить поиск свободных мест "
            f"({type(exc).__name__}). "
            "Проверьте страницу Chromium и повторите попытку.",
        )
    finally:
        manager.dialog_data["seat_scan_in_progress"] = False


async def get_seat_scan_data(dialog_manager: DialogManager, **kwargs):
    """Окно выбора содержит только инструкцию; места выбираются под фото вагонов."""
    return {
        "seat_scan_text": dialog_manager.dialog_data.get("seat_scan_notice", ""),
        "input_error": dialog_manager.dialog_data.get("input_error", ""),
    }


async def _handle_booking_seat_scan_selected(
    callback: CallbackQuery,
    manager: DialogManager,
    item_id: str,
):
    job_id = manager.dialog_data.get("booking_job_id")
    carriages = manager.dialog_data.get("booking_seat_scan") or []
    position = manager.dialog_data.get("selected_seat_position", "Любое")
    if not job_id:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return

    try:
        carriage_index_text, seat_index_text = str(item_id).split(":", 1)
        carriage_index = int(carriage_index_text)
        seat_index = int(seat_index_text)
        if not (0 <= carriage_index < len(carriages)):
            raise ValueError("Некорректный номер вагона")
        carriage = carriages[carriage_index]
        seats = carriage.get("seats", [])
        if not (0 <= seat_index < len(seats)):
            raise ValueError("Некорректный номер места")
        seat_number = str(seats[seat_index]["number"])
        panel_id = str(carriage["panel_id"])
    except (ValueError, IndexError, KeyError, TypeError):
        await callback.answer("Данные места устарели. Выполните поиск повторно.", show_alert=True)
        return

    browser_service = manager.middleware_data.get("booking_browser_service")
    if browser_service is None:
        await callback.answer("Браузерный сервис недоступен.", show_alert=True)
        return

    await callback.answer("Проверяю выбранное место...")
    try:
        selected = await browser_service.click_selected_seat(
            str(job_id),
            panel_id,
            seat_number,
            requested_position=position,
        )

        chat_id = callback.message.chat.id if callback.message else callback.from_user.id
        jobs = BookingJobsService()
        job_uuid = uuid.UUID(str(job_id))
        job = await jobs.get_job(job_uuid, chat_id=chat_id)
        if job is None:
            raise LookupError("Задача бронирования не найдена.")

        preferences = dict(job.preferences or {})
        orders_completed = int(preferences.get("orders_completed", 0))
        orders_target = int(preferences.get("orders_target", 1))
        current_order_number = orders_completed + 1
        manager.dialog_data["passenger_index"] = current_order_number
        manager.dialog_data["passenger_count"] = orders_target
        manager.dialog_data["bedding_enabled"] = False
        manager.dialog_data["selected_carriage"] = selected
        manager.dialog_data["selected_seat_number"] = seat_number
        manager.dialog_data.pop("current_passenger_summary", None)

        await jobs.update_job_state(
            job_uuid,
            status="running",
            current_step="seat_selected",
            chat_id=chat_id,
        )

        page = await browser_service.get_page(str(job_id))
        if page is None:
            raise RuntimeError("Страница бронирования не найдена.")
        await browser_service.open_passenger_data_form(page)
        options = await browser_service.get_document_options(page, passenger_index=1)
        if not options:
            raise RuntimeError("Сайт не предоставил типы документов пассажира.")

        manager.dialog_data["passenger_document_options"] = options
        try:
            await MonitorProfileService().save_document_options(chat_id, options)
        except Exception:
            logger.warning(
                "Не удалось сохранить список типов документов из pass.rw.by",
                exc_info=True,
            )

        # Место выбрано и форма пассажира открыта: удаляем все фото вагонов.
        await _delete_wagon_photo_messages(
            callback.bot,
            chat_id,
            manager,
        )
        _cleanup_booking_screenshots(str(job_id))

        manager.dialog_data["booking_notice"] = (
            f"✅ Место выбрано: вагон <b>{safe(selected.get('carriage_number', '?'))}</b>, "
            f"место <b>{safe(seat_number)}</b>.\n"
            f"Заказ {current_order_number} из {orders_target}. "
            f"Пассажир {current_order_number}/{orders_target}.\n\n"
        )
        _clear_input_error(manager)
        # Callback приходит с отдельного фото вагона. Сохраняем id старого
        # окна выбора, создаём новое окно ввода фамилии и удаляем старое,
        # чтобы в чате не оставалось сообщения «Выберите место на схеме вагона».
        old_dialog_message_id = None
        try:
            old_dialog_message_id = manager.current_stack().last_message_id
        except Exception:
            logger.debug("Не удалось получить id старого окна выбора места", exc_info=True)

        await manager.switch_to(BookingSG.booking_passenger_last_name)
        manager.show_mode = ShowMode.SEND
        await manager.show()

        if old_dialog_message_id:
            try:
                await callback.bot.delete_message(
                    chat_id=chat_id,
                    message_id=int(old_dialog_message_id),
                )
                logger.info(
                    "Удалено старое сообщение выбора места после выбора вагона %s места %s",
                    selected.get("carriage_number", "?"),
                    seat_number,
                )
            except Exception:
                logger.warning(
                    "Не удалось удалить старое сообщение выбора места %s",
                    old_dialog_message_id,
                    exc_info=True,
                )
        logger.info(
            "Место %s в вагоне %s выбрано; открыт ввод фамилии пассажира",
            seat_number,
            selected.get("carriage_number", "?"),
        )

    except Exception as exc:
        logger.exception("Не удалось выбрать место на сайте для задачи бронирования")
        await _show_input_error(
            manager,
            f"Не удалось выбрать место ({type(exc).__name__}). Возможно, его уже заняли. "
            "Выберите другое место или повторите поиск.",
        )
        await manager.switch_to(BookingSG.booking_choose_seat)
        await manager.show()


async def on_booking_seat_scan_item(
    callback: CallbackQuery,
    widget: Any,
    manager: DialogManager,
    item_id: str,
):
    await _handle_booking_seat_scan_selected(callback, manager, item_id)


@router.callback_query(F.data == "booking:seat_scan_back")
async def on_booking_seat_scan_back(
    callback: CallbackQuery,
    dialog_manager: DialogManager,
):
    """Возвращает к выбору расположения места из-под любой схемы вагона."""
    await on_back_from_seat_scan(callback, None, dialog_manager)


@router.callback_query(F.data == "booking:seat_scan_cancel")
async def on_booking_seat_scan_cancel(
    callback: CallbackQuery,
    dialog_manager: DialogManager,
):
    """Отменяет оформление из-под любой схемы вагона."""
    await on_cancel_dialog(callback, None, dialog_manager)


@router.callback_query(F.data.startswith("booking_seat_scan:"))
async def on_booking_seat_scan_selected(
    callback: CallbackQuery,
    dialog_manager: DialogManager,
):
    """Совместимость со старыми кнопками выбора мест из уже отправленных фото."""
    if not callback.data:
        await callback.answer("Некорректный запрос.", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Некорректный запрос.", show_alert=True)
        return
    await _handle_booking_seat_scan_selected(
        callback, dialog_manager, f"{parts[1]}:{parts[2]}"
    )


async def on_home_dialog(
    callback: CallbackQuery,
    button: Button,
    manager: DialogManager,
):
    """Return to the main menu from any dialog step without risking a duplicate order."""
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    data = manager.dialog_data

    task = data.get("search_task")
    if task and not task.done():
        task.cancel()

    keep_message_id = None
    try:
        keep_message_id = manager.current_stack().last_message_id
    except Exception:
        pass
    await clear_chat_messages(
        callback.bot,
        chat_id,
        keep_message_id=int(keep_message_id) if keep_message_id else None,
    )
    await _delete_wagon_photo_messages(callback.bot, chat_id, manager)

    job_id = data.get("booking_job_id")
    if job_id:
        jobs = BookingJobsService()
        try:
            job = await jobs.get_job(uuid.UUID(str(job_id)), chat_id=chat_id)
        except Exception:
            job = None
            logger.exception("Не удалось проверить задачу при переходе домой")

        # Never cancel/close a session after submission or when the result is unknown.
        if job and job.status not in {"submitting", "order_unknown", "order_created"}:
            try:
                await jobs.cancel_job(uuid.UUID(str(job_id)), chat_id=chat_id)
            except Exception:
                logger.warning("Не удалось отменить задачу при переходе домой", exc_info=True)
            browser = manager.middleware_data.get("booking_browser_service")
            if browser:
                try:
                    await browser.close_session(str(job_id))
                except Exception:
                    logger.warning("Не удалось закрыть браузер при переходе домой", exc_info=True)

    # Drop only dialog-local state; persisted monitor subscriptions and jobs remain.
    data.clear()
    manager.show_mode = ShowMode.EDIT
    await manager.switch_to(BookingSG.main_menu)
    await callback.answer("Главное меню")


@router.callback_query(F.data == "booking:home")
async def on_booking_home_from_photo(
    callback: CallbackQuery,
    dialog_manager: DialogManager,
):
    await on_home_dialog(callback, None, dialog_manager)


async def on_check_booking_authorization(
    callback: CallbackQuery,
    button: Button,
    manager: DialogManager,
):
    job_id = manager.dialog_data.get("booking_job_id")

    if not job_id:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return

    browser_service = manager.middleware_data.get(
        "booking_browser_service"
    )

    if browser_service is None:
        await callback.answer(
            "Браузерный сервис недоступен.",
            show_alert=True,
        )
        return

    chat_id = (
        callback.message.chat.id
        if callback.message
        else callback.from_user.id
    )

    try:
        authorized = await browser_service.is_authorized(str(job_id))

        if not authorized:
            await callback.answer(
                "Вход ещё не подтверждён. Если появилась CAPTCHA, "
                "решите её в браузере.",
                show_alert=True,
            )
            return

        jobs = BookingJobsService()
        job = await jobs.get_job(
            uuid.UUID(str(job_id)),
            chat_id=chat_id,
        )

        if job is None:
            await callback.answer(
                "Заявка не найдена.",
                show_alert=True,
            )
            return

        train_data = job.train_data or {}

        if not train_data:
            train_data = manager.dialog_data.get("selected_train") or {}

        await browser_service.open_selected_train_places(
            str(job_id),
            train_data,
        )

        available_types = await browser_service.get_available_car_types(
            str(job_id)
        )

        manager.dialog_data["available_car_types"] = available_types

        manager.dialog_data["picked_seats"] = []
        manager.dialog_data["passenger_count"] = 1
        manager.dialog_data["passenger_index"] = 1

        await jobs.update_job_state(
            uuid.UUID(str(job_id)),
            status="running",
            current_step="car_types_loaded",
            chat_id=chat_id,
        )

        manager.dialog_data["car_type_id_map"] = {
            f"ct{i}": str(item["car_type"])
            for i, item in enumerate(available_types)
        }

        try:
            await manager.switch_to(
                BookingSG.booking_choose_car_type,
            )
        except Exception:
            logger.exception("Не удалось показать окно выбора типа вагона")

        await callback.answer("Авторизация подтверждена!")

    except Exception:
        logger.warning(
            "Ошибка перехода к выбору мест выбранного поезда",
            exc_info=False,
        )
        await callback.answer(
            "Не удалось открыть места выбранного поезда. "
            "Проверьте страницу Chromium.",
            show_alert=True,
        )


# =========================================================
# ВВОД ДАННЫХ ПАССАЖИРОВ
# =========================================================

def _passenger_index(manager: DialogManager) -> int:
    return int(manager.dialog_data.get("passenger_index", 1))


async def _save_passenger_value(
    manager: DialogManager,
    chat_id: int,
    key: str,
    value: str,
) -> None:
    job_id = manager.dialog_data.get("booking_job_id")
    if not job_id:
        raise LookupError("Задача бронирования не найдена.")

    jobs = BookingJobsService()
    job_uuid = uuid.UUID(str(job_id))

    payload = await jobs.get_sensitive_payload(
        job_uuid,
        chat_id=chat_id,
    ) or {}

    passengers = payload.setdefault("passengers", [])
    index = _passenger_index(manager)

    while len(passengers) < index:
        passengers.append({})

    passengers[index - 1][key] = value

    await jobs.save_sensitive_payload(
        job_uuid,
        payload,
        chat_id=chat_id,
    )

    # Сохраняем для будущего мониторинга только полностью заполненных
    # пассажиров. Частично введённые паспортные данные в профиль не копируем.
    required = ("last_name", "first_name", "document_type", "document_number")
    complete_passengers = [
        passenger
        for passenger in passengers
        if isinstance(passenger, dict)
        and all(str(passenger.get(field) or "").strip() for field in required)
    ]
    if complete_passengers and len(complete_passengers) == len(passengers):
        try:
            await MonitorProfileService().save_passengers(
                chat_id,
                complete_passengers,
            )
        except Exception:
            logger.warning(
                "Не удалось обновить зашифрованные данные пассажиров профиля",
                exc_info=True,
            )


async def _get_passenger_page(manager: DialogManager):
    browser_service = manager.middleware_data.get(
        "booking_browser_service"
    )
    if browser_service is None:
        raise RuntimeError("Браузерный сервис недоступен.")

    job_id = manager.dialog_data.get("booking_job_id")
    if not job_id:
        raise LookupError("Задача бронирования не найдена.")

    page = await browser_service.get_page(str(job_id))
    if page is None:
        raise RuntimeError("Страница бронирования не открыта.")

    return browser_service, page


async def get_passenger_document_data(
    dialog_manager: DialogManager,
    **kwargs,
):
    options = dialog_manager.dialog_data.get(
        "passenger_document_options", []
    )

    return {
        "passenger_number": _passenger_index(dialog_manager),
        "passenger_count": dialog_manager.dialog_data.get(
            "passenger_count", 1
        ),
        "document_type_items": [
            (item["text"], item["value"])
            for item in options
        ],
        "input_error": dialog_manager.dialog_data.get("input_error", ""),
        "booking_notice": dialog_manager.dialog_data.get("booking_notice", ""),
    }


async def get_passenger_confirmation_data(
    dialog_manager: DialogManager,
    **kwargs,
):
    data = dialog_manager.dialog_data
    summary = data.get("current_passenger_summary") or {}

    fio_parts = [
        summary.get("last_name", ""),
        summary.get("first_name", ""),
        summary.get("middle_name", ""),
    ]
    fio = " ".join(p.strip() for p in fio_parts if p and p.strip()) or "—"

    train = data.get("booking_train_data") or {}
    selected_type = data.get("selected_car_type") or {}
    travel_date = str(data.get("booking_travel_date") or "")
    bedding_text = str(
        data.get("bedding_notice")
        or (
            "Постельное бельё требуется."
            if data.get("bedding_enabled")
            else "Постельное бельё не требуется."
        )
    )

    return {
        "passenger_count": data.get("passenger_count", 1),
        "train_number": safe(train.get("number") or "—"),
        "route_from": safe(data.get("booking_from_name") or "—"),
        "route_to": safe(data.get("booking_to_name") or "—"),
        "travel_date": safe(format_date(travel_date) if travel_date else "—"),
        "departure_time": safe(train.get("time") or "—"),
        "arrival_time": safe(train.get("arr_time") or "—"),
        "car_type_name": safe(selected_type.get("name") or "—"),
        "bedding_text": safe(bedding_text),
        "passenger_fio": safe(fio),
        "passenger_document_type": safe(
            summary.get("document_type") or "—"
        ),
        "passenger_document_number": safe(
            summary.get("document_number") or "—"
        ),
        "carriage_number": safe(
            summary.get("carriage_number") or "—"
        ),
        "seat_number": safe(summary.get("seat_number") or "—"),
        "order_number": int(
            summary.get("order_number")
            or data.get("passenger_index", 1)
        ),
        "orders_target": int(
            summary.get("orders_target")
            or data.get("passenger_index", 1)
        ),
        "bedding_notice": data.get("bedding_notice", ""),
        "input_error": data.get("input_error", ""),
    }


async def _apply_bedding_choice(
    callback: CallbackQuery,
    manager: DialogManager,
    enabled: bool,
):
    if not callback.message:
        await callback.answer(
            "Не удалось получить сообщение.",
            show_alert=True,
        )
        return

    await callback.answer("Проверяю постельное бельё...")

    try:
        browser_service = manager.middleware_data.get(
            "booking_browser_service"
        )
        if browser_service is None:
            raise RuntimeError("Браузерный сервис недоступен.")

        job_id = manager.dialog_data.get("booking_job_id")
        if not job_id:
            raise LookupError("Задача бронирования не найдена.")

        page = await browser_service.get_page(str(job_id))
        if page is None:
            raise RuntimeError("Страница бронирования не открыта.")

        bedding_result = await browser_service.set_bed_linen(
            page,
            bool(enabled),
        )

        if not bedding_result.get("available"):
            manager.dialog_data["bedding_enabled"] = False
            bedding_text = (
                "На сайте нет переключателя постельного белья "
                "для этого поезда — шаг пропущен."
            )
        else:
            if bool(bedding_result.get("checked")) != bool(enabled):
                raise RuntimeError(
                    "Сайт не подтвердил выбранное состояние "
                    "постельного белья."
                )
            manager.dialog_data["bedding_enabled"] = bool(enabled)
            bedding_text = (
                "Постельное бельё выбрано."
                if enabled
                else "Постельное бельё не требуется."
            )

        await BookingJobsService().update_job_state(
            uuid.UUID(str(job_id)),
            current_step="passenger_data_ready",
            chat_id=callback.message.chat.id,
        )

        manager.dialog_data["bedding_notice"] = bedding_text
        if bedding_result.get("available"):
            try:
                job_id = manager.dialog_data.get("booking_job_id")
                chat_id = callback.message.chat.id
                jobs = BookingJobsService()
                payload = await jobs.get_sensitive_payload(
                    uuid.UUID(str(job_id)),
                    chat_id=chat_id,
                ) or {}
                passengers = payload.setdefault("passengers", [])
                passenger_index = _passenger_index(manager)
                while len(passengers) < passenger_index:
                    passengers.append({})
                payload["bedding"] = bool(enabled)
                for passenger in passengers:
                    if isinstance(passenger, dict):
                        passenger["bedding"] = bool(enabled)
                await jobs.save_sensitive_payload(
                    uuid.UUID(str(job_id)), payload, chat_id=chat_id
                )
                complete_passengers = [
                    passenger for passenger in passengers
                    if isinstance(passenger, dict)
                    and all(str(passenger.get(key) or "").strip() for key in (
                        "last_name", "first_name", "document_type", "document_number"
                    ))
                ]
                if complete_passengers and len(complete_passengers) == len(passengers):
                    await MonitorProfileService().save_passengers(
                        chat_id, complete_passengers
                    )
                    await MonitorProfileService().save_bedding_preference(
                        chat_id, bool(enabled)
                    )
            except Exception:
                logger.warning(
                    "Не удалось сохранить предпочтение постельного белья в профиле",
                    exc_info=True,
                )
        _clear_input_error(manager)
        await manager.switch_to(BookingSG.booking_passenger_confirmation)

    except Exception:
        logger.exception(
            "Ошибка применения выбора постельного белья"
        )
        await _show_input_error(
            manager,
            "Не удалось обработать выбор постельного белья. Проверьте страницу Chromium и попробуйте ещё раз.",
        )


async def on_bedding_yes(
    callback: CallbackQuery,
    button: Button,
    manager: DialogManager,
):
    await _apply_bedding_choice(callback, manager, True)


async def on_bedding_no(
    callback: CallbackQuery,
    button: Button,
    manager: DialogManager,
):
    await _apply_bedding_choice(callback, manager, False)


async def on_passenger_last_name(
    message: Message,
    widget: Any,
    manager: DialogManager,
):
    _force_edit_mode(manager)
    value = (message.text or "").strip()
    if not value or len(value) > 100:
        await _show_input_error(manager, "Введите корректную фамилию (как в документе).")
        return
    try:
        await _save_passenger_value(manager, message.chat.id, "last_name", value)
        _clear_input_error(manager)
        manager.dialog_data.pop("booking_notice", None)
        await manager.switch_to(BookingSG.booking_passenger_first_name)
    except Exception:
        logger.exception("Ошибка сохранения фамилии пассажира")
        await _show_input_error(manager, "Не удалось сохранить фамилию. Попробуйте ещё раз.")


async def on_passenger_first_name(
    message: Message,
    widget: Any,
    manager: DialogManager,
):
    _force_edit_mode(manager)
    value = (message.text or "").strip()
    if not value or len(value) > 100:
        await _show_input_error(manager, "Введите корректное имя.")
        return
    try:
        await _save_passenger_value(manager, message.chat.id, "first_name", value)
        _clear_input_error(manager)
        await manager.switch_to(BookingSG.booking_passenger_middle_name)
    except Exception:
        logger.exception("Ошибка сохранения имени пассажира")
        await _show_input_error(manager, "Не удалось сохранить имя. Попробуйте ещё раз.")


async def on_passenger_middle_name(
    message: Message,
    widget: Any,
    manager: DialogManager,
):
    _force_edit_mode(manager)
    value = (message.text or "").strip()
    if value == "-":
        value = ""
    if len(value) > 100:
        await _show_input_error(manager, "Слишком длинное отчество. Попробуйте ещё раз.")
        return
    try:
        await _save_passenger_value(manager, message.chat.id, "middle_name", value)
        _clear_input_error(manager)
        await manager.switch_to(BookingSG.booking_passenger_document_type)
    except Exception:
        logger.exception("Ошибка сохранения отчества пассажира")
        await _show_input_error(manager, "Не удалось сохранить отчество. Попробуйте ещё раз.")


async def on_passenger_document_type_selected(
    callback: CallbackQuery,
    widget: Any,
    manager: DialogManager,
    item_id: str,
):
    options = manager.dialog_data.get(
        "passenger_document_options", []
    )

    selected = next(
        (
            item for item in options
            if str(item.get("value")) == str(item_id)
        ),
        None,
    )

    if selected is None:
        await callback.answer(
            "Этот тип документа больше недоступен.",
            show_alert=True,
        )
        return

    chat_id = (
        callback.message.chat.id
        if callback.message
        else callback.from_user.id
    )

    try:
        await _save_passenger_value(
            manager,
            chat_id,
            "document_type",
            selected["text"],
        )
        await callback.answer("Тип документа выбран.")
        await manager.switch_to(
            BookingSG.booking_passenger_document_number
        )
    except Exception:
        logger.exception("Ошибка сохранения типа документа")
        await callback.answer(
            "Не удалось сохранить выбор.",
            show_alert=True,
        )


async def on_passenger_document_number(
    message: Message,
    widget: Any,
    manager: DialogManager,
):
    _force_edit_mode(manager)
    value = (message.text or "").strip()

    if not value or len(value) > 100:
        await _show_input_error(manager, "Введите корректный номер документа.")
        return

    try:
        await _save_passenger_value(
            manager, message.chat.id, "document_number", value
        )

        job_id = manager.dialog_data.get("booking_job_id")
        jobs = BookingJobsService()

        payload = await jobs.get_sensitive_payload(
            uuid.UUID(str(job_id)),
            chat_id=message.chat.id,
        ) or {}

        global_index = _passenger_index(manager)
        passengers = payload.get("passengers", [])

        if len(passengers) < global_index:
            raise ValueError("Данные пассажира не найдены.")

        passenger = passengers[global_index - 1]

        browser_service, page = await _get_passenger_page(manager)

        result = await browser_service.prepare_passenger_form(
            page,
            passenger,
            passenger["document_type"],
            bedding=False,
            passenger_index=1,
        )

        if not result.get("valid"):
            errors = result.get("errors") or [
                "Проверьте заполнение формы на сайте."
            ]
            await _show_input_error(
                manager,
                "Сайт не принял заполнение формы: " + "; ".join(str(error) for error in errors),
            )
            return

        job_prefs = {}
        try:
            current_job = await jobs.get_job(
                uuid.UUID(str(job_id)),
                chat_id=message.chat.id,
            )
            if current_job and current_job.preferences:
                job_prefs = current_job.preferences
        except Exception:
            pass

        manager.dialog_data["current_passenger_summary"] = {
            "last_name": passenger.get("last_name", ""),
            "first_name": passenger.get("first_name", ""),
            "middle_name": passenger.get("middle_name", ""),
            "document_type": passenger.get("document_type", ""),
            "document_number": passenger.get("document_number", ""),
            "carriage_number": (
                manager.dialog_data.get("selected_carriage") or {}
            ).get("carriage_number", "?"),
            "seat_number": manager.dialog_data.get(
                "selected_seat_number", "?"
            ),
            "order_number": int(
                job_prefs.get("orders_completed", 0)
            ) + 1,
            "orders_target": int(
                job_prefs.get("orders_target", 1)
            ),
        }

        bedding_probe = await browser_service.set_bed_linen(page, False)
        if not bedding_probe.get("available"):
            manager.dialog_data["bedding_enabled"] = False
            manager.dialog_data["bedding_notice"] = (
                "На сайте нет переключателя постельного белья для этого поезда."
            )
            await BookingJobsService().update_job_state(
                uuid.UUID(str(job_id)),
                current_step="passenger_data_ready",
                chat_id=message.chat.id,
            )

            _clear_input_error(manager)
            await manager.switch_to(
                BookingSG.booking_passenger_confirmation,
            )
            return

        _clear_input_error(manager)
        await manager.switch_to(BookingSG.booking_bedding)

    except Exception:
        logger.exception(
            "Ошибка заполнения формы пассажира № %s",
            _passenger_index(manager),
        )
        await _show_input_error(
            manager,
            "Не удалось завершить заполнение формы. Проверьте открытую страницу Chromium. Данные повторно не отправлялись.",
        )


async def on_passenger_data_confirmation(
    callback: CallbackQuery,
    button: Button,
    manager: DialogManager,
):
    """Отправляет текущий заказ. Для следующего пассажира
    создаётся новая job, а существующий браузер переключается
    на неё (cookies и авторизация сохраняются).
    """
    job_id = manager.dialog_data.get("booking_job_id")

    if not job_id:
        await callback.answer(
            "Задача бронирования не найдена.",
            show_alert=True,
        )
        return

    chat_id = (
        callback.message.chat.id
        if callback.message
        else callback.from_user.id
    )

    job_uuid = uuid.UUID(str(job_id))
    jobs = BookingJobsService()
    submission_started = False
    order_confirmed = False
    current_order_number = 0
    orders_target = 1
    next_order_needed = False

    try:
        job = await jobs.get_job(job_uuid, chat_id=chat_id)

        if job is None:
            await callback.answer(
                "Задача бронирования не найдена.",
                show_alert=True,
            )
            return

        if job.status == "order_unknown":
            await callback.answer(
                "Этот заказ уже был отправлен. "
                "Дождитесь перехода к следующему шагу.",
                show_alert=True,
            )
            return

        if job.status not in {"waiting", "running"}:
            await callback.answer(
                f"Отправка невозможна: задача в состоянии "
                f"«{job.status}». Повторное нажатие запрещено.",
                show_alert=True,
            )
            return

        preferences = dict(job.preferences or {})
        orders_completed = int(
            preferences.get("orders_completed", 0)
        )
        orders_target = int(
            preferences.get("orders_target", 1)
        )
        current_order_number = orders_completed + 1

        if current_order_number > orders_target:
            await callback.answer(
                "Все заказы уже оформлены.", show_alert=True
            )
            return

        browser_service, page = await _get_passenger_page(manager)

        payload = await jobs.get_sensitive_payload(
            job_uuid,
            chat_id=chat_id,
        ) or {}

        passengers = payload.get("passengers", [])
        if len(passengers) < current_order_number:
            await callback.answer(
                "Не сохранены данные текущего пассажира.",
                show_alert=True,
            )
            return

        passenger = passengers[current_order_number - 1]
        required = (
            "last_name",
            "first_name",
            "document_type",
            "document_number",
        )
        if any(
            not str(passenger.get(key, "")).strip()
            for key in required
        ):
            await callback.answer(
                f"В данных пассажира №{current_order_number} "
                "есть пропуски.",
                show_alert=True,
            )
            return

        await browser_service.prepare_order_submission(page, 1)

        await jobs.mark_submitting(job_uuid)
        submission_started = True

        result = await browser_service.click_order_submit_once(
            page,
            expected_train_number=job.train_number,
            expected_travel_date=job.travel_date,
        )

        # Переходим дальше только при однозначном подтверждении сайта.
        result_status = str(result.get("status") or "unknown")
        if result_status == "rejected":
            await jobs.mark_order_rejected(
                job_uuid,
                error=(
                    f"Сайт явно отклонил заказ {current_order_number}/{orders_target}. "
                    f"Причина: {result.get('reason', 'не указана')}; "
                    f"URL: {result.get('url', '')[:500]}"
                ),
            )
            _cleanup_booking_screenshots(str(job_uuid))
            await _show_input_error(
                manager,
                "Сайт сообщил, что заказ не создан. Проверьте данные и доступность мест; "
                "автоматически повторять отправку не буду.",
            )
            return

        if result_status != "confirmed":
            await jobs.mark_order_unknown(
                job_uuid,
                error=(
                    f"Заказ {current_order_number}/{orders_target}: "
                    "сайт не подтвердил и не отклонил создание заказа. "
                    f"Статус: {result_status}; причина: {result.get('reason', 'неизвестна')}; "
                    f"URL: {result.get('url', '')[:500]}"
                ),
            )
            _cleanup_booking_screenshots(str(job_uuid))
            await _show_input_error(
                manager,
                "⚠️ Сайт не показал однозначное подтверждение оформления за отведённое время. "
                "Заказ мог быть создан, поэтому повторно отправлять его нельзя. "
                "Проверьте раздел заказов и страницу оплаты pass.rw.by; "
                "следующий заказ пока не запускается.",
            )
            return

        await jobs.mark_order_created(
            job_uuid,
            order_number=result.get("order_number"),
            order_expires_at=result.get("order_expires_at"),
        )
        order_confirmed = True
        _clear_input_error(manager)
        _cleanup_booking_screenshots(str(job_uuid))

        callback_text = (
            "Заказ успешно оформлен."
            if orders_target == 1
            else f"Заказ {current_order_number}/{orders_target} подтверждён сайтом."
        )
        await callback.answer(
            callback_text,
            show_alert=False,
        )

        orders_completed += 1
        preferences["orders_completed"] = orders_completed
        next_order_needed = orders_completed < orders_target

        if callback.message:
            order_number = result.get("order_number")
            expiry = result.get("order_expires_at")
            lines = [
                "🎉 <b>Поздравляем! Заказ успешно оформлен, место забронировано.</b>",
            ]
            # Счётчик нужен только при оформлении нескольких отдельных заказов.
            if orders_target > 1:
                lines.append(
                    f"Заказ: <b>{orders_completed}/{orders_target}</b>."
                )
            lines.append(
                (
                    f"Номер заказа: <b>{safe(order_number)}</b>."
                    if order_number
                    else "Номер заказа сайт не отобразил. Проверьте его в личном кабинете pass.rw.by."
                )
            )
            if expiry:
                local_expiry = expiry.astimezone()
                lines.append(
                    f"⏰ Оплатите билет до <b>{local_expiry:%d.%m.%Y %H:%M}</b>."
                )
            else:
                lines.append(
                    "⏰ <b>У вас есть 20 минут, чтобы оплатить билет.</b> "
                    "После истечения срока бронирование может быть отменено."
                )
            lines.extend([
                "",
                "💳 <b>Оплатить:</b> https://pass.rw.by/ru/order/payment/",
                "",
            ])

            if next_order_needed:
                lines.append(
                    f"➡️ Переходим к заказу "
                    f"<b>{orders_completed + 1}/{orders_target}</b>. "
                    "Браузер вернётся на страницу поездов."
                )
            elif orders_target > 1:
                if orders_target % 10 == 1 and orders_target % 100 != 11:
                    order_word = "заказ"
                elif 2 <= orders_target % 10 <= 4 and not 12 <= orders_target % 100 <= 14:
                    order_word = "заказа"
                else:
                    order_word = "заказов"
                lines.append(
                    f"🎉 <b>Все {orders_target} {order_word} оформлены.</b>\n"
                    "Проверьте корзину и оплатите каждый заказ."
                )

            await callback.message.answer(
                "\\n".join(lines),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="🏠 Домой",
                                callback_data="booking:home",
                            )
                        ]
                    ]
                ),
            )

        if next_order_needed:
            try:
                passengers_payload = payload
                train_data = job.train_data or {}
                new_prefs = dict(preferences)
                new_prefs["orders_completed"] = orders_completed
                new_prefs["orders_target"] = orders_target

                new_job = await jobs.create_job(
                    chat_id=chat_id,
                    from_name=job.from_name,
                    to_name=job.to_name,
                    travel_date=job.travel_date,
                    train_number=job.train_number,
                    url=job.url,
                    train_data=train_data,
                    preferences=new_prefs,
                    status="running",
                    current_step="preparing_next_order",
                )

                if passengers_payload:
                    await jobs.save_sensitive_payload(
                        new_job.id,
                        passengers_payload,
                        chat_id=chat_id,
                    )

                old_job_id = str(job_id)
                new_job_id = str(new_job.id)

                manager.dialog_data["picked_seats"] = []
                manager.dialog_data["passenger_count"] = 1
                manager.dialog_data["passenger_index"] = (
                    orders_completed + 1
                )
                manager.dialog_data.pop("booking_seat_scan", None)
                manager.dialog_data.pop("selected_car_type", None)
                manager.dialog_data.pop(
                    "selected_seat_position", None
                )
                manager.dialog_data.pop("selected_carriage", None)
                manager.dialog_data.pop(
                    "selected_seat_number", None
                )
                manager.dialog_data.pop(
                    "passenger_document_options", None
                )
                manager.dialog_data.pop(
                    "current_passenger_summary", None
                )

                await browser_service.switch_job(
                    new_job_id,
                    old_job_id,
                    new_job.url,
                )

                manager.dialog_data["booking_job_id"] = new_job_id
                manager.dialog_data["booking_train_data"] = dict(
                    new_job.train_data or train_data
                )
                manager.dialog_data["booking_travel_date"] = str(
                    new_job.travel_date or ""
                )
                manager.dialog_data["booking_from_name"] = str(
                    new_job.from_name or ""
                )
                manager.dialog_data["booking_to_name"] = str(
                    new_job.to_name or ""
                )

                await browser_service.open_selected_train_places(
                    new_job_id,
                    train_data,
                )

                available_types = (
                    await browser_service.get_available_car_types(
                        new_job_id
                    )
                )
                manager.dialog_data["available_car_types"] = (
                    available_types
                )
                manager.dialog_data["car_type_id_map"] = {
                    f"ct{i}": str(item["car_type"])
                    for i, item in enumerate(available_types)
                }

                await jobs.update_job_state(
                    new_job.id,
                    current_step="car_types_loaded",
                    chat_id=chat_id,
                )

                await manager.switch_to(
                    BookingSG.booking_choose_car_type,
                )

            except Exception as exc:
                logger.exception(
                    "Не удалось перейти к следующему заказу %s/%s",
                    orders_completed + 1,
                    orders_target,
                )
                await _show_input_error(
                    manager,
                    "Предыдущий заказ подтверждён, но следующий заказ не удалось подготовить. "
                    f"Причина: {type(exc).__name__}: {safe(str(exc))[:300]}. "
                    "Проверьте Chromium; подтверждённый заказ не будет отправлен повторно.",
                )
        else:
            logger.info(
                "Все %s заказов оформлены. Старая job %s "
                "в состоянии order_created.",
                orders_target,
                job_id,
            )
            # Все заказы оформлены — закрываем браузер
            try:
                await browser_service.close_session(str(job_id))
                logger.info("Браузерная сессия закрыта после завершения всех заказов.")
            except Exception:
                logger.exception("Не удалось закрыть браузерную сессию.")

            # Заказ завершён: закрываем контекст диалога и удаляем
            # исходное сообщение «Проверьте данные заказа».
            # При нескольких заказах этот блок не выполняется:
            # сообщение остаётся якорем диалога для следующего заказа.
            try:
                await manager.done()
            except Exception:
                logger.exception(
                    "Не удалось закрыть диалог после последнего заказа."
                )

            if callback.message:
                try:
                    await callback.message.delete()
                    logger.info(
                        "Удалено сообщение подтверждения после оформления заказа."
                    )
                except Exception:
                    logger.exception(
                        "Не удалось удалить сообщение подтверждения заказа."
                    )

    except Exception:
        logger.exception(
            "Ошибка финального этапа бронирования"
        )

        if submission_started and not order_confirmed:
            try:
                current = await jobs.get_job(job_uuid, chat_id=chat_id)
                if current and current.status == "submitting":
                    await jobs.mark_order_unknown(
                        job_uuid,
                        error=(
                            f"Заказ {current_order_number}/{orders_target}: "
                            "ошибка во время отправки/ожидания результата. "
                            "Требуется сверка на сайте."
                        ),
                    )
            except Exception:
                logger.exception("Не удалось сохранить неопределённый результат")
            message = (
                "⚠️ Результат отправки неизвестен. Не нажимайте кнопку повторно. "
                "Проверьте личный кабинет pass.rw.by."
            )
        elif order_confirmed:
            message = (
                "Предыдущий заказ уже подтверждён сайтом. "
                "Ошибка возникла только при подготовке следующего заказа."
            )
        else:
            message = (
                "Не удалось подготовить форму к отправке. "
                "Заказ не отправлялся. Проверьте Chromium."
            )

        await callback.answer(message, show_alert=True)
