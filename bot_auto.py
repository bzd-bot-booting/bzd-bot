import asyncio
import logging
import html
import re
from urllib.parse import quote
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ==================== КОНФИГУРАЦИЯ ====================
API_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "ВАШ_ТОКЕН_СЮДА")
HEADLESS = True          
CHECK_INTERVAL = 30      # Проверяем каждые 30 секунд
# =====================================================

bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# ХРАНИЛИЩЕ ЗАДАЧ: chat_id -> { "номер_поезда": asyncio.Task }
active_monitors: dict[int, dict[str, asyncio.Task]] = {}

SMART_STATIONS = {
    "минск": "Минск-Пассажирский", "минск-пассажирский": "Минск-Пассажирский",
    "брест": "Брест-Центральный", "брест-центральный": "Брест-Центральный",
    "гомель": "Гомель", "витебск": "Витебск", "могилев": "Могилев", "гродно": "Гродно",
}

class BookingStates(StatesGroup):
    station_from = State()
    station_from_manual = State()
    station_to = State()
    station_to_manual = State()
    travel_date = State()
    travel_date_manual = State()
    choose_train = State()

# ---------- Генераторы клавиатур ----------
def get_cities_kb(prefix: str):
    """Генерирует клавиатуру с городами (откуда/куда)"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Минск", callback_data=f"{prefix}:Минск"),
         InlineKeyboardButton(text="Брест", callback_data=f"{prefix}:Брест")],
        [InlineKeyboardButton(text="Гомель", callback_data=f"{prefix}:Гомель"),
         InlineKeyboardButton(text="Витебск", callback_data=f"{prefix}:Витебск")],
        [InlineKeyboardButton(text="Могилев", callback_data=f"{prefix}:Могилев"),
         InlineKeyboardButton(text="Гродно", callback_data=f"{prefix}:Гродно")],
        [InlineKeyboardButton(text="⌨️ Ввести вручную", callback_data=f"{prefix}:manual")]
    ])

def get_dates_kb():
    """Генерирует клавиатуру с датами"""
    today = datetime.now()
    dates = {
        "Сегодня": today.strftime('%Y-%m-%d'),
        "Завтра": (today + timedelta(days=1)).strftime('%Y-%m-%d'),
        "Послезавтра": (today + timedelta(days=2)).strftime('%Y-%m-%d')
    }
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Сегодня", callback_data=f"date:{dates['Сегодня']}")],
        [InlineKeyboardButton(text="Завтра", callback_data=f"date:{dates['Завтра']}")],
        [InlineKeyboardButton(text="Послезавтра", callback_data=f"date:{dates['Послезавтра']}")],
        [InlineKeyboardButton(text="⌨️ Ввести вручную", callback_data="date:manual")]
    ])

# ---------- Парсер поезда ----------
async def parse_train_row(row, travel_date_str):
    train_num_el = await row.query_selector(".train-number")
    if not train_num_el: return None
    t_num = (await train_num_el.inner_text()).strip()
    
    # ИЗМЕНЕНИЕ: Берем весь текст карточки и ищем ВСЕ времена формата ЧЧ:ММ
    row_text = await row.inner_text()
    times = re.findall(r'\b\d{2}:\d{2}\b', row_text)
    
    if len(times) >= 2:
        dep_time = times[0]   # Первое время - отправление
        arr_time = times[1]   # Второе время - прибытие
    elif len(times) == 1:
        dep_time = times[0]
        arr_time = "--:--"
    else:
        dep_time = "--:--"
        arr_time = "--:--"

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
        matches = re.finditer(r'(Плацкартный|Купейный|Сидячий|Общий|Люкс|Мягкий)\s+(\d+)', row_text, re.IGNORECASE)
        for m in matches:
            t_type = m.group(1).capitalize()
            count = int(m.group(2))
            
            if "Плацкарт" in t_type: t_type = "Плацкарт"
            elif "Купей" in t_type: t_type = "Купе"
            elif "Сидяч" in t_type: t_type = "Сидячий"
            elif "Общ" in t_type: t_type = "Общий"
            
            if count > 0:
                seats_details[t_type] = count
                total_seats += count
        
        if total_seats == 0:
            buy_btn = await row.query_selector("a:has-text('ВЫБРАТЬ МЕСТА'), button:has-text('ВЫБРАТЬ МЕСТА')")
            if buy_btn:
                seats_details["Есть места"] = 1
                total_seats = 1

    return {"number": t_num, "time": dep_time, "arr_time": arr_time, "seats": total_seats, "details": seats_details, "departed": is_departed}

# ---------- Команды ----------
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Привет! Я помогу найти и отследить билеты на поезд БЖД.\n"
        "/book — начать поиск\n/stop — остановить все мониторинги\n/cancel — отменить ввод"
    )

@dp.message(Command("book"))
async def cmd_book(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🚉 <b>Шаг 1/3. Откуда вы едете?</b>", 
        reply_markup=get_cities_kb("from")
    )
    await state.set_state(BookingStates.station_from)

@dp.message(Command("stop"))
async def cmd_stop(message: types.Message):
    chat_id = message.chat.id
    user_tasks = active_monitors.get(chat_id, {})
    if user_tasks:
        for train_num, task in list(user_tasks.items()):
            if not task.done(): task.cancel()
            del user_tasks[train_num]
        await message.answer("🛑 Все мониторинги остановлены.")
    else:
        await message.answer("ℹ️ Активных мониторингов нет.")

@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Ввод отменён. Для начала напишите /book")

# ---------- Обработка кнопок (Откуда) ----------
@dp.callback_query(lambda c: c.data.startswith("from:"))
async def process_from_callback(callback: CallbackQuery, state: FSMContext):
    action = callback.data.split(":")[1]
    if action == "manual":
        await callback.message.edit_text("⌨️ Введите <b>название</b> станции отправления (например, <i>Минск-Пассажирский</i>):")
        await state.set_state(BookingStates.station_from_manual)
    else:
        station_name = SMART_STATIONS.get(action.lower(), action)
        await state.update_data(from_name=station_name)
        await callback.message.edit_text(
            f"✅ Отправление: <b>{station_name}</b>\n\n🏙️ <b>Шаг 2/3. Куда вы едете?</b>",
            reply_markup=get_cities_kb("to")
        )
        await state.set_state(BookingStates.station_to)
    await callback.answer()

# ---------- Обработка ручного ввода (Откуда) ----------
@dp.message(BookingStates.station_from_manual)
async def process_from_manual(message: types.Message, state: FSMContext):
    station_name = SMART_STATIONS.get(message.text.strip().lower(), message.text.strip().title())
    await state.update_data(from_name=station_name)
    await message.answer(
        f"✅ Отправление: <b>{station_name}</b>\n\n🏙️ <b>Шаг 2/3. Куда вы едете?</b>",
        reply_markup=get_cities_kb("to")
    )
    await state.set_state(BookingStates.station_to)

# ---------- Обработка кнопок (Куда) ----------
@dp.callback_query(lambda c: c.data.startswith("to:"), BookingStates.station_to)
async def process_to_callback(callback: CallbackQuery, state: FSMContext):
    action = callback.data.split(":")[1]
    if action == "manual":
        await callback.message.edit_text("⌨️ Введите <b>название</b> станции назначения (например, <i>Гродно</i>):")
        await state.set_state(BookingStates.station_to_manual)
    else:
        station_name = SMART_STATIONS.get(action.lower(), action)
        await state.update_data(to_name=station_name)
        await callback.message.edit_text(
            f"✅ Назначение: <b>{station_name}</b>\n\n📅 <b>Шаг 3/3. Выберите дату поездки:</b>",
            reply_markup=get_dates_kb()
        )
        await state.set_state(BookingStates.travel_date)
    await callback.answer()

# ---------- Обработка ручного ввода (Куда) ----------
@dp.message(BookingStates.station_to_manual)
async def process_to_manual(message: types.Message, state: FSMContext):
    station_name = SMART_STATIONS.get(message.text.strip().lower(), message.text.strip().title())
    await state.update_data(to_name=station_name)
    await message.answer(
        f"✅ Назначение: <b>{station_name}</b>\n\n📅 <b>Шаг 3/3. Выберите дату поездки:</b>",
        reply_markup=get_dates_kb()
    )
    await state.set_state(BookingStates.travel_date)

# ---------- Обработка кнопок (Дата) ----------
@dp.callback_query(lambda c: c.data.startswith("date:"), BookingStates.travel_date)
async def process_date_callback(callback: CallbackQuery, state: FSMContext):
    action = callback.data.split(":")[1]
    if action == "manual":
        await callback.message.edit_text("⌨️ Введите дату в формате <code>ГГГГ-ММ-ДД</code>:")
        await state.set_state(BookingStates.travel_date_manual)
    else:
        await callback.message.edit_text(f"✅ Дата: <b>{action}</b>\n\n🌐 Ищу поезда на сайте БЖД...")
        await state.update_data(travel_date=action)
        data = await state.get_data()
        await state.set_state(BookingStates.choose_train)
        asyncio.create_task(search_trains(callback.message.chat.id, data, state))
    await callback.answer()

# ---------- Обработка ручного ввода (Дата) ----------
@dp.message(BookingStates.travel_date_manual)
async def process_date_manual(message: types.Message, state: FSMContext):
    date_str = message.text.strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        await message.answer("❌ Неверный формат. Введите <code>ГГГГ-ММ-ДД</code>:")
        return

    await state.update_data(travel_date=date_str)
    data = await state.get_data()
    await message.answer("🌐 Ищу поезда на сайте БЖД...")
    await state.set_state(BookingStates.choose_train)
    asyncio.create_task(search_trains(message.chat.id, data, state))

# ---------- Поиск поездов ----------
async def search_trains(chat_id: int, data: dict, state: FSMContext):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        page = await browser.new_page()
        try:
            from_enc = quote(data["from_name"])
            to_enc = quote(data["to_name"])
            travel_date = data['travel_date']
            search_url = f"https://pass.rw.by/ru/route/?from={from_enc}&to={to_enc}&date={travel_date}"

            await page.goto(search_url, timeout=60000)
            await page.wait_for_load_state("networkidle")
            await page.wait_for_timeout(3000)

            rows = await page.query_selector_all(".sch-table__row-wrap")
            if not rows:
                await bot.send_message(chat_id, "❌ Поездов не найдено.")
                await state.clear()
                return

            trains = []
            lines = ["🚆 <b>Выберите поезд из списка:</b>\n"]
            lines.append("🟢 - есть места\n🔴 - нет мест (можно включить мониторинг)\n🛑 - поезд уже отправился\n")
            
            for idx, row in enumerate(rows, 1):
                t = await parse_train_row(row, travel_date)
                if not t: continue
                t["index"] = idx
                trains.append(t)

                if t["departed"]:
                    status = "🛑 Отправился"
                elif t["seats"] > 0:
                    details_str = ", ".join([f"{k} ({v})" for k, v in t["details"].items()])
                    status = f"🟢 {details_str}"
                else:
                    status = "🔴 Нет мест"
                
                lines.append(f"{idx}. <b>{t['number']}</b> ({t['time']} — {t['arr_time']}) — {status}")

            await state.update_data(trains=trains, base_link=f"https://pass.rw.by/ru/route/?from={from_enc}&to={to_enc}&date={travel_date}", travel_date=travel_date)
            
            lines.append("\n✍️ <b>Напишите номер поезда</b>, чтобы получить ссылку.")
            await bot.send_message(chat_id, "\n".join(lines))

        except Exception as e:
            logging.exception("Ошибка поиска")
            await bot.send_message(chat_id, f"❌ Ошибка: <code>{html.escape(str(e))}</code>")
            await state.clear()
        finally:
            await browser.close()

# ---------- Выбор поезда ----------
@dp.message(BookingStates.choose_train)
async def process_choice(message: types.Message, state: FSMContext):
    data = await state.get_data()
    trains = data.get("trains", [])
    base_link = data.get("base_link")
    travel_date = data.get("travel_date")
    chat_id = message.chat.id

    try:
        choice = int(message.text.strip())
        selected = next(t for t in trains if t["index"] == choice)
    except (ValueError, StopIteration):
        await message.answer("⚠️ Введите правильный номер поезда из списка.")
        return

    if selected["departed"]:
        await message.answer(f"🛑 Поезд {selected['number']} уже отправился. Выберите другой поезд из списка.")
        return

    if selected["seats"] > 0:
        details_str = ", ".join([f"{k} ({v})" for k, v in selected["details"].items()])
        await message.answer(
            f"🎯 Выбран поезд <b>{selected['number']}</b> ({selected['time']} — {selected['arr_time']}).\n"
            f"🟢 В наличии: <b>{details_str}</b>.\n\n"
            f"👇 Нажмите кнопку для перехода к выбору места:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🎫 Выбрать место", url=base_link)]
            ])
        )
        await state.clear()
        return

    train_num = selected["number"]
    if chat_id in active_monitors and train_num in active_monitors[chat_id]:
        await message.answer(f"⏳ За поездом <b>{train_num}</b> уже ведется мониторинг. Я пришлю уведомление, как только места появятся.")
    else:
        if chat_id not in active_monitors:
            active_monitors[chat_id] = {}
        
        task = asyncio.create_task(monitor_train(chat_id, selected, base_link, travel_date))
        active_monitors[chat_id][train_num] = task
        
        msg = f"🔴 На поезд <b>{train_num}</b> ({selected['time']} — {selected['arr_time']}) сейчас нет мест.\n\n"
        msg += f"⏳ <b>Запускаю фоновый мониторинг (каждые 30 сек).</b> Я пришлю уведомление, как только места появятся!\n\n"

        alternatives = [t for t in trains if t["seats"] > 0 and not t["departed"] and t["number"] != train_num]
        if alternatives:
            msg += "💡 <b>Пока вы ждете, можете выбрать другой поезд с местами:</b>\n"
            for alt in alternatives:
                alt_details = ", ".join([f"{k} ({v})" for k, v in alt["details"].items()])
                msg += f"• {alt['index']}. {alt['number']} ({alt['time']} — {alt['arr_time']}) — {alt_details}\n"
            msg += "\n👇 <i>Просто напишите номер поезда из этого списка. Старые мониторинги отменяться НЕ будут.</i>"
        
        await message.answer(msg)

# ---------- Мониторинг ----------
async def monitor_train(chat_id: int, target_train: dict, base_link: str, travel_date_str: str):
    train_num = target_train["number"]
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        page = await browser.new_page()
        try:
            while True:
                await page.goto(base_link, timeout=60000)
                await page.wait_for_load_state("networkidle")
                await page.wait_for_timeout(3000)

                rows = await page.query_selector_all(".sch-table__row-wrap")
                found_target = False
                
                for row in rows:
                    t = await parse_train_row(row, travel_date_str)
                    if t and t["number"] == train_num:
                        found_target = True
                        
                        if t["departed"]:
                            await bot.send_message(chat_id, f"🛑 Поезд <b>{train_num}</b> отправился. Мониторинг остановлен.")
                            return
                            
                        if t["seats"] > 0:
                            details_str = ", ".join([f"{k} ({v})" for k, v in t["details"].items()])
                            await bot.send_message(
                                chat_id,
                                f"🚨 <b>На поезд {train_num} появились места!</b>\n"
                                f"🕒 Время: {t['time']} — {t['arr_time']}\n"
                                f"🟢 В наличии: <b>{details_str}</b>.\n\n"
                                f"Скорее переходите к выбору:",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                    [InlineKeyboardButton(text="🎫 Выбрать место", url=base_link)]
                                ])
                            )
                            return
                        break
                
                if not found_target:
                     await bot.send_message(chat_id, f"⚠️ Поезд {train_num} исчез из расписания. Мониторинг остановлен.")
                     return

                logging.info(f"Мониторинг поезда {train_num}: мест пока нет.")
                await asyncio.sleep(CHECK_INTERVAL)

        except asyncio.CancelledError:
            logging.info(f"Мониторинг поезда {train_num} отменен вручную.")
        except Exception as e:
            logging.error(f"Ошибка мониторинга поезда {train_num}: {e}")
        finally:
            await browser.close()
            if chat_id in active_monitors and train_num in active_monitors[chat_id]:
                del active_monitors[chat_id][train_num]

# ---------- Точка входа ----------
async def main():
    logging.info("Бот запущен. Жду /book...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
