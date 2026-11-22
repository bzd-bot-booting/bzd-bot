
from datetime import date, timedelta

from aiogram_dialog import Dialog, Window
from aiogram_dialog.widgets.text import Const, Format, Multi
from aiogram_dialog.widgets.kbd import (
    Column,
    Select,
    SwitchTo,
    Url,
    Button,
    Row,
)
from aiogram_dialog.widgets.input import MessageInput

from app.handlers.booking import (
    get_cities_data,
    on_city_selected,
    on_manual_from,
    on_manual_to,
    on_date_selected,
    on_manual_date,
    on_ticket_count,
    on_max_price,
    get_trains_data,
    on_train_selected,
    get_monitoring_info_data,
    on_alt_selected,
    on_back_from_monitoring,
    get_train_result_data,
    on_back_to_trains,
    on_cancel_dialog,
    on_home_dialog,
    on_back_dialog,
    on_dialog_back_to,
    on_back_from_search,
    on_back_from_seat_scan,
    on_monitor_ticket_count,
    on_start_auto_booking,
    on_booking_login,
    on_booking_password,
    get_car_types_data,
    on_car_type_selected,
    get_seat_position_data,
    on_seat_position_selected,
    get_seat_scan_data,
    on_booking_seat_scan_item,
    on_bedding_yes,
    on_bedding_no,
    on_passenger_last_name,
    on_passenger_first_name,
    on_passenger_middle_name,
    on_passenger_document_type_selected,
    on_passenger_document_number,
    on_passenger_data_confirmation,
    get_passenger_document_data,
    get_passenger_confirmation_data,
)
from app.handlers.commands import (
    get_main_menu_data,
    on_menu_book,
    on_menu_monitors,
    on_menu_stop,
    on_menu_help,
    get_my_monitors_data,
    on_my_monitor_selected,
    get_monitor_details_data,
    on_monitor_profile,
    get_monitor_profile_data,
    on_monitor_history,
    get_monitor_history_data,
    on_monitor_stop,
    on_monitor_delete,
    on_monitor_resume,
    on_monitor_edit_price,
    on_monitor_price_input,
    get_monitor_price_data,
    on_monitor_passenger_edit,
    get_monitor_passengers_data,
    on_monitor_passenger_selected,
    get_monitor_passenger_edit_data,
    on_monitor_passenger_field_selected,
    get_monitor_passenger_edit_value_data,
    on_monitor_passenger_edit_value,
    get_monitor_document_type_data,
    on_monitor_document_type_selected,
    on_monitor_bedding_yes,
    on_monitor_bedding_no,
)
from app.states import BookingSG
from app.utils import safe


async def get_from_data(dialog_manager, **kwargs):
    cities_data = await get_cities_data()

    return {
        "from_name": dialog_manager.dialog_data.get(
            "from_name", "—"
        ),
        "cities": cities_data["cities"],
    }


async def get_route_data(dialog_manager, **kwargs):
    data = dialog_manager.dialog_data

    return {
        "from_name": safe(data.get("from_name", "—")),
        "to_name": safe(data.get("to_name", "—")),
    }


async def get_dates_window_data(dialog_manager, **kwargs):
    data = dialog_manager.dialog_data
    today = date.today()

    return {
        "from_name": safe(data.get("from_name", "—")),
        "to_name": safe(data.get("to_name", "—")),
        "dates": [
            ("Сегодня", today.strftime("%Y-%m-%d")),
            ("Завтра", (today + timedelta(days=1)).strftime("%Y-%m-%d")),
            (
                "Послезавтра",
                (today + timedelta(days=2)).strftime("%Y-%m-%d"),
            ),
        ],
    }




async def get_input_error(dialog_manager, **kwargs):
    return {"input_error": dialog_manager.dialog_data.get("input_error", "")}

async def get_ticket_data(dialog_manager, **kwargs):
    selected = dialog_manager.dialog_data.get(
        "selected_train", {}
    )

    return {
        "train_number": safe(selected.get("number", "—")),
        "input_error": dialog_manager.dialog_data.get("input_error", ""),
    }


