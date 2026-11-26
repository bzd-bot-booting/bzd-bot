from aiogram.fsm.state import State, StatesGroup


class BookingSG(StatesGroup):
    station_from = State()
    station_from_manual = State()
    station_to = State()
    station_to_manual = State()
    travel_date = State()
    travel_date_manual = State()

    # Обычный поиск билетов
    ticket_count = State()
    max_price = State()

    # Мониторинг выбранного поезда
    monitor_ticket_count = State()
    monitoring_info = State()

    searching = State()
    choose_train = State()
    train_result = State()
    # Автоматическое бронирование
    booking_login = State()
    booking_password = State()
    booking_wait_auth = State()
    booking_choose_car_type = State()
    booking_choose_seat_position = State()
    booking_choose_seat = State()
    booking_bedding = State()
    booking_passenger_last_name = State()
    booking_passenger_first_name = State()
    booking_passenger_middle_name = State()
    booking_passenger_document_type = State()
    booking_passenger_document_number = State()
    booking_passenger_confirmation = State()

    # Главное меню и раздел управления мониторингами
    main_menu = State()
    help = State()
    my_monitors = State()
    monitor_details = State()
    monitor_profile = State()
    monitor_history = State()
    monitor_edit_price = State()

    # Редактирование сохранённых данных пассажиров
    monitor_passenger_select = State()
    monitor_passenger_edit = State()
    monitor_passenger_edit_value = State()
    monitor_passenger_document_type = State()
    monitor_passenger_bedding = State()
