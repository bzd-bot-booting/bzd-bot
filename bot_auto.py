import asyncio
import logging
import re
from urllib.parse import quote
from datetime import datetime, timedelta
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    BotCommand, ErrorEvent
)
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest

from aiogram_dialog import (
    Dialog, Window, DialogManager, StartMode, setup_dialogs
)
from aiogram_dialog.widgets.text import Const, Format, Multi
from aiogram_dialog.widgets.kbd import Column, Select, SwitchTo, Url, Button, Row
from aiogram_dialog.widgets.input import MessageInput
from aiogram.fsm.state import State, StatesGroup
import os
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ==================== КОНФИГУРАЦИЯ ====================
API_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "your-telegram-toccen")
HEADLESS = True
CHECK_INTERVAL = 30
# =====================================================

bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

active_monitors: dict[str, dict] = {}

SMART_STATIONS = {
    "минск": "Минск-Пассажирский",
    "минск-пассажирский": "Минск-Пассажирский",
    "брест": "Брест-Центральный",
    "брест-центральный": "Брест-Центральный",
    "гомель": "Гомель",
    "витебск": "Витебск",
    "могилев": "Могилев",
    "гродно": "Гродно",
}

CITIES = ["Брест", "Витебск", "Гомель", "Гродно", "Минск", "Могилев"]


class BookingSG(StatesGroup):
    station_from = State()
    station_from_manual = State()
    station_to = State()
    station_to_manual = State()
    travel_date = State()
    travel_date_manual = State()
    searching = State()
    choose_train = State()
    monitoring_info = State()
    train_result = State()


def make_monitor_key(travel_date: str, train_num: str) -> str:
    return f"{travel_date}|{train_num}"


def format_date(date_str: str) -> str:
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%d.%m.%Y")
    except Exception:
        return date_str


# ==================== ЗАЩИТА ОТ ДВОЙНЫХ НАЖАТИЙ ====================
def is_processing(manager: DialogManager) -> bool:
    """Проверяет, идет ли обработка. Если нет - ставит флаг."""
    if manager.dialog_data.get("processing"):
        return True
    manager.dialog_data["processing"] = True
    return False

def stop_processing(manager: DialogManager):
    """Снимает флаг обработки."""
    manager.dialog_data["processing"] = False


# ==================== ОБРАБОТЧИКИ КНОПОК ====================
async def on_cancel_dialog(callback: CallbackQuery, button: Button, manager: DialogManager):
    search_task = manager.dialog_data.get("search_task")
    if search_task and not search_task.done():
        search_task.cancel()
        logging.info("Задача поиска поездов отменена.")

    try:
        await callback.message.delete()
    except Exception as e:
        logging.error(f"Не удалось удалить сообщение: {e}")

    await manager.done()


async def on_back_from_search(callback: CallbackQuery, button: Button, manager: DialogManager):
    search_task = manager.dialog_data.get("search_task")
    if search_task and not search_task.done():
        search_task.cancel()
        logging.info("Поиск поездов прерван пользователем (кнопка Назад).")
    
    await manager.switch_to(BookingSG.travel_date)


async def on_back_from_monitoring(callback: CallbackQuery, button: Button, manager: DialogManager):
    chat_id = callback.message.chat.id
    travel_date = manager.dialog_data.get("travel_date")
    target_train = manager.dialog_data.get("selected_train")
    
    if travel_date and target_train:
        monitor_key = make_monitor_key(travel_date, target_train["number"])
        if monitor_key in active_monitors:
            data = active_monitors[monitor_key]
            if chat_id in data["users"]:
                data["users"].remove(chat_id)
                logging.info(f"Пользователь {chat_id} отписался от мониторинга {target_train['number']}")
                
                if not data["users"]:
                    task = data.get("task")
                    if task and not task.done():
                        task.cancel()
                    del active_monitors[monitor_key]
    
    await manager.switch_to(BookingSG.choose_train)


