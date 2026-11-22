
import logging
import os
import re
from datetime import date, datetime, time
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation
from typing import Any

from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)

from app.config import Settings
from app.utils import route_url

logger = logging.getLogger(__name__)


def is_departure_time_passed(
    travel_date: str,
    departure_text: str | None,
    *,
    now: datetime | None = None,
    timezone_name: str | None = None,
) -> bool:
    """Проверяет отправление по локальному времени станции/расписания."""
    if not departure_text:
        return False

    match = re.search(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)", str(departure_text))
    if not match:
        logger.warning(
            "Не удалось извлечь время отправления из значения %r",
            departure_text,
        )
        return False

    zone_name = timezone_name or os.getenv("TRAIN_TIMEZONE", "Europe/Minsk")
    try:
        tz = ZoneInfo(zone_name)
        departure_date = date.fromisoformat(str(travel_date).strip())
        departure_time = time(int(match.group(1)), int(match.group(2)))
        departure = datetime.combine(departure_date, departure_time, tzinfo=tz)
        current = now or datetime.now(tz)
        if current.tzinfo is None:
            current = current.replace(tzinfo=tz)
        else:
            current = current.astimezone(tz)
        return current >= departure
    except (ValueError, TypeError, KeyError) as exc:
        logger.warning(
            "Не удалось проверить время отправления: date=%r time=%r timezone=%s error=%s",
            travel_date, departure_text, zone_name, exc,
        )
        return False



SEAT_TYPES = {
    "плацкарт": "Плацкарт",
    "купе": "Купе",
    "купей": "Купе",
    "сидяч": "Сидячий",
    "общ": "Общий",
    "люкс": "Люкс",
    "мягк": "Мягкий",
}


class ScheduleUnavailable(RuntimeError):
    """Сайт не удалось загрузить или его структура не распознана."""


def normalize_seat_type(name: str) -> str | None:
    """Определяет категорию места по названию на сайте."""
    normalized = name.casefold().strip()

    for key, value in SEAT_TYPES.items():
        if key in normalized:
            return value

    return None


def parse_price(value: str | None) -> Decimal | None:
    """Преобразует цену сайта в Decimal."""
    if not value:
        return None

    normalized = value.strip().replace(",", ".")

    try:
        price = Decimal(normalized)
    except InvalidOperation:
        return None

    if not price.is_finite() or price <= 0:
        return None

    return price