booking_dialog = Dialog(

    # Выбор станции отправления.
    Window(
        Const("🚉 <b>Шаг 1/3. Откуда вы едете?</b>"),

        Column(
            Select(
                Format("{item}"),
                id="from_city",
                item_id_getter=lambda x: x,
                items="cities",
                on_click=on_city_selected,
            ),
        ),

        SwitchTo(
            Const("⌨️ Ввести вручную"),
            id="from_manual_btn",
            state=BookingSG.station_from_manual,
        ),

        Row(
            Button(
                Const("◀️ Назад"),
                id="back_1",
                on_click=on_back_dialog,
            ),
            Button(
                Const("❌ Отмена"),
                id="cancel_1",
                on_click=on_cancel_dialog,
            ),
        ),

        Button(
            Const("🏠 Домой"),
            id="home_station_from",
            on_click=on_home_dialog,
        ),
        state=BookingSG.station_from,
        getter=get_cities_data,
    ),

    # Ручной ввод станции отправления.
    Window(
        Multi(
            Const(
                "⌨️ Введите название станции отправления "
                "(например, Минск-Пассажирский):"
            ),
            Format("{input_error}"),
        ),

        MessageInput(on_manual_from),

        Row(
            Button(
                Const("◀️ Назад"),
                id="back_from_man",
                on_click=lambda c, b, m: on_dialog_back_to(c, b, m, BookingSG.station_from),
            ),
            Button(
                Const("❌ Отмена"),
                id="cancel_from_man",
                on_click=on_cancel_dialog,
            ),
        ),

        Button(
            Const("🏠 Домой"),
            id="home_station_from_manual",
            on_click=on_home_dialog,
        ),
        state=BookingSG.station_from_manual,
        getter=get_input_error,
    ),

    # Выбор станции назначения.
    Window(
        Format(
            "✅ Отправление: <b>{from_name}</b>\n\n"
            "🏙️ <b>Шаг 2/3. Куда вы едете?</b>"
        ),

        Column(
            Select(
                Format("{item}"),
                id="to_city",
                item_id_getter=lambda x: x,
                items="cities",
                on_click=on_city_selected,
            ),

            SwitchTo(
                Const("⌨️ Ввести вручную"),
                id="to_manual_btn",
                state=BookingSG.station_to_manual,
            ),

            Button(
                Const("◀️ Назад"),
                id="back_to",
                on_click=lambda c, b, m: on_dialog_back_to(c, b, m, BookingSG.station_from),
            ),

            Button(
                Const("❌ Отмена"),
                id="cancel_2",
                on_click=on_cancel_dialog,
            ),
        ),

        Button(
            Const("🏠 Домой"),
            id="home_station_to",
            on_click=on_home_dialog,
        ),
        state=BookingSG.station_to,
        getter=get_from_data,
    ),

    # Ручной ввод станции назначения.
    Window(
        Multi(
            Const(
                "⌨️ Введите название станции назначения "
                "(например, Гродно):"
            ),
            Format("{input_error}"),
        ),

        MessageInput(on_manual_to),

        Row(
            Button(
                Const("◀️ Назад"),
                id="back_to_man",
                on_click=lambda c, b, m: on_dialog_back_to(c, b, m, BookingSG.station_to),
            ),
            Button(
                Const("❌ Отмена"),
                id="cancel_to_man",
                on_click=on_cancel_dialog,
            ),
        ),

        Button(
            Const("🏠 Домой"),
            id="home_station_to_manual",
            on_click=on_home_dialog,
        ),
        state=BookingSG.station_to_manual,
        getter=get_input_error,
    ),

    # Выбор даты поездки.
    Window(
        Format(
            "✅ Отправление: <b>{from_name}</b>\n"
            "✅ Назначение: <b>{to_name}</b>\n\n"
            "📅 <b>Шаг 3/3. Выберите дату поездки:</b>"
        ),

        Column(
            Select(
                Format("{item[0]}"),
                id="date_select",
                item_id_getter=lambda x: x[1],
                items="dates",
                on_click=on_date_selected,
            ),
        ),

        SwitchTo(
            Const("⌨️ Ввести вручную"),
            id="date_manual_btn",
            state=BookingSG.travel_date_manual,
        ),

        Row(
            Button(
                Const("◀️ Назад"),
                id="back_date",
                on_click=lambda c, b, m: on_dialog_back_to(c, b, m, BookingSG.station_to),
            ),
            Button(
                Const("❌ Отмена"),
                id="cancel_3",
                on_click=on_cancel_dialog,
            ),
        ),

        Button(
            Const("🏠 Домой"),
            id="home_travel_date",
            on_click=on_home_dialog,
        ),
        state=BookingSG.travel_date,
        getter=get_dates_window_data,
    ),

    # Ручной ввод даты.
    Window(
        Multi(
            Const(
                "⌨️ Введите дату в формате "
                "<code>ГГГГ-ММ-ДД</code> "
                "(например, 2026-12-01):"
            ),
            Format("{input_error}"),
        ),

        MessageInput(on_manual_date),

        Row(
            Button(
                Const("◀️ Назад"),
                id="back_date_man",
                on_click=lambda c, b, m: on_dialog_back_to(c, b, m, BookingSG.travel_date),
            ),
            Button(
                Const("❌ Отмена"),
                id="cancel_date_man",
                on_click=on_cancel_dialog,
            ),
        ),

        Button(
            Const("🏠 Домой"),
            id="home_travel_date_manual",
            on_click=on_home_dialog,
        ),
        state=BookingSG.travel_date_manual,
        getter=get_input_error,
    ),

    # Количество билетов для поиска.
    Window(
        Multi(
            Const(
                "🎟 <b>Шаг 4/5. Сколько билетов ищем?</b>\n\n"
                "Введите необходимое количество билетов."
            ),
            Format("{input_error}"),
        ),

        MessageInput(on_ticket_count),

        Row(
            Button(
                Const("◀️ Назад"),
                id="back_count",
                on_click=lambda c, b, m: on_dialog_back_to(c, b, m, BookingSG.travel_date),
            ),
            Button(
                Const("❌ Отмена"),
                id="cancel_count",
                on_click=on_cancel_dialog,
            ),
        ),

        Button(
            Const("🏠 Домой"),
            id="home_ticket_count",
            on_click=on_home_dialog,
        ),
        state=BookingSG.ticket_count,
        getter=get_input_error,
    ),

    # Максимальная стоимость билета.
    Window(
        Multi(
            Const(
                "💰 <b>Шаг 5/5. На какую сумму рассчитываем?</b>\n\n"
                "Введите максимальную стоимость одного билета "
                "в белорусских рублях.\n"
                "Например: 35 или 35.50."
            ),
            Format("{input_error}"),
        ),

        MessageInput(on_max_price),

        Row(
            Button(
                Const("◀️ Назад"),
                id="back_price",
                on_click=lambda c, b, m: on_dialog_back_to(c, b, m, BookingSG.ticket_count),
            ),
            Button(
                Const("❌ Отмена"),
                id="cancel_price",
                on_click=on_cancel_dialog,
            ),
        ),

        Button(
            Const("🏠 Домой"),
            id="home_max_price",
            on_click=on_home_dialog,
        ),
        state=BookingSG.max_price,
        getter=get_input_error,
    ),

    # Выполнение поиска.
    Window(
        Const("🌐 Ищу поезда на сайте БЖД..."),

        Row(
            Button(
                Const("◀️ Назад"),
                id="back_search",
                on_click=on_back_from_search,
            ),
            Button(
                Const("❌ Отмена"),
                id="cancel_search",
                on_click=on_cancel_dialog,
            ),
        ),

        Button(
            Const("🏠 Домой"),
            id="home_searching",
            on_click=on_home_dialog,
        ),
        state=BookingSG.searching,
    ),

    # Список найденных поездов.
    Window(
        Multi(
            Format(
                "✅ Отправление: <b>{from_name}</b>\n"
                "✅ Назначение: <b>{to_name}</b>\n"
                "📅 Дата: <b>{travel_date_fmt}</b>\n\n"
                "🚆 <b>Выберите поезд:</b>\n"
            ),
            Const(
                "🟢 — есть места\n"
                "🔴 — нет мест (можно включить мониторинг)\n"
                "🛑 — поезд уже отправился"
            ),
            Format("{input_error}"),
        ),

        Column(
            Select(
                Format("{item[0]}"),
                id="train_select",
                item_id_getter=lambda x: x[1],
                items="train_items",
                on_click=on_train_selected,
            ),
        ),

        Row(
            Button(
                Const("◀️ Назад"),
                id="back_train",
                on_click=lambda c, b, m: on_dialog_back_to(c, b, m, BookingSG.travel_date),
            ),
            Button(
                Const("❌ Отмена"),
                id="cancel_train",
                on_click=on_cancel_dialog,
            ),
        ),

        Button(
            Const("🏠 Домой"),
            id="home_choose_train",
            on_click=on_home_dialog,
        ),
        state=BookingSG.choose_train,
        getter=get_trains_data,
    ),

    # Ввод количества билетов для мониторинга.
    Window(
        Multi(
            Format(
                "🔴 На поезд <b>{train_number}</b> "
                "сейчас не найдено мест.\n\n"
                "Сколько билетов нужно отслеживать? "
                "Введите целое число."
            ),
            Format("{input_error}"),
        ),
        MessageInput(on_monitor_ticket_count),

        Row(
            Button(
                Const("◀️ Назад"),
                id="back_monitor_count",
                on_click=on_back_dialog,
            ),
            Button(
                Const("❌ Отмена"),
                id="cmc",
                on_click=on_cancel_dialog,
            ),
        ),

        Button(
            Const("🏠 Домой"),
            id="home_monitor_ticket_count",
            on_click=on_home_dialog,
        ),
        state=BookingSG.monitor_ticket_count,
        getter=get_ticket_data,
    ),

    # Информация о мониторинге.
    Window(
        Format("{monitor_text}"),

        Column(
            Select(
                Format("{item[0]}"),
                id="alt_select",
                item_id_getter=lambda x: x[1],
                items="alt_items",
                on_click=on_alt_selected,
            ),
        ),

        Row(
            Button(
                Const("◀️ Назад"),
                id="back_monitor",
                on_click=on_back_from_monitoring,
            ),
            Button(
                Const("❌ Отмена"),
                id="cancel_monitor",
                on_click=on_cancel_dialog,
            ),
        ),

        Button(
            Const("🏠 Домой"),
            id="home_monitoring_info",
            on_click=on_home_dialog,
        ),
        state=BookingSG.monitoring_info,
        getter=get_monitoring_info_data,
    ),

    # Результат выбора поезда.
    Window(
        Multi(Format("{result_text}"), Format("{input_error}")),

        Url(
            Const("🎫 Перейти к выбору места"),
            url=Format("{base_link}"),
        ),

        Button(
            Const("🤖 Автоматически оформить"),
            id="auto_booking",
            on_click=on_start_auto_booking,
        ),

        Row(
            Button(
                Const("◀️ Назад"),
                id="back_result",
                on_click=on_back_to_trains,
            ),
            Button(
                Const("❌ Отмена"),
                id="cancel_result",
                on_click=on_cancel_dialog,
            ),
        ),

        Button(
            Const("🏠 Домой"),
            id="home_train_result",
            on_click=on_home_dialog,
        ),
        state=BookingSG.train_result,
        getter=get_train_result_data,
    ),

    Window(
        Multi(
            Const(
                "🔐 <b>Авторизация на сайте</b>\n\n"
                "Введите логин личного кабинета pass.rw.by:"
            ),
            Format("{input_error}"),
        ),
        MessageInput(on_booking_login),
        Row(
            Button(
                Const("◀️ Назад"),
                id="back_booking_login",
                on_click=on_back_dialog,
            ),
            Button(
            Const("❌ Отмена"),
            id="clog",
            on_click=on_cancel_dialog,
            ),
        ),
        Button(
            Const("🏠 Домой"),
            id="home_booking_login",
            on_click=on_home_dialog,
        ),
        state=BookingSG.booking_login,
        getter=get_input_error,
    ),

    Window(
        Multi(
            Const(
                "🔑 <b>Введите пароль</b>\n\n"
                "Отправьте пароль отдельным сообщением. Бот удалит сообщение после обработки."
            ),
            Format("{input_error}"),
        ),
        MessageInput(on_booking_password),
        Row(
            Button(
                Const("◀️ Назад"),
                id="bpw",
                on_click=on_back_dialog,
            ),
            Button(
            Const("❌ Отмена"),
            id="cpw",
            on_click=on_cancel_dialog,
            ),
        ),
        Button(
            Const("🏠 Домой"),
            id="home_booking_password",
            on_click=on_home_dialog,
        ),
        state=BookingSG.booking_password,
        getter=get_input_error,
    ),

    Window(
        Const(
            "🚆 <b>Выберите тип вагона</b>\n\n"
            "Варианты получены непосредственно с сайта БЖД."
        ),
        Column(
            Select(
                Format("{item[0]}"),
                id="cars",
                item_id_getter=lambda item: item[1],
                items="car_type_items",
                on_click=on_car_type_selected,
            ),
        ),
        Row(
            Button(
                Const("◀️ Назад"),
                id="bcar",
                on_click=on_back_dialog,
            ),
            Button(
            Const("❌ Отмена"),
            id="ccar",
            on_click=on_cancel_dialog,
            ),
        ),
        Button(
            Const("🏠 Домой"),
            id="home_booking_choose_car_type",
            on_click=on_home_dialog,
        ),
        state=BookingSG.booking_choose_car_type,
        getter=get_car_types_data,
    ),


    Window(
        Multi(
            Const(
                "🛏 <b>Выберите расположение места</b>\n\n"
                "Варианты зависят от выбранного типа вагона."
            ),
            Format("{seat_scan_notice}"),
            Format("{input_error}"),
        ),
        Column(
            Select(
                Format("{item[0]}"),
                id="poss",
                item_id_getter=lambda item: item[1],
                items="seat_position_items",
                on_click=on_seat_position_selected,
            ),
        ),
        Row(
            Button(
                Const("◀️ Назад"),
                id="bpos",
                on_click=on_back_dialog,
            ),
            Button(
            Const("❌ Отмена"),
            id="cpos",
            on_click=on_cancel_dialog,
            ),
        ),
        Button(
            Const("🏠 Домой"),
            id="home_booking_choose_seat_position",
            on_click=on_home_dialog,
        ),
        state=BookingSG.booking_choose_seat_position,
        getter=get_seat_position_data,
    ),

    # Номера вагонов и мест не дублируются отдельным списком:
    # кнопки выбора находятся только под соответствующими скриншотами.
    Window(
        Multi(
            Format("{seat_scan_text}"),
            Const("Выберите свободное место с помощью кнопок под фотографиями вагонов."),
            Format("{input_error}"),
        ),
        Row(
            Button(
                Const("◀️ Назад"),
                id="seat_scan_back",
                on_click=on_back_from_seat_scan,
            ),
            Button(
                Const("❌ Отмена"),
                id="seat_scan_cancel",
                on_click=on_cancel_dialog,
            ),
        ),
        Button(
            Const("🏠 Домой"),
            id="home_max_price",
            on_click=on_home_dialog,
        ),
        state=BookingSG.booking_choose_seat,
        getter=get_seat_scan_data,
    ),

    # Данные пассажира: фамилия.
    Window(
        Multi(
            Format("{booking_notice}"),
            Format(
                "👤 <b>Пассажир {passenger_number}/{passenger_count}</b>\n\n"
                "Введите фамилию так, как она указана в документе:"
            ),
            Format("{input_error}"),
        ),
        MessageInput(on_passenger_last_name),
        Row(
            Button(
                Const("◀️ Назад"),
                id="blast",
                on_click=on_back_dialog,
            ),
            Button(
                Const("❌ Отмена"),
                id="clast",
                on_click=on_cancel_dialog,
            ),
        ),

        Button(
            Const("🏠 Домой"),
            id="home_booking_passenger_last_name",
            on_click=on_home_dialog,
        ),
        state=BookingSG.booking_passenger_last_name,
        getter=get_passenger_document_data,
    ),

    # Данные пассажира: имя.
    Window(
        Multi(Const("Введите имя пассажира:"), Format("{input_error}")),
        MessageInput(on_passenger_first_name),
        Row(
            Button(
                Const("◀️ Назад"),
                id="bfirst",
                on_click=on_back_dialog,
            ),
            Button(
                Const("❌ Отмена"),
                id="cfirst",
                on_click=on_cancel_dialog,
            ),
        ),

        Button(
            Const("🏠 Домой"),
            id="home_booking_passenger_first_name",
            on_click=on_home_dialog,
        ),
        state=BookingSG.booking_passenger_first_name,
        getter=get_passenger_document_data,
    ),

    # Данные пассажира: отчество.
    Window(
        Multi(
            Const(
                "Введите отчество пассажира.\n"
                "Если отчества нет, отправьте дефис: -"
            ),
            Format("{input_error}"),
        ),
        MessageInput(on_passenger_middle_name),
        Row(
            Button(
                Const("◀️ Назад"),
                id="bmid",
                on_click=on_back_dialog,
            ),
            Button(
                Const("❌ Отмена"),
                id="cmid",
                on_click=on_cancel_dialog,
            ),
        ),

        Button(
            Const("🏠 Домой"),
            id="home_booking_passenger_middle_name",
            on_click=on_home_dialog,
        ),
        state=BookingSG.booking_passenger_middle_name,
        getter=get_passenger_document_data,
    ),

    # Выбор документа из вариантов, полученных с сайта.
    Window(
        Multi(
            Format(
                "📄 <b>Пассажир {passenger_number}/{passenger_count}</b>\n\n"
                "Выберите тип документа из доступных на сайте вариантов:"
            ),
            Format("{input_error}"),
        ),
        Column(
            Select(
                Format("{item[0]}"),
                id="docs",
                item_id_getter=lambda item: item[1],
                items="document_type_items",
                on_click=on_passenger_document_type_selected,
            ),
        ),
        Row(
            Button(
                Const("◀️ Назад"),
                id="bdoctype",
                on_click=on_back_dialog,
            ),
            Button(
                Const("❌ Отмена"),
                id="cdoctype",
                on_click=on_cancel_dialog,
            ),
        ),
        Button(
            Const("🏠 Домой"),
            id="home_booking_passenger_document_type",
            on_click=on_home_dialog,
        ),
        getter=get_passenger_document_data,
        state=BookingSG.booking_passenger_document_type,
    ),

    # Номер документа.
    Window(
        Multi(Const("Введите номер документа пассажира:"), Format("{input_error}")),
        MessageInput(on_passenger_document_number),
        Row(
            Button(
                Const("◀️ Назад"),
                id="bdocnum",
                on_click=on_back_dialog,
            ),
            Button(
                Const("❌ Отмена"),
                id="cdocnum",
                on_click=on_cancel_dialog,
            ),
        ),

        Button(
            Const("🏠 Домой"),
            id="home_booking_passenger_document_number",
            on_click=on_home_dialog,
        ),
        state=BookingSG.booking_passenger_document_number,
        getter=get_passenger_document_data,
    ),

    # Выбор постельного белья.
    Window(
        Multi(
            Const(
                "🛏 <b>Нужно ли постельное бельё?</b>\n\n"
                "Выберите подходящий вариант."
            ),
            Format("{input_error}"),
        ),
        Row(
            Button(
                Const("Да, нужно"),
                id="bedyes",
                on_click=on_bedding_yes,
            ),
            Button(
                Const("Нет"),
                id="booking_bedding_no",
                on_click=on_bedding_no,
            ),
        ),
        Row(
            Button(
                Const("◀️ Назад"),
                id="bbed",
                on_click=on_back_dialog,
            ),
            Button(
            Const("❌ Отмена"),
            id="cbed",
            on_click=on_cancel_dialog,
            ),
        ),
        Button(
            Const("🏠 Домой"),
            id="home_booking_bedding",
            on_click=on_home_dialog,
        ),
        state=BookingSG.booking_bedding,
        getter=get_input_error,
    ),


    # Подтверждение заполнения формы.
    Window(
        Format(
            "🧾 <b>Проверьте данные заказа перед подтверждением</b>\n\n"
            "🚆 Поезд: <b>{train_number}</b>\n"
            "📍 Откуда: <b>{route_from}</b>\n"
            "📍 Куда: <b>{route_to}</b>\n"
            "📅 Дата: <b>{travel_date}</b>\n"
            "🕒 Отправление: <b>{departure_time}</b>\n"
            "🕒 Прибытие: <b>{arrival_time}</b>\n\n"
            "👤 ФИО: <b>{passenger_fio}</b>\n"
            "📄 Вид документа: <b>{passenger_document_type}</b>\n"
            "🔢 Паспортные данные: <b>{passenger_document_number}</b>\n\n"
            "🚃 Тип вагона: <b>{car_type_name}</b>\n"
            "🚃 Номер вагона: <b>{carriage_number}</b>\n"
            "💺 Номер места: <b>{seat_number}</b>\n\n"
            "Пассажиров в заказе: <b>{passenger_count}</b>\n"
            "{bedding_notice}\n{input_error}"
        ),
        Button(
            Const("Подтвердить данные"),
            id="confirm",
            on_click=on_passenger_data_confirmation,
        ),
        Row(
            Button(
                Const("◀️ Назад"),
                id="bconfirm",
                on_click=on_back_dialog,
            ),
            Button(
                Const("❌ Отмена"),
                id="cconfirm",
                on_click=on_cancel_dialog,
            ),
        ),
        Button(
            Const("🏠 Домой"),
            id="home_booking_passenger_confirmation",
            on_click=on_home_dialog,
        ),
        getter=get_passenger_confirmation_data,
        state=BookingSG.booking_passenger_confirmation,
    ),


    # Главное меню является первым окном диалога; остальные разделы
    # переключаются через switch_to и редактируют это же сообщение.
    Window(
        Format("{main_menu_text}"),
        Column(
            Button(Const("🔎 Найти билеты"), id="menu_dialog_book",
                   on_click=on_menu_book),
            Button(Const("📋 Мои мониторинги"), id="menu_dialog_monitors",
                   on_click=on_menu_monitors),
            Button(Const("🛑 Остановить все мониторинги"), id="menu_dialog_stop",
                   on_click=on_menu_stop),
            Button(Const("ℹ️ Помощь"), id="menu_dialog_help",
                   on_click=on_menu_help),
        ),
        state=BookingSG.main_menu,
        getter=get_main_menu_data,
    ),

    # Раздел «Мои мониторинги». Все экраны ниже редактируют одно сообщение.
    Window(
        Format("{monitor_list_text}"),
        Column(
            Select(
                Format("{item[0]}"),
                id="my_monitor_select",
                item_id_getter=lambda item: item[1],
                items="monitor_items",
                on_click=on_my_monitor_selected,
            ),
        ),
        Row(
            Button(Const("🔄 Обновить"), id="refresh_my_monitors",
                   on_click=lambda c, b, m: on_dialog_back_to(c, b, m, BookingSG.my_monitors)),
            Button(Const("◀️ Назад"), id="my_monitors_back",
                   on_click=lambda c, b, m: m.switch_to(BookingSG.main_menu)),
        ),
        Button(
            Const("🏠 Домой"),
            id="home_my_monitors",
            on_click=on_home_dialog,
        ),
        state=BookingSG.my_monitors,
        getter=get_my_monitors_data,
    ),

    Window(
        Format("{monitor_details_text}"),
        Row(
            Button(Const("👤 Данные для оформления"), id="monitor_profile_btn",
                   on_click=on_monitor_profile),
            Button(Const("🧾 История"), id="monitor_history_btn",
                   on_click=on_monitor_history),
        ),
        Row(
            Button(Const("💰 Изменить цену"), id="monitor_price_btn",
                   on_click=on_monitor_edit_price),
            Button(Const("⏸ Остановить"), id="monitor_stop_btn",
                   on_click=on_monitor_stop),
        ),
        Button(Const("▶️ Возобновить"), id="monitor_resume_btn",
               on_click=on_monitor_resume),
        Button(Const("🗑 Удалить мониторинг"), id="monitor_delete_btn",
               on_click=on_monitor_delete),
        Button(Const("◀️ Назад"), id="monitor_back_list",
               on_click=lambda c, b, m: on_dialog_back_to(c, b, m, BookingSG.my_monitors)),
        Button(
            Const("🏠 Домой"),
            id="home_monitor_details",
            on_click=on_home_dialog,
        ),
        state=BookingSG.monitor_details,
        getter=get_monitor_details_data,
    ),

    Window(
        Format("{monitor_profile_text}"),
        Button(Const("✏️ Изменить данные пассажиров"), id="profile_edit_passengers",
               on_click=on_monitor_passenger_edit),
        Button(Const("◀️ Назад"), id="profile_back",
               on_click=lambda c, b, m: on_dialog_back_to(c, b, m, BookingSG.monitor_details)),
        Button(
            Const("🏠 Домой"),
            id="home_monitor_profile",
            on_click=on_home_dialog,
        ),
        state=BookingSG.monitor_profile,
        getter=get_monitor_profile_data,
    ),

    Window(
        Format("{passenger_select_text}"),
        Column(
            Select(
                Format("{item[0]}"),
                id="monitor_passenger_select",
                item_id_getter=lambda item: item[1],
                items="passenger_items",
                on_click=on_monitor_passenger_selected,
            ),
        ),
        Button(Const("◀️ Назад"), id="passenger_select_back",
               on_click=lambda c, b, m: on_dialog_back_to(c, b, m, BookingSG.monitor_profile)),
        Button(
            Const("🏠 Домой"),
            id="home_monitor_passenger_select",
            on_click=on_home_dialog,
        ),
        state=BookingSG.monitor_passenger_select,
        getter=get_monitor_passengers_data,
    ),

    Window(
        Format("{passenger_edit_text}"),
        Column(
            Select(
                Format("{item[0]}"),
                id="monitor_passenger_field_select",
                item_id_getter=lambda item: item[1],
                items="passenger_field_items",
                on_click=on_monitor_passenger_field_selected,
            ),
        ),
        Button(Const("◀️ Назад"), id="passenger_edit_back",
               on_click=lambda c, b, m: on_dialog_back_to(c, b, m, BookingSG.monitor_passenger_select)),
        Button(
            Const("🏠 Домой"),
            id="home_monitor_passenger_edit",
            on_click=on_home_dialog,
        ),
        state=BookingSG.monitor_passenger_edit,
        getter=get_monitor_passenger_edit_data,
    ),

    Window(
        Multi(Format("{monitor_document_type_text}")),
        Column(
            Select(
                Format("{item[0]}"),
                id="monitor_document_type_select",
                item_id_getter=lambda item: item[1],
                items="monitor_document_type_items",
                on_click=on_monitor_document_type_selected,
            ),
        ),
        Button(Const("◀️ Назад"), id="monitor_document_type_back",
               on_click=lambda c, b, m: m.switch_to(BookingSG.monitor_passenger_edit)),
        state=BookingSG.monitor_passenger_document_type,
        getter=get_monitor_document_type_data,
    ),

    Window(
        Const("🛏 <b>Нужно ли постельное бельё?</b>\n\nВыберите настройку для этого пассажира."),
        Row(
            Button(Const("Да, нужно"), id="monitor_bedding_yes", on_click=on_monitor_bedding_yes),
            Button(Const("Нет"), id="monitor_bedding_no", on_click=on_monitor_bedding_no),
        ),
        Button(Const("◀️ Назад"), id="monitor_bedding_back",
               on_click=lambda c, b, m: m.switch_to(BookingSG.monitor_passenger_edit)),
        state=BookingSG.monitor_passenger_bedding,
    ),

    Window(
        Multi(Format("{passenger_edit_value_text}"), Format("{monitor_input_error}")),
        MessageInput(on_monitor_passenger_edit_value),
        Button(Const("◀️ Назад"), id="passenger_edit_value_back",
               on_click=lambda c, b, m: on_dialog_back_to(c, b, m, BookingSG.monitor_passenger_edit)),
        Button(
            Const("🏠 Домой"),
            id="home_monitor_passenger_edit_value",
            on_click=on_home_dialog,
        ),
        state=BookingSG.monitor_passenger_edit_value,
        getter=get_monitor_passenger_edit_value_data,
    ),

    Window(
        Format("{monitor_history_text}"),
        Button(Const("◀️ Назад"), id="history_back",
               on_click=lambda c, b, m: on_dialog_back_to(c, b, m, BookingSG.monitor_details)),
        Button(
            Const("🏠 Домой"),
            id="home_monitor_history",
            on_click=on_home_dialog,
        ),
        state=BookingSG.monitor_history,
        getter=get_monitor_history_data,
    ),

    Window(
        Multi(
            Const(
                "💰 <b>Изменение максимальной цены</b>\n\n"
                "Введи новую максимальную стоимость одного билета в BYN.\n"
                "Например: <code>45</code> или <code>45.50</code>."
            ),
            Format("{monitor_input_error}"),
        ),
        MessageInput(on_monitor_price_input),
        Button(Const("◀️ Назад"), id="price_edit_back",
               on_click=lambda c, b, m: on_dialog_back_to(c, b, m, BookingSG.monitor_details)),
        Button(
            Const("🏠 Домой"),
            id="home_monitor_edit_price",
            on_click=on_home_dialog,
        ),
        state=BookingSG.monitor_edit_price,
        getter=get_monitor_price_data,
    ),

)