async def on_back_to_trains(callback: CallbackQuery, button: Button, manager: DialogManager):
    """Принудительный возврат к списку поездов (для экрана результата)"""
    await manager.switch_to(BookingSG.choose_train)


# ---------- Парсер поезда ----------
async def parse_train_row(row, travel_date_str: str):
    train_num_el = await row.query_selector(".train-number")
    if not train_num_el:
        return None
    t_num = (await train_num_el.inner_text()).strip()

    row_text = await row.inner_text()
    times = re.findall(r'\b\d{2}:\d{2}\b', row_text)

    if len(times) >= 2:
        dep_time, arr_time = times[0], times[1]
    elif len(times) == 1:
        dep_time, arr_time = times[0], "--:--"
    else:
        dep_time = arr_time = "--:--"

    is_departed = False
    try:
        if dep_time != "--:--":
            travel_dt = datetime.strptime(f"{travel_date_str} {dep_time}", "%Y-%m-%d %H:%M")
            if datetime.now() > travel_dt:
                is_departed = True
    except Exception:
        pass

    seats_details = {}
    total_seats = 0

    if "нет мест" not in row_text.lower():
        lines = [line.strip() for line in row_text.split('\n') if line.strip()]
        current_type = None
        
        for line in lines:
            line_lower = line.lower()
            if "плацкарт" in line_lower: current_type = "Плацкарт"
            elif "купей" in line_lower or "купе" in line_lower: current_type = "Купе"
            elif "сидяч" in line_lower: current_type = "Сидячий"
            elif "общ" in line_lower: current_type = "Общий"
            elif "люкс" in line_lower: current_type = "Люкс"
            elif "мягк" in line_lower: current_type = "Мягкий"

            if current_type and re.fullmatch(r'\d+', line):
                seats_details[current_type] = seats_details.get(current_type, 0) + int(line)
        
        if not seats_details:
            matches = re.finditer(
                r'(Плацкартный|Купейный|Сидячий|Общий|Люкс|Мягкий)\s+(\d+)',
                row_text, re.IGNORECASE
            )
            for m in matches:
                t_type = m.group(1).capitalize()
                if "Плацкарт" in t_type: t_type = "Плацкарт"
                elif "Купей" in t_type: t_type = "Купе"
                elif "Сидяч" in t_type: t_type = "Сидячий"
                elif "Общ" in t_type: t_type = "Общий"
                count = int(m.group(2))
                seats_details[t_type] = seats_details.get(t_type, 0) + count

        total_seats = sum(seats_details.values())

        if total_seats == 0:
            buy_btn = await row.query_selector(
                "a:has-text('ВЫБРАТЬ МЕСТА'), button:has-text('ВЫБРАТЬ МЕСТА')"
            )
            if buy_btn:
                seats_details["Есть места"] = 1
                total_seats = 1

    return {
        "number": t_num,
        "time": dep_time,
        "arr_time": arr_time,
        "seats": total_seats,
        "details": seats_details,
        "departed": is_departed,
    }


# ---------- Поиск поездов ----------
async def search_trains(chat_id: int, data: dict) -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        page = await browser.new_page()
        try:
            from_enc = quote(data["from_name"])
            to_enc = quote(data["to_name"])
            travel_date = data["travel_date"]
            search_url = f"https://pass.rw.by/ru/route/?from={from_enc}&to={to_enc}&date={travel_date}"

            try:
                await page.goto(search_url, timeout=90000, wait_until="domcontentloaded")
                await page.wait_for_timeout(3000)
            except Exception as e:
                logging.error(f"Ошибка загрузки страницы БЖД: {e}")
                return []

            rows = await page.query_selector_all(".sch-table__row-wrap")
            if not rows:
                return []

            trains = []
            for idx, row in enumerate(rows, 1):
                t = await parse_train_row(row, travel_date)
                if not t:
                    continue
                t["index"] = idx
                trains.append(t)

            return trains
        except asyncio.CancelledError:
            logging.info("Поиск поездов отменен пользователем.")
            raise
        except Exception as e:
            logging.error(f"Ошибка поиска поездов: {e}")
            return []
        finally:
            try:
                await browser.close()
            except Exception:
                pass