async def parse_train_row(row: Any, travel_date: str) -> dict | None:
    # Номер поезда
    number_element = row.locator(".train-number").first

    if await number_element.count() == 0:
        return None

    number = (await number_element.text_content() or "").strip()

    if not number:
        return None

    # Время отправления
    dep_element = row.locator(".train-from-time").first
    dep_time = (
        (await dep_element.text_content() or "").strip()
        if await dep_element.count()
        else "--:--"
    )

    # Время прибытия
    arr_element = row.locator(".train-to-time").first
    arr_time = (
        (await arr_element.text_content() or "").strip()
        if await arr_element.count()
        else "--:--"
    )

    # Проверка отправления в часовом поясе расписания (по умолчанию Вильнюс).
    departed = is_departure_time_passed(travel_date, dep_time)
    logger.debug(
        "Проверка времени отправления: train=%s date=%s time=%s departed=%s",
        number, travel_date, dep_time, departed,
    )

    # Информация о местах и стоимости
    seat_details: dict[str, int] = {}
    raw_details: dict[str, int] = {}
    raw_seats = 0
    fare_options: list[dict] = []

    seat_items = row.locator(".sch-table__t-item")
    item_count = await seat_items.count()

    current_seat_type = None
    current_car_type = None

    for index in range(item_count):
        item = seat_items.nth(index)

        name_locator = item.locator(".sch-table__t-name").first
        quantity_locator = item.locator(".sch-table__t-quant").first
        price_locator = item.locator(".js-price").first

        if await quantity_locator.count() == 0:
            continue

        name = ""

        if await name_locator.count():
            name = (
                await name_locator.text_content() or ""
            ).strip()

        car_type = await quantity_locator.get_attribute(
            "data-car-type"
        )

        if name:
            detected_type = normalize_seat_type(name)

            if detected_type is not None:
                current_seat_type = detected_type
                current_car_type = car_type
            else:
                current_seat_type = None
                current_car_type = None

        elif car_type == current_car_type:
            # У некоторых дополнительных категорий сайта
            # название пустое, но car_type совпадает.
            # Сохраняем категорию предыдущей строки.
            pass
        else:
            current_seat_type = None
            current_car_type = None

        if current_seat_type is None:
            logger.warning(
                "Неизвестная категория мест: поезд=%s, car_type=%s",
                number,
                car_type,
            )
            continue

        # Количество свободных мест
        span_locator = quantity_locator.locator("span").first

        if await span_locator.count():
            quantity_text = await span_locator.text_content() or ""
        else:
            quantity_text = (
                await quantity_locator.text_content() or ""
            )

        match = re.search(r"\d+", quantity_text)

        if not match:
            continue

        count = int(match.group())

        if count <= 0:
            continue

        # Сохраняем фактическое количество обнаруженных мест
        # независимо от того, удалось ли получить цену.
        raw_seats += count
        raw_details[current_seat_type] = (
            raw_details.get(current_seat_type, 0) + count
        )

        # Цена в BYN из атрибута data-cost-byn
        price = None

        if await price_locator.count():
            price = parse_price(
                await price_locator.get_attribute("data-cost-byn")
            )

            # Запасной вариант — текст цены на странице.
            if price is None:
                cost_locator = price_locator.locator(
                    ".ticket-cost"
                ).first

                if await cost_locator.count():
                    price_text = (
                        await cost_locator.text_content() or ""
                    )
                    price = parse_price(price_text)

        if price is None:
            logger.warning(
                "Не удалось получить цену: поезд=%s, категория=%s",
                number,
                current_seat_type,
            )
            continue

        fare_options.append(
            {
                "name": current_seat_type,
                "seats": count,
                "price": float(price),
                "car_type": car_type,
            }
        )

        seat_details[current_seat_type] = (
            seat_details.get(current_seat_type, 0) + count
        )

    total_seats = sum(seat_details.values())

    # Минимальная цена среди найденных вариантов
    prices = [
        option["price"]
        for option in fare_options
        if option.get("price") is not None
    ]

    min_price = min(prices) if prices else None

    return {
        "number": number,
        "time": dep_time,
        "arr_time": arr_time,
        "details": seat_details,
        "seats": total_seats,
        "raw_details": raw_details,
        "raw_seats": raw_seats,
        "fare_options": fare_options,
        "min_price": min_price,
        "seats_count_is_estimate": False,
        "departed": departed,
    }


def filter_train_by_max_price(
    train: dict,
    max_price: Decimal | str | float,
) -> dict:
    """
    Оставляет только категории мест, цена которых
    не превышает максимальную стоимость билета.
    """
    limit = Decimal(str(max_price))

    suitable_options = []

    for option in train.get("fare_options", []):
        price = parse_price(str(option.get("price", "")))

        if price is None:
            continue

        if price <= limit:
            suitable_options.append(option)

    filtered_details: dict[str, int] = {}

    for option in suitable_options:
        name = option["name"]
        count = int(option["seats"])

        filtered_details[name] = (
            filtered_details.get(name, 0) + count
        )

    result = dict(train)
    result["fare_options"] = suitable_options
    result["details"] = filtered_details
    result["seats"] = sum(filtered_details.values())

    prices = [
        option["price"]
        for option in suitable_options
    ]

    result["min_price"] = min(prices) if prices else None

    return result


