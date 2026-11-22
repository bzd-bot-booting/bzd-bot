from datetime import date, datetime
from html import escape
from urllib.parse import urlencode

BASE_URL = "https://pass.rw.by/ru/route/"


def validate_travel_date(value: str) -> date:
    try:
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise ValueError("Введите дату в формате ГГГГ-ММ-ДД, например 2026-12-01.") from exc
    if parsed < date.today():
        raise ValueError("Дата поездки не может быть в прошлом.")
    return parsed


def format_date(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return value


def route_url(from_name: str, to_name: str, travel_date: str) -> str:
    return BASE_URL + "?" + urlencode({"from": from_name, "to": to_name, "date": travel_date})


def safe(value: str) -> str:
    return escape(str(value), quote=False)


def monitor_key(from_name: str, to_name: str, travel_date: str, train_num: str) -> str:
    # Один и тот же номер поезда на разных маршрутах — разные мониторинги.
    return "|".join((from_name.casefold(), to_name.casefold(), travel_date, train_num.casefold()))