# ---------- Мониторинг ----------
async def monitor_train(monitor_key: str):
    data = active_monitors.get(monitor_key)
    if not data:
        return

    target_train = data["target"]
    base_link = data["base_link"]
    travel_date_str = data["travel_date"]
    train_num = target_train["number"]

    browser = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=HEADLESS)
            page = await browser.new_page()

            while True:
                current = active_monitors.get(monitor_key)
                if not current or not current.get("users"):
                    logging.info(f"Мониторинг {train_num} остановлен — нет подписчиков")
                    return

                try:
                    await page.goto(base_link, timeout=90000, wait_until="domcontentloaded")
                    await page.wait_for_timeout(3000)

                    rows = await page.query_selector_all(".sch-table__row-wrap")
                    found_target = False

                    for row in rows:
                        t = await parse_train_row(row, travel_date_str)
                        if t and t["number"] == train_num:
                            found_target = True

                            if t["departed"]:
                                for chat_id in list(current["users"]):
                                    try:
                                        await bot.send_message(
                                            chat_id,
                                            f"🛑 Поезд <b>{train_num}</b> отправился. Мониторинг остановлен."
                                        )
                                    except (TelegramForbiddenError, TelegramBadRequest):
                                        pass
                                return

                            if t["seats"] > 0:
                                users = current["users"]
                                if not users:
                                    return

                                first_chat_id = users.pop(0)
                                details_str = ", ".join(
                                    f"{k} ({v})" for k, v in t["details"].items()
                                )
                                try:
                                    await bot.send_message(
                                        first_chat_id,
                                        f"🚨 <b>На поезд {train_num} появились места!</b>\n"
                                        f"🕒 Время: {t['time']} — {t['arr_time']}\n"
                                        f"🟢 В наличии: <b>{details_str}</b>.\n\n"
                                        f"Скорее переходите к выбору:",
                                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                            [InlineKeyboardButton(text="🎫 Выбрать место", url=base_link)]
                                        ])
                                    )
                                except (TelegramForbiddenError, TelegramBadRequest) as e:
                                    logging.warning(f"Не удалось отправить уведомление {first_chat_id}: {e}")

                                logging.info(f"Уведомлён пользователь {first_chat_id} о местах на {train_num}")

                                if not users:
                                    return

                                logging.info(f"Продолжаем мониторинг {train_num} для оставшихся {len(users)} чел.")
                                break
                            break

                    if not found_target:
                        for chat_id in list(current["users"]):
                            try:
                                await bot.send_message(
                                    chat_id,
                                    f"⚠️ Поезд {train_num} исчез из расписания. Мониторинг остановлен."
                                )
                            except (TelegramForbiddenError, TelegramBadRequest):
                                pass
                        return

                    logging.info(f"Мониторинг {train_num}: мест пока нет. Подписчиков: {len(current['users'])}")
                    await asyncio.sleep(CHECK_INTERVAL)

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logging.warning(f"Ошибка при проверке поезда {train_num}: {e}")
                    await asyncio.sleep(CHECK_INTERVAL)

    except asyncio.CancelledError:
        logging.info(f"Мониторинг поезда {train_num} отменён.")
    except Exception as e:
        logging.error(f"Критическая ошибка мониторинга поезда {train_num}: {e}")
        current = active_monitors.get(monitor_key)
        if current:
            for chat_id in list(current.get("users", [])):
                try:
                    await bot.send_message(
                        chat_id,
                        f"⚠️ <b>Мониторинг поезда {train_num} остановлен из-за технической ошибки.</b>\n"
                        f"Пожалуйста, попробуйте запустить поиск заново через /book."
                    )
                except Exception:
                    pass
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        if monitor_key in active_monitors:
            del active_monitors[monitor_key]