class RWParser:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def _read_schedule(
        self,
        page: Any,
        url: str,
        travel_date: str,
        max_price: Decimal | str | float | None = None,
    ) -> list[dict]:

        try:
            await page.goto(
                url,
                timeout=self.settings.page_timeout_ms,
                wait_until="domcontentloaded",
            )

            if self.settings.page_wait_ms:
                await page.wait_for_timeout(
                    self.settings.page_wait_ms
                )

            try:
                await page.wait_for_selector(
                    ".sch-table__row-wrap",
                    timeout=self.settings.page_timeout_ms,
                    state="attached",
                )
            except PlaywrightTimeoutError:
                pass

            rows = page.locator(".sch-table__row-wrap")
            row_count = await rows.count()

        except PlaywrightTimeoutError as exc:
            raise ScheduleUnavailable(
                "Истекло время ожидания ответа сайта БЖД"
            ) from exc

        except Exception as exc:
            raise ScheduleUnavailable(
                f"Не удалось открыть расписание: {exc}"
            ) from exc

        if row_count == 0:
            try:
                page_text = (
                    await page.locator("body").inner_text()
                ).casefold()
            except Exception:
                page_text = ""

            no_trains_phrases = (
                "не найден",
                "нет поездов",
                "поезда не найдены",
                "рейсы не найдены",
            )

            if any(
                phrase in page_text
                for phrase in no_trains_phrases
            ):
                return []

            raise ScheduleUnavailable(
                "Не найдены строки расписания. "
                "Возможно, сайт изменил разметку "
                "или расписание не загрузилось."
            )

        trains = []

        for index in range(row_count):
            row = rows.nth(index)

            try:
                train = await parse_train_row(
                    row,
                    travel_date,
                )

                if not train:
                    continue

                train["index"] = index + 1

                # Фильтрация по максимальной цене
                if max_price is not None:
                    train = filter_train_by_max_price(
                        train,
                        max_price,
                    )

                    # Если места есть, но ни один тариф
                    # не подходит по цене, поезд исключаем.
                    # Если мест действительно нет, оставляем
                    # поезд для возможности включить мониторинг.
                    if (
                        not train["fare_options"]
                        and train.get("raw_seats", 0) > 0
                    ):
                        logger.info(
                            "Поезд %s исключён: есть места, "
                            "но нет тарифов в пределах цены %s BYN",
                            train["number"],
                            max_price,
                        )
                        continue

                trains.append(train)

            except Exception:
                logger.exception(
                    "Ошибка обработки строки расписания №%s",
                    index + 1,
                )

        logger.info(
            "Расписание обработано: найдено подходящих поездов %s",
            len(trains),
        )

        return trains

    async def search_trains(
        self,
        from_name: str,
        to_name: str,
        travel_date: str,
        max_price: Decimal | str | float | None = None,
    ) -> list[dict]:

        url = route_url(
            from_name,
            to_name,
            travel_date,
        )

        async with async_playwright() as playwright:
            browser = None

            try:
                browser = await playwright.chromium.launch(
                    headless=self.settings.headless,
                )

                page = await browser.new_page(
                    locale="ru-RU",
                )

                return await self._read_schedule(
                    page,
                    url,
                    travel_date,
                    max_price=max_price,
                )

            except ScheduleUnavailable:
                raise

            except Exception as exc:
                raise ScheduleUnavailable(
                    "Ошибка запуска браузера или чтения "
                    f"расписания: {exc}"
                ) from exc

            finally:
                if browser:
                    await browser.close()

    async def read_train(
        self,
        page: Any,
        url: str,
        travel_date: str,
        train_number: str,
        max_price: Decimal | str | float | None = None,
    ) -> dict | None:

        trains = await self._read_schedule(
            page,
            url,
            travel_date,
            max_price=max_price,
        )

        return next(
            (
                train
                for train in trains
                if train["number"] == train_number
            ),
            None,
        )


    async def open_monitor_browser(self):
        playwright = await async_playwright().start()
        browser = None

        try:
            browser = await playwright.chromium.launch(
                headless=self.settings.headless,
            )

            page = await browser.new_page(
                locale="ru-RU",
            )

            return playwright, browser, page

        except BaseException:
            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    logger.warning(
                        "Не удалось закрыть браузер "
                        "при ошибке его создания",
                        exc_info=True,
                    )

            try:
                await playwright.stop()
            except Exception:
                logger.warning(
                    "Не удалось остановить Playwright "
                    "при ошибке создания браузера",
                    exc_info=True,
                )

            raise