# ==================== УВЕДОМЛЕНИЕ О ВЫКЛЮЧЕНИИ ====================
async def notify_shutdown():
    if not active_monitors:
        return
    logging.info("Отправка уведомлений о выключении бота...")
    for key, data in list(active_monitors.items()):
        for chat_id in list(data.get("users", [])):
            try:
                await bot.send_message(
                    chat_id,
                    "⚠️ <b>Мониторинг остановлен.</b>\n\n"
                    "Бот был принудительно выключен или перезапущен.\n"
                    "Пожалуйста, запустите поиск заново через /book, когда бот снова заработает."
                )
            except Exception as e:
                logging.warning(f"Не удалось уведомить {chat_id} о выключении: {e}")


# ==================== ГЕТТЕРЫ И ОБРАБОТЧИКИ ====================

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
        station = SMART_STATIONS.get(item_id.lower(), item_id)
        current = manager.current_context().state

        if current == BookingSG.station_from:
            manager.dialog_data["from_name"] = station
            await manager.switch_to(BookingSG.station_to)
        else:
            manager.dialog_data["to_name"] = station
            await manager.switch_to(BookingSG.travel_date)
    finally:
        stop_processing(manager)


async def on_manual_from(message: Message, widget: Any, manager: DialogManager):
    text = message.text.strip()
    station = SMART_STATIONS.get(text.lower(), text.title())
    manager.dialog_data["from_name"] = station
    await manager.switch_to(BookingSG.station_to)


async def on_manual_to(message: Message, widget: Any, manager: DialogManager):
    text = message.text.strip()
    station = SMART_STATIONS.get(text.lower(), text.title())
    manager.dialog_data["to_name"] = station
    await manager.switch_to(BookingSG.travel_date)


async def get_dates_data(**kwargs):
    today = datetime.now()
    return {
        "dates": [
            ("Сегодня", today.strftime("%Y-%m-%d")),
            ("Завтра", (today + timedelta(days=1)).strftime("%Y-%m-%d")),
            ("Послезавтра", (today + timedelta(days=2)).strftime("%Y-%m-%d")),
        ]
    }


async def start_search_and_switch(manager: DialogManager):
    data = manager.dialog_data
    event = manager.event

    if isinstance(event, CallbackQuery):
        chat_id = event.message.chat.id
    else:
        chat_id = event.chat.id

    await manager.switch_to(BookingSG.searching)

    search_task = asyncio.create_task(search_trains(chat_id, data))
    manager.dialog_data["search_task"] = search_task

    try:
        trains = await search_task
    except asyncio.CancelledError:
        logging.info("Ожидание поиска прервано.")
        return

    if not trains:
        try:
            await bot.send_message(
                chat_id,
                "❌ Не удалось загрузить поезда.\n"
                "Сайт БЖД не ответил или поездов нет. Попробуйте ещё раз через минуту."
            )
        except (TelegramForbiddenError, TelegramBadRequest):
            pass
        await manager.switch_to(BookingSG.travel_date)
        return

    from_enc = quote(data["from_name"])
    to_enc = quote(data["to_name"])
    base_link = (
        f"https://pass.rw.by/ru/route/?from={from_enc}"
        f"&to={to_enc}&date={data['travel_date']}"
    )

    manager.dialog_data["trains"] = trains
    manager.dialog_data["base_link"] = base_link
    manager.dialog_data["travel_date_fmt"] = format_date(data["travel_date"])

    await manager.switch_to(BookingSG.choose_train)


async def on_date_selected(
    callback: CallbackQuery,
    widget: Any,
    manager: DialogManager,
    item_id: str,
):
    if is_processing(manager):
        await callback.answer("Подождите...")
        return
    
    try:
        manager.dialog_data["travel_date"] = item_id
        await start_search_and_switch(manager)
    finally:
        stop_processing(manager)


async def on_manual_date(message: Message, widget: Any, manager: DialogManager):
    date_str = message.text.strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        await message.answer("❌ Неверный формат. Введите <code>ГГГГ-ММ-ДД</code>:")
        return

    manager.dialog_data["travel_date"] = date_str
    await start_search_and_switch(manager)


async def get_trains_data(dialog_manager: DialogManager, **kwargs):
    trains = dialog_manager.dialog_data.get("trains", [])
    from_name = dialog_manager.dialog_data.get("from_name", "—")
    to_name = dialog_manager.dialog_data.get("to_name", "—")
    travel_date_fmt = dialog_manager.dialog_data.get("travel_date_fmt", "—")

    items = []
    for t in trains:
        if t["departed"]:
            status = "🛑 Поезд отправился"
        elif t["seats"] > 0:
            details = ", ".join(f"{k}({v})" for k, v in t["details"].items())
            status = f"🟢 {details}"
        else:
            status = "🔴 Нет мест"

        # ДОБАВЛЕНО ВРЕМЯ ПРИБЫТИЯ (arr_time) В ОБЩИЙ СПИСОК
        text = f"{t['index']}. {t['number']} ({t['time']} — {t['arr_time']}) {status}"
        items.append((text, str(t["index"])))

    return {
        "from_name": from_name,
        "to_name": to_name,
        "travel_date_fmt": travel_date_fmt,
        "train_items": items,
    }


async def get_monitoring_info_data(dialog_manager: DialogManager, **kwargs):
    data = dialog_manager.dialog_data
    return {
        "monitor_text": data.get("monitor_text", ""),
        "alt_items": data.get("alt_items", []),
        "from_name": data.get("from_name", "—"),
        "to_name": data.get("to_name", "—"),
        "travel_date_fmt": data.get("travel_date_fmt", "—"),
        "has_alts": bool(data.get("alt_items")),
    }


async def get_train_result_data(dialog_manager: DialogManager, **kwargs):
    data = dialog_manager.dialog_data
    return {
        "result_text": data.get("result_text", ""),
        "base_link": data.get("base_link", "https://pass.rw.by"),
    }


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
        data = manager.dialog_data
        trains = data.get("trains", [])
        base_link = data.get("base_link")
        travel_date = data.get("travel_date")
        chat_id = callback.message.chat.id
        from_name = data.get("from_name", "—")
        to_name = data.get("to_name", "—")
        travel_date_fmt = data.get("travel_date_fmt", "—")

        try:
            choice = int(item_id)
            selected = next(t for t in trains if t["index"] == choice)
        except (ValueError, StopIteration):
            await callback.answer("Поезд не найден", show_alert=True)
            return

        if selected["departed"]:
            await callback.answer("Этот поезд уже отправился", show_alert=True)
            return

        manager.dialog_data["selected_train"] = selected

        if selected["seats"] > 0:
            details_str = ", ".join(f"{k} ({v})" for k, v in selected["details"].items())
            result_text = (
                f"🎯 Выбран поезд <b>{selected['number']}</b> "
                f"({selected['time']} — {selected['arr_time']}).\n"
                f"✅ Отправление: <b>{from_name}</b>\n"
                f"✅ Назначение: <b>{to_name}</b>\n"
                f"📅 Дата: <b>{travel_date_fmt}</b>\n"
                f"🟢 В наличии: <b>{details_str}</b>.\n\n"
                f"👇 Нажмите кнопку для перехода к выбору места:"
            )
            manager.dialog_data["result_text"] = result_text
            await manager.switch_to(BookingSG.train_result)
            await callback.answer()
            return

        train_num = selected["number"]
        monitor_key = make_monitor_key(travel_date, train_num)

        alternatives = [
            t for t in trains
            if t["seats"] > 0 and not t["departed"] and t["number"] != train_num
        ]

        alt_items = []
        for alt in alternatives:
            details = ", ".join(f"{k}({v})" for k, v in alt["details"].items())
            # ДОБАВЛЕНО ВРЕМЯ ПРИБЫТИЯ (arr_time) В СПИСОК АЛЬТЕРНАТИВ
            text = f"{alt['index']}. {alt['number']} ({alt['time']} — {alt['arr_time']}) 🟢 {details}"
            alt_items.append((text, str(alt["index"])))

        if monitor_key in active_monitors:
            users = active_monitors[monitor_key]["users"]
            if chat_id in users:
                monitor_text = f"⏳ Вы уже в очереди на мониторинг поезда <b>{train_num}</b>."
            else:
                users.append(chat_id)
                position = len(users)
                monitor_text = (
                    f"⏳ Поезд <b>{train_num}</b> уже мониторится.\n"
                    f"Вы добавлены в очередь (позиция: {position}).\n"
                    f"Когда места появятся — сначала уведомление получит тот, кто попросил раньше."
                )
        else:
            active_monitors[monitor_key] = {
                "task": None,
                "users": [chat_id],
                "target": selected,
                "base_link": base_link,
                "travel_date": travel_date,
            }
            task = asyncio.create_task(monitor_train(monitor_key))
            active_monitors[monitor_key]["task"] = task

            monitor_text = (
                f"🔴 На поезд <b>{train_num}</b> "
                f"({selected['time']} — {selected['arr_time']}) сейчас нет мест.\n\n"
                f"⏳ <b>Запускаю фоновый мониторинг (каждые 30 сек).</b>\n"
                f"Я пришлю уведомление, как только места появятся!"
            )

        manager.dialog_data["monitor_text"] = monitor_text
        manager.dialog_data["alt_items"] = alt_items

        await manager.switch_to(BookingSG.monitoring_info)
        await callback.answer()
    finally:
        stop_processing(manager)


async def on_alt_selected(
    callback: CallbackQuery,
    widget: Any,
    manager: DialogManager,
    item_id: str,
):
    if is_processing(manager):
        await callback.answer("Подождите...")
        return
    
    try:
        data = manager.dialog_data
        trains = data.get("trains", [])
        from_name = data.get("from_name", "—")
        to_name = data.get("to_name", "—")
        travel_date_fmt = data.get("travel_date_fmt", "—")

        try:
            choice = int(item_id)
            selected = next(t for t in trains if t["index"] == choice)
        except (ValueError, StopIteration):
            await callback.answer("Поезд не найден", show_alert=True)
            return

        if selected["departed"] or selected["seats"] <= 0:
            await callback.answer("Этот поезд уже недоступен", show_alert=True)
            return

        details_str = ", ".join(f"{k} ({v})" for k, v in selected["details"].items())
        result_text = (
            f"🎯 Выбран поезд <b>{selected['number']}</b> "
            f"({selected['time']} — {selected['arr_time']}).\n"
            f"✅ Отправление: <b>{from_name}</b>\n"
            f"✅ Назначение: <b>{to_name}</b>\n"
            f"📅 Дата: <b>{travel_date_fmt}</b>\n"
            f"🟢 В наличии: <b>{details_str}</b>.\n\n"
            f"👇 Нажмите кнопку для перехода к выбору места:"
        )
        manager.dialog_data["result_text"] = result_text
        await manager.switch_to(BookingSG.train_result)
        await callback.answer()
    finally:
        stop_processing(manager)


# ==================== ДИАЛОГ ====================

booking_dialog = Dialog(
    Window(
        Const("🚉 <b>Шаг 1/3. Откуда вы едете?</b>"),
        Column(
            Select(Format("{item}"), id="from_city", item_id_getter=lambda x: x, items="cities", on_click=on_city_selected),
        ),
        SwitchTo(Const("⌨️ Ввести вручную"), id="from_manual_btn", state=BookingSG.station_from_manual),
        Row(
            Button(Const("❌ Отмена"), id="cancel_1", on_click=on_cancel_dialog),
        ),
        state=BookingSG.station_from,
        getter=get_cities_data,
    ),
    Window(
        Const("⌨️ Введите <b>название</b> станции отправления\n(например, <i>Минск-Пассажирский</i>):"),
        MessageInput(on_manual_from),
        Row(
            SwitchTo(Const("◀️ Назад"), id="back_from_man", state=BookingSG.station_from),
            Button(Const("❌ Отмена"), id="cancel_from_man", on_click=on_cancel_dialog),
        ),
        state=BookingSG.station_from_manual,
    ),
    Window(
        Format("✅ Отправление: <b>{dialog_data[from_name]}</b>\n\n🏙️ <b>Шаг 2/3. Куда вы едете?</b>"),
        Column(
            Select(Format("{item}"), id="to_city", item_id_getter=lambda x: x, items="cities", on_click=on_city_selected),
        ),
        SwitchTo(Const("⌨️ Ввести вручную"), id="to_manual_btn", state=BookingSG.station_to_manual),
        Row(
            SwitchTo(Const("◀️ Назад"), id="back_to", state=BookingSG.station_from),
            Button(Const("❌ Отмена"), id="cancel_2", on_click=on_cancel_dialog),
        ),
        state=BookingSG.station_to,
        getter=get_cities_data,
    ),
    Window(
        Const("⌨️ Введите <b>название</b> станции назначения\n(например, <i>Гродно</i>):"),
        MessageInput(on_manual_to),
        Row(
            SwitchTo(Const("◀️ Назад"), id="back_to_man", state=BookingSG.station_to),
            Button(Const("❌ Отмена"), id="cancel_to_man", on_click=on_cancel_dialog),
        ),
        state=BookingSG.station_to_manual,
    ),
    Window(
        Format(
            "✅ Отправление: <b>{dialog_data[from_name]}</b>\n"
            "✅ Назначение: <b>{dialog_data[to_name]}</b>\n\n"
            "📅 <b>Шаг 3/3. Выберите дату поездки:</b>"
        ),
        Column(
            Select(Format("{item[0]}"), id="date_select", item_id_getter=lambda x: x[1], items="dates", on_click=on_date_selected),
        ),
        SwitchTo(Const("⌨️ Ввести вручную"), id="date_manual_btn", state=BookingSG.travel_date_manual),
        Row(
            SwitchTo(Const("◀️ Назад"), id="back_date", state=BookingSG.station_to),
            Button(Const("❌ Отмена"), id="cancel_3", on_click=on_cancel_dialog),
        ),
        state=BookingSG.travel_date,
        getter=get_dates_data,
    ),
    Window(
        Const("⌨️ Введите дату в формате <code>ГГГГ-ММ-ДД</code>:"),
        MessageInput(on_manual_date),
        Row(
            SwitchTo(Const("◀️ Назад"), id="back_date_man", state=BookingSG.travel_date),
            Button(Const("❌ Отмена"), id="cancel_date_man", on_click=on_cancel_dialog),
        ),
        state=BookingSG.travel_date_manual,
    ),
    
    Window(
        Const("🌐 Ищу поезда на сайте БЖД..."),
        Row(
            Button(Const("◀️ Назад"), id="back_search", on_click=on_back_from_search),
            Button(Const("❌ Отмена"), id="cancel_search", on_click=on_cancel_dialog),
        ),
        state=BookingSG.searching,
    ),
    
    Window(
        Multi(
            Format(
                "✅ Отправление: <b>{from_name}</b>\n"
                "✅ Назначение: <b>{to_name}</b>\n"
                "📅 Дата: <b>{travel_date_fmt}</b>\n\n"
                "🚆 <b>Выберите поезд:</b>\n"
            ),
            Const("🟢 - есть места\n🔴 - нет мест (можно включить мониторинг)\n🛑 - поезд уже отправился"),
        ),
        Column(
            Select(Format("{item[0]}"), id="train_select", item_id_getter=lambda x: x[1], items="train_items", on_click=on_train_selected),
        ),
        Row(
            SwitchTo(Const("◀️ Назад"), id="back_train", state=BookingSG.travel_date),
            Button(Const("❌ Закрыть"), id="cancel_train", on_click=on_cancel_dialog),
        ),
        state=BookingSG.choose_train,
        getter=get_trains_data,
    ),
    
    Window(
        Multi(
            Format("{monitor_text}"),
            Const("\n\n💡 <b>Пока ждете, можете выбрать другой поезд с местами:</b>"),
        ),
        Column(
            Select(Format("{item[0]}"), id="alt_select", item_id_getter=lambda x: x[1], items="alt_items", on_click=on_alt_selected),
        ),
        Row(
            Button(Const("◀️ Назад"), id="back_monitor", on_click=on_back_from_monitoring),
            Button(Const("❌ Закрыть"), id="cancel_monitor", on_click=on_cancel_dialog),
        ),
        state=BookingSG.monitoring_info,
        getter=get_monitoring_info_data,
    ),
    
    Window(
        Format("{result_text}"),
        Url(Const("🎫 Выбрать место"), url=Format("{base_link}")),
        Row(
            Button(Const("◀️ Назад"), id="back_result", on_click=on_back_to_trains),
            Button(Const("❌ Закрыть"), id="cancel_result", on_click=on_cancel_dialog),
        ),
        state=BookingSG.train_result,
        getter=get_train_result_data,
    ),
)


# ==================== КОМАНДЫ ====================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    try:
        await message.answer(
            "👋 Привет! Я помогу найти и отследить билеты на поезд БЖД.\n\n"
            "<b>Доступные команды:</b>\n"
            "/book — начать поиск билетов\n"
            "/stop — остановить все мониторинги\n"
            "/cancel — отменить текущий ввод"
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        pass


@dp.message(Command("book"))
async def cmd_book(message: Message, dialog_manager: DialogManager):
    await dialog_manager.start(BookingSG.station_from, mode=StartMode.RESET_STACK)


@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    chat_id = message.chat.id
    stopped = 0

    for key in list(active_monitors.keys()):
        data = active_monitors[key]
        if chat_id in data["users"]:
            data["users"].remove(chat_id)
            stopped += 1

            if not data["users"]:
                task = data.get("task")
                if task and not task.done():
                    task.cancel()
                if key in active_monitors:
                    del active_monitors[key]

    try:
        if stopped:
            await message.answer(f"🛑 Вы остановлены в {stopped} мониторинг(ах).")
        else:
            await message.answer("ℹ️ Вы нигде не стоите в очереди на мониторинг.")
    except (TelegramForbiddenError, TelegramBadRequest):
        pass


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, dialog_manager: DialogManager):
    search_task = dialog_manager.dialog_data.get("search_task")
    if search_task and not search_task.done():
        search_task.cancel()
    
    await dialog_manager.done()
    try:
        await message.answer("❌ Ввод отменён. Для начала напишите /book")
    except (TelegramForbiddenError, TelegramBadRequest):
        pass


@dp.errors()
async def errors_handler(event: ErrorEvent):
    exception = event.exception
    if isinstance(exception, (TelegramForbiddenError, TelegramBadRequest)):
        return True
    logging.exception(f"Необработанная ошибка: {exception}")
    return True


async def main():
    await bot.set_my_commands([
        BotCommand(command="book", description="Начать поиск билетов"),
        BotCommand(command="stop", description="Остановить все мониторинги"),
        BotCommand(command="cancel", description="Отменить текущий ввод"),
        BotCommand(command="start", description="Показать справку"),
    ])

    dp.include_router(booking_dialog)
    setup_dialogs(dp)

    logging.info("Бот запущен. Жду /book...")
    
    try:
        await dp.start_polling(bot)
    except (KeyboardInterrupt, SystemExit):
        logging.info("Получен сигнал завершения (Ctrl+C).")
    except Exception as e:
        logging.error(f"Критическая ошибка работы бота: {e}")
    finally:
        await notify_shutdown()
        logging.info("Бот остановлен.")

if __name__ == "__main__":
    asyncio.run(main())
