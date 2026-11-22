"""
Отдельный видимый браузер для автоматического бронирования.

Браузер мониторинга работает независимо.
Конфиденциальные данные не записываются в логи.
"""

import re

from pathlib import Path

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Playwright,
)

logger = logging.getLogger(__name__)


@dataclass
class BookingBrowserSession:
    job_id: str
    playwright: Playwright
    browser: Browser
    context: BrowserContext
    page: Page


class BookingBrowserService:
    """Управляет отдельными браузерными сессиями бронирования."""

    def __init__(
        self,
        *,
        page_timeout_ms: int = 90_000,
        slow_mo: int = 300,
        headless: bool = True,
    ):
        self.page_timeout_ms = page_timeout_ms
        self.slow_mo = slow_mo
        self.headless = headless
        self.sessions: dict[str, BookingBrowserSession] = {}
        self._lock = asyncio.Lock()

    async def open_session(
        self,
        job_id: str,
        url: str,
    ) -> Page:
        """Открывает видимый Chromium и очищает ресурсы при ошибке."""

        async with self._lock:
            existing = self.sessions.get(job_id)

            if existing:
                if not existing.page.is_closed():
                    return existing.page

                await self._close_session_unlocked(job_id)

            playwright = await async_playwright().start()
            browser = None
            context = None
            session = None

            try:
                browser = await playwright.chromium.launch(
                    headless=self.headless,
                    slow_mo=self.slow_mo,
                )

                context = await browser.new_context(
                    ignore_https_errors=False,
                )
                context.set_default_timeout(self.page_timeout_ms)

                page = await context.new_page()

                session = BookingBrowserSession(
                    job_id=job_id,
                    playwright=playwright,
                    browser=browser,
                    context=context,
                    page=page,
                )
                self.sessions[job_id] = session

                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                )
                await self._accept_cookies(page)
                await self._close_consultant(page)

                return page

            except BaseException:
                logger.exception(
                    "Не удалось открыть браузер бронирования"
                )

                if self.sessions.get(job_id) is session:
                    self.sessions.pop(job_id, None)

                if context is not None:
                    try:
                        await context.close()
                    except Exception:
                        logger.warning(
                            "Не удалось закрыть контекст после ошибки"
                        )

                if browser is not None:
                    try:
                        await browser.close()
                    except Exception:
                        logger.warning(
                            "Не удалось закрыть Chromium после ошибки"
                        )

                try:
                    await playwright.stop()
                except Exception:
                    logger.warning(
                        "Не удалось остановить Playwright после ошибки"
                    )

                raise

    async def open_auth_form(self, job_id: str, url: str) -> Page:
        """Открывает сайт, дважды перезагружает страницу и открывает авторизацию."""

        page = await self.open_session(job_id, url)

        for reload_number in range(1, 3):
            await page.reload(
                wait_until="load",
                timeout=self.page_timeout_ms,
            )
            logger.info(
                "Страница pass.rw.by перезагружена: %s/2",
                reload_number,
            )

        auth_link = page.locator("a[data-target='#auth-popup']")
        await auth_link.wait_for(
            state="visible",
            timeout=self.page_timeout_ms,
        )
        await auth_link.click()

        await page.locator("#auth-popup").wait_for(
            state="visible",
            timeout=self.page_timeout_ms,
        )

        return page

    async def _click_anyway_button(self, page) -> bool:
        """Нажимает предупреждение «Все равно открыть»."""

        buttons = page.locator(
            "button, a, input[type='button'], "
            "input[type='submit'], [role='button']"
        )

        for attempt in range(5):
            try:
                for index in range(await buttons.count()):
                    button = buttons.nth(index)

                    try:
                        if not await button.is_visible():
                            continue
                        if await button.is_disabled():
                            continue

                        text = await button.evaluate("""
                            el => (
                                el.innerText ||
                                el.value ||
                                el.getAttribute('aria-label') ||
                                el.getAttribute('title') ||
                                ''
                            ).trim()
                        """)

                        normalized = " ".join(
                            text.lower().replace("ё", "е").split()
                        )

                        if "все равно открыть" not in normalized:
                            continue

                        logger.info(
                            "Найдено предупреждение: %s", text
                        )

                        await button.scroll_into_view_if_needed()

                        try:
                            await button.click(timeout=5000)
                        except Exception:
                            if not await button.is_visible():
                                continue
                            await button.click(
                                timeout=5000,
                                force=True,
                            )

                        await page.wait_for_timeout(1000)
                        logger.info(
                            "Нажата кнопка «Все равно открыть»"
                        )
                        return True

                    except Exception:
                        continue

            except Exception:
                logger.exception(
                    "Ошибка поиска кнопки «Все равно открыть»"
                )

            if attempt < 4:
                await page.wait_for_timeout(500)

        return False

    async def submit_credentials(
        self,
        job_id: str,
        login: str,
        password: str,
    ) -> str | None:
        """Заполняет и отправляет форму входа без сохранения пароля."""

        page = await self.get_page(job_id)

        if page is None:
            raise RuntimeError("Браузерная сессия не найдена.")

        popup = page.locator("#auth-popup")

        await popup.locator("input[name='login']").fill(login)
        await popup.locator("input[name='password']").fill(password)

        selectors = [
            "#dologin",
            "button[type='submit']",
            "input[type='submit']",
        ]

        submit_button = None

        for selector in selectors:
            candidate = popup.locator(selector)

            if await candidate.count() > 0:
                visible_candidate = candidate.first

                if (
                    await visible_candidate.is_visible()
                    and not await visible_candidate.is_disabled()
                ):
                    submit_button = visible_candidate
                    break

        if submit_button is None:
            raise RuntimeError(
                "Не найдена доступная кнопка отправки формы авторизации."
            )

        await submit_button.click()

        success_selector = page.locator(
            "button:has-text('ВЫБРАТЬ МЕСТА'), "
            "a:has-text('ВЫБРАТЬ МЕСТА')"
        ).first

        for _ in range(120):
            try:
                page_text = await page.locator("body").inner_text(
                    timeout=3000
                )

                if "Пользователь не найден" in page_text:
                    return "user_not_found"

                if "Неверное имя пользователя или пароль" in page_text:
                    return "invalid_credentials"

                warning_clicked = await self._click_anyway_button(page)

                if warning_clicked:
                    await page.wait_for_timeout(1000)
                    continue

                if await success_selector.is_visible():
                    return "authorized"

            except Exception:
                pass

            await asyncio.sleep(0.5)

        return "auth_timeout"


    async def open_selected_train_places(
        self,
        job_id: str,
        train_data: dict[str, Any],
    ) -> None:
        """Находит поезд по содержимому строки и открывает выбор мест.

        Наличие поезда и наличие кнопки выбора мест проверяются раздельно:
        сайт может показать поезд в расписании с пометкой «мест нет».
        В таком случае задача безопасно возвращается в ожидание инвентаря.
        """
        page = await self.get_page(job_id)
        if page is None:
            raise RuntimeError("Браузерная сессия не найдена.")

        if "/order/places/" in page.url:
            return

        def norm(value: Any) -> str:
            return " ".join(str(value or "").replace("\xa0", " ").split()).casefold()

        def norm_time(value: Any) -> str:
            raw = norm(value)
            match = re.search(r"(?<!\d)(\d{1,2})[:.](\d{2})(?!\d)", raw)
            return f"{int(match.group(1)):02d}:{match.group(2)}" if match else raw

        expected_number = norm(train_data.get("number"))
        expected_time = norm_time(train_data.get("time"))
        expected_arrival = norm_time(train_data.get("arr_time"))
        if not expected_number or not expected_time:
            raise RuntimeError(
                "В данных автозаказа отсутствует номер поезда или время отправления."
            )

        rows = page.locator(".sch-table__row-wrap")
        try:
            await rows.first.wait_for(state="attached", timeout=20000)
        except Exception as exc:
            body = ""
            try:
                body = (await page.locator("body").inner_text(timeout=3000))[:1200]
            except Exception:
                pass
            logger.error(
                "Расписание поездов не появилось: job_id=%s url=%s body=%r",
                job_id, page.url.split("?", 1)[0], body,
            )
            raise RuntimeError(
                "После авторизации не загрузился список поездов. "
                "Автобронирование остановлено до выбора места."
            ) from exc

        # Сначала определяем поезд только по полному тексту строки. Не полагаемся
        # на .train-number/.train-from-time: разметка сайта может содержать
        # пустые/скрытые элементы, хотя нужные значения видны пользователю.
        matched_rows = []
        row_count = await rows.count()
        number_pattern = re.compile(
            rf"(?<![0-9a-zа-яё]){re.escape(expected_number)}(?![0-9a-zа-яё])",
            re.IGNORECASE,
        )
        for index in range(row_count):
            row = rows.nth(index)
            try:
                row_text = norm(await row.inner_text(timeout=3000))
            except Exception:
                continue

            if not number_pattern.search(row_text):
                continue

            row_times = [
                f"{int(hour):02d}:{minute}"
                for hour, minute in re.findall(
                    r"(?<!\d)(\d{1,2})[:.](\d{2})(?!\d)", row_text
                )
            ]
            if expected_time not in row_times:
                continue
            if expected_arrival and expected_arrival not in row_times:
                continue
            matched_rows.append((index, row, row_text))

        if len(matched_rows) != 1:
            diagnostic = []
            for index in range(min(row_count, 20)):
                try:
                    diagnostic.append(
                        f"{index}: {norm(await rows.nth(index).inner_text(timeout=1000))[:180]}"
                    )
                except Exception:
                    continue
            logger.error(
                "Не удалось однозначно сопоставить поезд по тексту строки: "
                "job_id=%s number=%r departure=%r arrival=%r matches=%s rows=%s",
                job_id, expected_number, expected_time, expected_arrival,
                len(matched_rows), diagnostic,
            )
            raise RuntimeError(
                "Не удалось однозначно найти поезд в актуальном расписании "
                "по номеру и времени отправления/прибытия. "
                "Кнопка выбора мест не нажималась."
            )

        row_index, selected_row, row_text = matched_rows[0]
        button = selected_row.locator(
            "button:has-text('ВЫБРАТЬ МЕСТА'), "
            "a:has-text('ВЫБРАТЬ МЕСТА'), "
            "[role='button']:has-text('ВЫБРАТЬ МЕСТА')"
        ).first

        if "мест нет" in row_text or await button.count() == 0:
            logger.info(
                "Поезд найден, но свободных мест/кнопки выбора сейчас нет: "
                "job_id=%s row=%s number=%s departure=%s arrival=%s row_text=%r",
                job_id, row_index, expected_number, expected_time,
                expected_arrival, row_text[:300],
            )
            raise RuntimeError(
                "Поезд найден, но pass.rw.by сейчас показывает «мест нет» "
                "или не предоставляет кнопку выбора мест. Задача останется "
                "в ожидании следующей проверки наличия мест."
            )

        if not await button.is_visible() or await button.is_disabled():
            raise RuntimeError(
                "Поезд найден, но кнопка выбора мест пока недоступна. "
                "Задача будет повторена при следующей проверке."
            )

        await button.scroll_into_view_if_needed()
        logger.info(
            "Выбран поезд в актуальном расписании: job_id=%s row=%s "
            "number=%s departure=%s arrival=%s",
            job_id, row_index, expected_number, expected_time, expected_arrival,
        )
        await button.click(timeout=15000)

        try:
            await page.wait_for_url(
                "**/order/places/**", wait_until="domcontentloaded", timeout=30000
            )
        except Exception as exc:
            if "/order/places/" not in page.url:
                raise RuntimeError(
                    "Сайт не открыл страницу выбора мест после нажатия кнопки "
                    "выбранного поезда. Повторное оформление не выполнялось."
                ) from exc

        await page.locator(".pl-type__list a.pl-type__item").first.wait_for(
            state="visible", timeout=30000
        )
        await page.wait_for_timeout(800)

    async def reopen_train_list(
        self,
        job_id: str,
        url: str,
    ) -> None:
        """Возвращает браузер на страницу списка поездов."""
        page = await self.get_page(job_id)

        if page is None:
            raise RuntimeError("Браузерная сессия не найдена.")

        safe_url = url.split("?", 1)[0]
        logger.info(
            "Возврат на список поездов: job_id=%s url=%s",
            job_id,
            safe_url,
        )

        last_error: Exception | None = None

        for attempt in range(1, 4):
            try:
                await page.goto(
                    url,
                    wait_until="load",
                    timeout=self.page_timeout_ms,
                )
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "goto списка поездов не удался "
                    "(попытка %s/3): %s",
                    attempt,
                    type(exc).__name__,
                )
                continue

            await self._accept_cookies(page)
            await self._close_consultant(page)

            try:
                if await page.locator("#auth-popup").is_visible():
                    raise RuntimeError(
                        "Сайт требует повторный вход "
                        "(сессия сброшена). Начните /book заново."
                    )
            except RuntimeError:
                raise
            except Exception:
                pass

            try:
                await page.locator(
                    "button:has-text('ВЫБРАТЬ МЕСТА'), "
                    "a:has-text('ВЫБРАТЬ МЕСТА')"
                ).first.wait_for(
                    state="visible",
                    timeout=20000,
                )

                logger.info(
                    "Список поездов открыт (попытка %s). URL: %s",
                    attempt,
                    page.url.split("?", 1)[0],
                )

                await page.wait_for_timeout(800)
                return

            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Кнопки поездов не появились "
                    "(попытка %s/3). URL: %s",
                    attempt,
                    page.url.split("?", 1)[0],
                )

        current_url = page.url.split("?", 1)[0]
        raise RuntimeError(
            "Не удалось вернуться к списку поездов. "
            f"Последняя ошибка: {type(last_error).__name__}. "
            f"Текущий URL: {current_url}"
        )

    async def switch_job(
        self,
        new_job_id: str,
        old_job_id: str,
        url: str,
    ) -> None:
        """Переключает существующую браузерную сессию на новый job_id.

        Контекст (cookies, авторизация) сохраняется.
        """
        async with self._lock:
            old_session = self.sessions.pop(old_job_id, None)

            if old_session is None:
                raise RuntimeError(
                    f"Браузерная сессия для {old_job_id} не найдена."
                )

            existing = self.sessions.pop(new_job_id, None)
            if existing is not None and existing is not old_session:
                try:
                    await existing.context.close()
                except Exception:
                    pass
                try:
                    await existing.browser.close()
                except Exception:
                    pass
                try:
                    await existing.playwright.stop()
                except Exception:
                    pass

            old_session.job_id = new_job_id
            self.sessions[new_job_id] = old_session

        page = await self.get_page(new_job_id)

        if page is None:
            raise RuntimeError("Страница бронирования недоступна.")

        await page.goto(
            url,
            wait_until="load",
            timeout=self.page_timeout_ms,
        )
        await self._accept_cookies(page)
        await self._close_consultant(page)

        try:
            if await page.locator("#auth-popup").is_visible():
                raise RuntimeError(
                    "Сайт требует повторный вход "
                    "(сессия сброшена). Начните /book заново."
                )
        except RuntimeError:
            raise
        except Exception:
            pass

        await page.locator(
            "button:has-text('ВЫБРАТЬ МЕСТА'), "
            "a:has-text('ВЫБРАТЬ МЕСТА')"
        ).first.wait_for(
            state="visible",
            timeout=20000,
        )

        await page.wait_for_timeout(800)


    async def get_available_car_types(
        self,
        job_id: str,
    ) -> list[dict[str, str]]:
        """Получает доступные типы вагонов непосредственно со страницы."""

        page = await self.get_page(job_id)

        if page is None:
            raise RuntimeError("Браузерная сессия не найдена.")

        items = page.locator(".pl-type__list a.pl-type__item")

        await items.first.wait_for(
            state="visible",
            timeout=15000,
        )

        available_types = []

        for index in range(await items.count()):
            item = items.nth(index)

            if not await item.is_visible():
                continue

            name_locator = item.locator(".pl-type__name")

            if await name_locator.count() == 0:
                continue

            name = (await name_locator.first.inner_text()).strip()
            car_type = await item.get_attribute("data-car-type")

            if not name or not car_type:
                continue

            seats_locator = item.locator(".pl-count__total-seats")
            seats = ""

            if await seats_locator.count():
                seats = (await seats_locator.first.inner_text()).strip()

            available_types.append({
                "name": name,
                "car_type": car_type,
                "seats": seats,
            })

        if not available_types:
            raise RuntimeError(
                "На странице не найдены доступные типы вагонов."
            )

        return available_types

    async def select_car_type(
        self,
        job_id: str,
        car_type: str,
    ) -> dict[str, str]:
        """Выбирает тип вагона (логика как в рабочем terminal-скрипте)."""

        page = await self.get_page(job_id)

        if page is None:
            raise RuntimeError("Браузерная сессия не найдена.")

        items = page.locator(".pl-type__list a.pl-type__item")
        await items.first.wait_for(state="visible", timeout=15000)

        selected_item = None
        selected_data = None

        for index in range(await items.count()):
            item = items.nth(index)

            if not await item.is_visible():
                continue

            if await item.get_attribute("data-car-type") == car_type:
                selected_item = item

                name_locator = item.locator(".pl-type__name")
                seats_locator = item.locator(".pl-count__total-seats")

                name = (
                    (await name_locator.first.inner_text()).strip()
                    if await name_locator.count()
                    else ""
                )
                seats = (
                    (await seats_locator.first.inner_text()).strip()
                    if await seats_locator.count()
                    else ""
                )

                selected_data = {
                    "name": name,
                    "car_type": car_type,
                    "seats": seats,
                }
                break

        if selected_item is None or selected_data is None:
            raise RuntimeError(
                "Выбранный тип вагона больше не доступен на странице."
            )

        class_name = await selected_item.get_attribute("class") or ""

        if "active" not in class_name.split():
            await selected_item.scroll_into_view_if_needed()
            try:
                await selected_item.evaluate("(el) => el.click()")
            except Exception:
                try:
                    await selected_item.click(timeout=5000, force=True)
                except Exception:
                    logger.warning("Не удалось кликнуть тип вагона")

            try:
                await page.wait_for_function(
                    """(carType) => {
                        const el = document.querySelector(
                            `.pl-type__item[data-car-type="${carType}"]`
                        );
                        return el && el.classList.contains("active");
                    }""",
                    arg=car_type,
                    timeout=10000,
                )
            except Exception:
                logger.warning(
                    "Тип вагона %s: active не подтверждён за 10с",
                    car_type,
                )

        try:
            await page.locator(
                ".pl-accord__acc-link[data-target^='carriage-collapse-item-'], "
                ".carriage__seat"
            ).first.wait_for(state="visible", timeout=10000)
        except Exception:
            logger.warning(
                "После выбора типа вагона схема вагонов не появилась сразу"
            )

        await page.wait_for_timeout(1000)
        return selected_data


    @staticmethod
    def _seat_matches_position(seat: dict, requested_position: str) -> bool:
        """Проверяет положение места по классам, полученным с сайта."""

        if requested_position == "Любое":
            return True

        classes = set(seat.get("classes", []))
        is_side = "side" in classes
        is_bottom = "bottom" in classes
        is_top = "top" in classes

        if requested_position == "Нижнее":
            return is_bottom and not is_side

        if requested_position == "Верхнее":
            return is_top and not is_side

        if requested_position == "Нижнее боковое":
            return is_bottom and is_side

        if requested_position == "Верхнее боковое":
            return is_top and is_side

        return False

    async def _open_carriage(
        self,
        page: Page,
        panel_id: str,
    ):
        """Открывает аккордеон вагона."""

        panel_id = str(panel_id).lstrip("#")

        header = page.locator(
            f'.pl-accord__acc-link[data-target="{panel_id}"]'
        ).first

        body = page.locator(f"#{panel_id} .pl-accord__acc-body").first

        if await header.count() == 0:
            header = page.locator(
                f'.pl-accord__acc-link[data-target="#{panel_id}"]'
            ).first

        if await header.count() == 0:
            raise RuntimeError(f"Не найден заголовок вагона: {panel_id}")

        if await body.count() == 0:
            raise RuntimeError(f"Не найден блок мест вагона: {panel_id}")

        if not await body.is_visible():
            await header.scroll_into_view_if_needed()
            try:
                await header.click(timeout=10000)
            except Exception:
                await header.evaluate("(el) => el.click()")
            await body.wait_for(state="visible", timeout=15000)

        try:
            await body.locator(".carriage__seat").first.wait_for(
                state="attached",
                timeout=8000,
            )
        except Exception as exc:
            logger.exception(
                "Не удалось загрузить схему мест вагона %s",
                panel_id,
            )
            raise RuntimeError(
                f"Схема мест вагона {panel_id} не загрузилась"
            ) from exc

        await page.wait_for_timeout(600)

        return header, body

    async def scan_free_seats(
        self,
        job_id: str,
        requested_position: str = "Любое",
    ) -> list[dict[str, Any]]:
        """Сканирует все вагоны выбранного типа."""

        page = await self.get_page(job_id)

        if page is None:
            raise RuntimeError("Браузерная сессия не найдена.")

        allowed_positions = {
            "Любое",
            "Нижнее",
            "Верхнее",
            "Нижнее боковое",
            "Верхнее боковое",
        }

        if requested_position not in allowed_positions:
            raise ValueError("Неизвестное положение места.")

        headers = page.locator(
            ".pl-accord__acc-link[data-target^='carriage-collapse-item-']"
        )

        await headers.first.wait_for(
            state="visible",
            timeout=15000,
        )

        screenshot_dir = (
            Path("data")
            / "booking_screenshots"
            / re.sub(r"[^a-zA-Z0-9_-]", "_", job_id)
        )
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        screenshot_dir = screenshot_dir.resolve()

        results = []

        for index in range(await headers.count()):
            header = headers.nth(index)

            if not await header.is_visible():
                continue

            panel_id = await header.get_attribute("data-target")
            if not panel_id:
                continue

            panel_id = panel_id.lstrip("#")
            title = " ".join(
                (await header.inner_text()).split()
            )

            _, body = await self._open_carriage(page, panel_id)

            seat_elements = body.locator(".carriage__seat")
            free_seats = []

            for seat_index in range(await seat_elements.count()):
                element = seat_elements.nth(seat_index)

                classes = (
                    await element.get_attribute("class") or ""
                ).split()

                if "free" not in classes:
                    continue

                seat_number = await element.get_attribute(
                    "data-carriage-number"
                )

                if not seat_number:
                    continue

                seat = {
                    "number": str(seat_number),
                    "classes": classes,
                }

                if self._seat_matches_position(
                    seat,
                    requested_position,
                ):
                    free_seats.append(seat)

            if not free_seats:
                continue

            number_match = re.search(
                r"(?:вагон\s*№?\s*)?(\d+)",
                title,
                flags=re.IGNORECASE,
            )
            carriage_number = (
                number_match.group(1)
                if number_match
                else title
            )

            screenshot_path = screenshot_dir / (
                f"carriage_{index + 1}.png"
            )

            await body.screenshot(
                path=str(screenshot_path),
                animations="disabled",
            )

            results.append({
                "panel_id": panel_id,
                "title": title,
                "carriage_number": carriage_number,
                "seats": free_seats,
                "screenshot_path": str(screenshot_path),
            })

        return results

    async def click_selected_seat(
        self,
        job_id: str,
        panel_id: str,
        seat_number: str,
        requested_position: str = "Любое",
    ) -> dict[str, str]:
        """Повторно проверяет место на сайте и выбирает его."""

        page = await self.get_page(job_id)

        if page is None:
            raise RuntimeError("Браузерная сессия не найдена.")

        header, body = await self._open_carriage(page, panel_id)

        seat = body.locator(
            f'.carriage__seat[data-carriage-number="{seat_number}"]'
        )

        seat_count = await seat.count()
        if seat_count != 1:
            raise RuntimeError(
                "Выбранное место должно встречаться ровно один раз. "
                f"Найдено элементов: {seat_count}."
            )

        classes = (
            await seat.get_attribute("class") or ""
        ).split()

        if "free" not in classes:
            raise RuntimeError(
                "Выбранное место уже недоступно. Обновите список мест."
            )

        seat_data = {
            "number": str(seat_number),
            "classes": classes,
        }

        if not self._seat_matches_position(
            seat_data,
            requested_position,
        ):
            raise RuntimeError(
                "Выбранное место не соответствует заданному положению."
            )

        await seat.scroll_into_view_if_needed()
        await seat.click(timeout=10000)

        selected = False
        for _ in range(20):
            await page.wait_for_timeout(250)
            classes_now = (await seat.get_attribute("class") or "").split()
            if "active" in classes_now:
                selected = True
                break

        if not selected:
            try:
                await page.screenshot(
                    path="seat_selection_error.png",
                    full_page=True,
                )
            except Exception:
                pass
            raise RuntimeError(
                "Сайт не подтвердил выбор места. "
                "Скриншот: seat_selection_error.png"
            )

        header_text = " ".join((await header.inner_text()).split())
        number_match = re.search(
            r"(?:вагон\s*№?\s*)?(\d+)",
            header_text,
            flags=re.IGNORECASE,
        )
        carriage_number = (
            number_match.group(1) if number_match else ""
        )

        return {
            "carriage": header_text,
            "carriage_number": carriage_number,
            "seat_number": str(seat_number),
        }


    async def return_to_seat_selection(self, job_id: str) -> bool:
        """Возвращает страницу сайта с формы пассажира к схеме вагонов."""
        page = await self.get_page(job_id)
        if page is None:
            raise RuntimeError("Браузерная сессия не найдена.")

        seat_page_selector = (
            ".pl-type__list a.pl-type__item, "
            ".pl-accord__acc-link[data-target^='carriage-collapse-item-']"
        )
        seat_page = page.locator(seat_page_selector).first
        try:
            if await seat_page.is_visible():
                return True
        except Exception:
            pass

        try:
            await page.go_back(wait_until="domcontentloaded", timeout=15000)
        except Exception as exc:
            logger.warning("История браузера не вернула страницу выбора места: %s", type(exc).__name__)

        await page.locator(seat_page_selector).first.wait_for(
            state="visible",
            timeout=15000,
        )
        return True

    async def open_passenger_data_form(self, page):
        """Открывает форму пассажира после выбора места."""

        # В некоторых вариантах сайта после выбора места форма пассажира
        # открывается автоматически. Не нажимаем «Продолжить» повторно.
        passenger_form = page.locator('select[name="document_type_1"]')
        try:
            if await passenger_form.is_visible():
                return True
        except Exception:
            pass

        candidates = [
            ".carriage-cost.active .carriage-cost__btn-wrap .btn",
            ".carriage-cost.active button",
            ".carriage-cost__btn-wrap .btn",
            "button:has-text('Продолжить')",
            "button:has-text('Далее')",
            ".btn:has-text('Продолжить')",
        ]

        button = None
        for selector in candidates:
            locator = page.locator(selector)
            try:
                if await locator.count() and await locator.first.is_visible():
                    button = locator.first
                    break
            except Exception:
                continue

        if button is None:
            raise RuntimeError(
                "Не найдена кнопка перехода к данным пассажира."
            )

        await button.wait_for(state="visible", timeout=15000)

        for _ in range(30):
            try:
                if not await button.is_disabled():
                    break
            except Exception:
                pass
            await page.wait_for_timeout(500)
        else:
            raise RuntimeError(
                "Кнопка перехода к данным пассажира заблокирована."
            )

        await button.scroll_into_view_if_needed()
        try:
            await button.click(timeout=10000)
        except Exception:
            await button.evaluate("(el) => el.click()")

        await page.wait_for_timeout(1500)

        await page.locator('select[name="document_type_1"]').wait_for(
            state="visible",
            timeout=20000,
        )
        return True

    async def get_document_options_from_active_sessions(self, passenger_index=1):
        """Ищет уже открытую форму пассажира в активных сессиях автозаказа.

        Метод ничего не нажимает на сайте и не выбирает поезд/место. Он только
        читает список, если фоновое бронирование уже дошло до формы пассажира.
        """
        selector_text = f'select[name="document_type_{int(passenger_index)}"]'
        for job_id, session in list(self.sessions.items()):
            page = session.page
            try:
                if page.is_closed():
                    continue
                selector = page.locator(selector_text)
                if not await selector.is_visible(timeout=1000):
                    continue
                options = await self.get_document_options(
                    page, passenger_index=passenger_index
                )
                if options:
                    logger.info(
                        "Получен список типов документов из активной сессии автозаказа"
                    )
                    return options
            except Exception:
                logger.debug(
                    "В активной сессии %s форма пассажира ещё недоступна",
                    job_id,
                    exc_info=True,
                )
        return []

    async def get_document_options(self, page, passenger_index=1):
        """Возвращает реальные доступные на сайте типы документов."""
        selector = page.locator(
            f'select[name="document_type_{int(passenger_index)}"]'
        )
        await selector.wait_for(state="visible", timeout=15000)

        options = await selector.locator("option").evaluate_all("""
            elements => elements.map(option => ({
                value: option.value.trim(),
                text: option.textContent.trim()
            })).filter(option => option.value && option.text)
        """)

        return options

    async def select_document_type(
        self, page, requested_document, passenger_index=1
    ):
        """Выбирает тип документа только из реально доступных вариантов."""
        options = await self.get_document_options(page, passenger_index)

        requested = str(requested_document or "").strip().casefold()
        if not requested:
            raise ValueError("Не указан тип документа.")

        exact = [
            item for item in options
            if item["text"].strip().casefold() == requested
        ]

        matches = exact or [
            item for item in options
            if requested in item["text"].strip().casefold()
        ]

        if len(matches) != 1:
            available = [item["text"] for item in options]
            raise ValueError(
                "Не удалось однозначно выбрать тип документа. "
                f"Доступные варианты: {available}"
            )

        await page.locator(
            f'select[name="document_type_{int(passenger_index)}"]'
        ).select_option(value=matches[0]["value"])

        return matches[0]["text"]

    async def fill_passenger_data(
        self, page, passenger, passenger_index=1
    ):
        """Заполняет данные пассажира без вывода персональных данных."""
        passenger_index = int(passenger_index)

        required_fields = {
            "last_name": f'input[name="last_name_{passenger_index}"]',
            "first_name": f'input[name="first_name_{passenger_index}"]',
            "document_number": f'input[name="document_number_{passenger_index}"]',
        }

        missing = [
            key for key in required_fields
            if not str(passenger.get(key, "")).strip()
        ]
        if missing:
            raise ValueError(
                "Не заполнены обязательные поля пассажира: "
                + ", ".join(missing)
            )

        for key, selector in required_fields.items():
            locator = page.locator(selector)
            await locator.wait_for(state="visible", timeout=10000)
            await locator.fill(str(passenger[key]).strip())

        middle_name = str(
            passenger.get("middle_name", "")
        ).strip()

        # На pass.rw.by имя поля отчества может отличаться в зависимости
        # от типа документа/версии формы. Ищем его по name, id и подписи.
        # Персональные значения в логи не записываются.
        middle_locator = None
        middle_selectors = (
            f'input[name="middle_name_{passenger_index}"]',
            f'input[name="patronymic_{passenger_index}"]',
            f'input[name="second_name_{passenger_index}"]',
            f'input[name="third_name_{passenger_index}"]',
            f'input[name="otchestvo_{passenger_index}"]',
            'input[name*="middle_name" i]',
            'input[name*="patronym" i]',
            'input[name*="second_name" i]',
            'input[name*="third_name" i]',
            'input[name*="otchestv" i]',
            'input[id*="middle_name" i]',
            'input[id*="patronym" i]',
            'input[id*="second_name" i]',
            'input[id*="third_name" i]',
            'input[id*="otchestv" i]',
        )

        for selector in middle_selectors:
            candidates = page.locator(selector)
            try:
                for candidate_index in range(await candidates.count()):
                    candidate = candidates.nth(candidate_index)
                    if await candidate.is_visible() and await candidate.is_enabled():
                        middle_locator = candidate
                        break
            except Exception:
                continue
            if middle_locator is not None:
                break

        if middle_locator is None:
            # Резервный поиск по реальной подписи label/aria-label.
            field_index = await page.evaluate("""() => {
                const fields = Array.from(document.querySelectorAll(
                    'input:not([type="hidden"]), textarea'
                ));
                const pattern = /(отчеств|patronym|middle.?name|father.?name|second.?name|third.?name|otchestv)/i;
                for (let index = 0; index < fields.length; index++) {
                    const field = fields[index];
                    const labels = field.labels
                        ? Array.from(field.labels).map(label => label.innerText || label.textContent || '')
                        : [];
                    const metadata = [
                        field.name,
                        field.id,
                        field.placeholder,
                        field.getAttribute('aria-label'),
                        ...labels,
                    ].join(' ');
                    const style = window.getComputedStyle(field);
                    const visible = style.display !== 'none'
                        && style.visibility !== 'hidden'
                        && field.getClientRects().length > 0;
                    if (visible && pattern.test(metadata)) return index;
                }
                return -1;
            }""")
            if field_index >= 0:
                middle_locator = page.locator(
                    'input:not([type="hidden"]), textarea'
                ).nth(field_index)

        if middle_locator is not None:
            if middle_name:
                await middle_locator.fill(middle_name)
                actual_middle_name = (
                    await middle_locator.input_value()
                ).strip()
                if actual_middle_name != middle_name:
                    raise RuntimeError(
                        "Сайт не сохранил введённое отчество пассажира."
                    )
            else:
                # Если отчество не указано, очищаем поле, если оно есть.
                await middle_locator.fill("")
        elif middle_name:
            raise RuntimeError(
                "В форме pass.rw.by не найдено поле для отчества. "
                "Отправка с пропущенным отчеством заблокирована."
            )

        logger.info(
            "Поле отчества пассажира %s: %s",
            passenger_index,
            "заполнено" if middle_name and middle_locator is not None
            else "пустое/не требуется",
        )
        return True

    async def set_bed_linen(self, page, enabled):
        """Устанавливает постельное бельё через визуальный jq-checkbox."""

        if page.is_closed():
            raise RuntimeError(
                "Невозможно изменить постельное бельё: страница закрыта."
            )

        selector = 'input[name="sel_bedding_agree"]'
        checkbox = page.locator(selector)

        count = await checkbox.count()

        if count == 0:
            return {
                "available": False,
                "checked": False,
            }

        if count != 1:
            raise RuntimeError(
                f"Найдено неверное количество чекбоксов постельного белья: {count}"
            )

        await checkbox.wait_for(
            state="attached",
            timeout=10000,
        )

        current = await checkbox.is_checked()

        if current == enabled:
            return {
                "available": True,
                "checked": current,
            }

        wrapper = checkbox.locator(
            "xpath=ancestor::div[contains(@class, 'jq-checkbox')][1]"
        )

        label = checkbox.locator(
            "xpath=ancestor::label[1]"
        )

        if await wrapper.count() != 1:
            raise RuntimeError(
                "Не найден визуальный элемент jq-checkbox "
                "для постельного белья."
            )

        await wrapper.scroll_into_view_if_needed()

        try:
            await wrapper.click(timeout=5000)

        except Exception:
            if page.is_closed():
                raise RuntimeError(
                    "Страница закрылась во время выбора постельного белья."
                )

            current = await checkbox.is_checked()

            if current != enabled and await label.count() == 1:
                await label.click(timeout=5000)
            elif current != enabled:
                raise

        await page.wait_for_timeout(500)

        if page.is_closed():
            raise RuntimeError(
                "Страница закрылась после нажатия чекбокса постельного белья."
            )

        checked = await checkbox.is_checked()

        if checked != enabled:
            checked = await checkbox.is_checked()

            if checked != enabled and await label.count() == 1:
                await label.click(timeout=5000)
                await page.wait_for_timeout(500)
                checked = await checkbox.is_checked()

        if checked != enabled:
            try:
                await page.screenshot(
                    path="bedding_checkbox_error.png",
                    full_page=True,
                )
            except Exception:
                pass

            raise RuntimeError(
                "Сайт не подтвердил изменение состояния "
                "чекбокса постельного белья."
            )

        return {
            "available": True,
            "checked": checked,
        }

    async def validate_passenger_form(self, page, passenger_index=1):
        """Проверяет заполнение формы, не возвращая значения персональных полей."""
        errors = []

        passenger_index = int(passenger_index)

        selectors = {
            "Фамилия": f'input[name="last_name_{passenger_index}"]',
            "Имя": f'input[name="first_name_{passenger_index}"]',
            "Номер документа": f'input[name="document_number_{passenger_index}"]',
        }

        for label, selector in selectors.items():
            locator = page.locator(selector)

            if not await locator.count():
                errors.append(f"Не найдено поле «{label}».")
                continue

            if not await locator.is_visible():
                errors.append(f"Поле «{label}» недоступно.")
                continue

            value = await locator.input_value()
            if not value.strip():
                errors.append(f"Не заполнено поле «{label}».")

        document_selector = page.locator(
            f'select[name="document_type_{passenger_index}"]'
        )

        if not await document_selector.count():
            errors.append("Не найден выбор типа документа.")
        else:
            selected_value = await document_selector.input_value()
            if not selected_value.strip():
                errors.append("Не выбран тип документа.")

        return {
            "valid": not errors,
            "errors": errors,
        }

    async def prepare_passenger_form(
        self,
        page,
        passenger,
        document_type,
        bedding=False,
        passenger_index=1,
    ):
        """Подготавливает форму пассажира, но не отправляет заказ."""
        await self.select_document_type(
            page, document_type, passenger_index
        )
        await self.fill_passenger_data(
            page, passenger, passenger_index
        )

        bedding_result = await self.set_bed_linen(page, bool(bedding))
        if bedding_result.get("available") and bool(bedding_result.get("checked")) != bool(bedding):
            raise RuntimeError(
                "Сайт не подтвердил выбранную настройку постельного белья."
            )

        validation = await self.validate_passenger_form(
            page, passenger_index
        )

        return {
            "valid": validation["valid"],
            "errors": validation["errors"],
            "bedding": bedding_result,
            "ready_for_confirmation": validation["valid"],
            "submitted": False,
        }


    async def prepare_order_submission(
        self,
        page,
        passenger_count: int,
    ) -> dict:
        """Проверяет заполнение формы и готовность к отправке."""
        passenger_count = int(passenger_count)
        if passenger_count < 1 or passenger_count > 10:
            raise ValueError("Некорректное количество пассажиров.")

        errors = []
        for index in range(1, passenger_count + 1):
            result = await self.validate_passenger_form(page, index)
            errors.extend(
                f"Пассажир №{index}: {error}"
                for error in result.get("errors", [])
            )

        if errors:
            raise RuntimeError(
                "Форма пассажиров не прошла проверку: "
                + "; ".join(errors)
            )

        if page.is_closed():
            raise RuntimeError(
                "Страница закрыта перед проверкой согласия с правилами."
            )

        agreement = page.locator('input[name="agreement"]')

        if await agreement.count() != 1:
            raise RuntimeError(
                "Не найден единственный чекбокс согласия с правилами."
            )

        await agreement.wait_for(state="attached", timeout=10000)

        if not await agreement.is_checked():
            wrapper = agreement.locator(
                "xpath=ancestor::div[contains(@class, 'jq-checkbox')][1]"
            )
            label = agreement.locator(
                "xpath=ancestor::label[1]"
            )

            if await wrapper.count() != 1:
                raise RuntimeError(
                    "Не найден визуальный jq-checkbox согласия с правилами."
                )

            await wrapper.scroll_into_view_if_needed()

            try:
                await wrapper.click(timeout=5000)

            except Exception:
                if page.is_closed():
                    raise RuntimeError(
                        "Страница закрылась при установке согласия с правилами."
                    )

                if (
                    not await agreement.is_checked()
                    and await label.count() == 1
                ):
                    await label.click(timeout=5000)
                elif not await agreement.is_checked():
                    raise

            await page.wait_for_timeout(500)

            checked = await agreement.is_checked()

            if not checked and await label.count() == 1:
                checked = await agreement.is_checked()

                if not checked:
                    await label.click(timeout=5000)
                    await page.wait_for_timeout(500)
                    checked = await agreement.is_checked()

            if not checked:
                try:
                    await page.evaluate(
                        """() => {
                            const input = document.querySelector('input[name="agreement"]');
                            if (!input) return false;
                            input.checked = true;
                            input.dispatchEvent(new Event('input', { bubbles: true }));
                            input.dispatchEvent(new Event('change', { bubbles: true }));
                            const jq = input.closest('.jq-checkbox');
                            if (jq) jq.classList.add('checked');
                            return Boolean(input.checked);
                        }"""
                    )
                    await page.wait_for_timeout(500)
                    checked = await agreement.is_checked()
                except Exception:
                    checked = False

            if not checked:
                try:
                    await page.screenshot(
                        path="agreement_checkbox_error.png",
                        full_page=True,
                    )
                except Exception:
                    pass

                raise RuntimeError(
                    "Сайт не подтвердил согласие с правилами."
                )

        if not await agreement.is_checked():
            try:
                await page.evaluate(
                    """() => {
                        const input = document.querySelector('input[name="agreement"]');
                        if (!input) return false;
                        input.checked = true;
                        input.dispatchEvent(new Event('input', { bubbles: true }));
                        input.dispatchEvent(new Event('change', { bubbles: true }));
                        const jq = input.closest('.jq-checkbox');
                        if (jq) jq.classList.add('checked');
                        return Boolean(input.checked);
                    }"""
                )
                await page.wait_for_timeout(500)
            except Exception:
                pass

            if not await agreement.is_checked():
                raise RuntimeError(
                    "Согласие с правилами не установлено."
                )

        submit = page.locator("button.info-form__submit")
        await submit.wait_for(state="visible", timeout=15000)

        for _ in range(30):
            if await submit.is_enabled():
                break
            await page.wait_for_timeout(500)

        if not await submit.is_enabled():
            raise RuntimeError(
                "Кнопка оформления заказа остаётся заблокированной."
            )

        return {
            "ready": True,
            "passenger_count": passenger_count,
            "agreement_checked": True,
        }


    async def click_order_submit_once(
        self,
        page,
        *,
        expected_train_number: str | None = None,
        expected_travel_date: str | None = None,
    ) -> dict:
        """Однократно отправляет заказ и проверяет ответ сайта."""
        submit = page.locator("button.info-form__submit")
        count = await submit.count()

        if count != 1:
            raise RuntimeError(
                f"Ожидалась одна кнопка отправки, найдено: {count}"
            )

        if not await submit.is_visible() or not await submit.is_enabled():
            raise RuntimeError("Кнопка отправки недоступна.")

        # Сначала вызывающий код обязан зафиксировать статус submitting в БД.
        previous_url = page.url
        await submit.evaluate("(button) => button.click()")

        try:
            await page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass

        # Сайт может подтверждать заказ асинхронно, без полной перезагрузки.
        # Проверяем ответ несколько раз, но никогда не нажимаем кнопку повторно.
        last_result = {
            "status": "unknown",
            "reason": "site_response_timeout",
            "url": page.url,
        }
        for attempt in range(25):
            last_result = await self.inspect_order_result(
                page,
                expected_train_number=expected_train_number,
                expected_travel_date=expected_travel_date,
                previous_url=previous_url,
            )
            if last_result.get("status") != "unknown":
                return last_result
            if attempt < 24:
                await page.wait_for_timeout(1000)

        return last_result


    async def detect_captcha(self, page: Page) -> bool:
        """Ищет признаки CAPTCHA. При её наличии автоматизация останавливается."""
        selectors = (
            'iframe[src*="captcha"]',
            'iframe[title*="captcha" i]',
            '.g-recaptcha',
            '.h-captcha',
            '[data-sitekey]',
            'input[name*="captcha" i]',
        )
        for selector in selectors:
            try:
                locator = page.locator(selector)
                if await locator.count() and await locator.first.is_visible():
                    return True
            except Exception:
                continue

        try:
            body = (await page.locator("body").inner_text(timeout=3000)).casefold()
        except Exception:
            return False

        return any(
            phrase in body
            for phrase in (
                "подтвердите, что вы не робот",
                "проверка на робота",
                "введите символы с картинки",
                "captcha",
                "капча",
            )
        )

    @staticmethod
    def _extract_order_number(body_text: str) -> str | None:
        patterns = (
            r"(?:номер\s+заказа|заказ\s*№|заказ\s*#)\s*(?:[:№#]\s*){0,2}([A-ZА-Я0-9-]{4,})",
            r"(?:order\s*(?:number|no\.?|#))\s*(?:[:№#]\s*){0,2}([A-Z0-9-]{4,})",
        )
        for pattern in patterns:
            match = re.search(pattern, body_text, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip(" .,:;#№")
        return None

    @staticmethod
    def _find_order_number_for_trip(
        body_text: str,
        train_number: str,
        travel_date: str,
    ) -> str | None:
        """Ищет номер заказа в локальном текстовом блоке с поездом и датой."""
        number = str(train_number or "").strip().casefold()
        raw_date = str(travel_date or "").strip()
        if not number or not raw_date:
            return None

        date_tokens = {raw_date.casefold()}
        try:
            parsed = datetime.strptime(raw_date, "%Y-%m-%d")
            date_tokens.add(parsed.strftime("%d.%m.%Y").casefold())
            date_tokens.add(parsed.strftime("%d/%m/%Y").casefold())
        except ValueError:
            pass

        lines = [re.sub(r"\s+", " ", line).strip() for line in body_text.splitlines()]
        for index, line in enumerate(lines):
            if not re.search(rf"(?<!\d){re.escape(number)}(?!\d)", line.casefold()):
                continue
            context = " ".join(lines[max(0, index - 8): min(len(lines), index + 9)])
            if not any(token and token in context.casefold() for token in date_tokens):
                continue
            order_number = BookingBrowserService._extract_order_number(context)
            if order_number:
                return order_number
        return None

    @staticmethod
    def _extract_payment_expiry(body_text: str) -> datetime | None:
        """Извлекает только явный относительный срок оплаты, не угадывая его."""
        match = re.search(
            r"(?:в течение|within)\s+(\d{1,3})\s*(?:минут|мин\.?|minutes?)",
            body_text,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        minutes = int(match.group(1))
        if minutes < 1 or minutes > 240:
            return None
        return datetime.now(timezone.utc) + timedelta(minutes=minutes)

    async def inspect_order_result(
        self,
        page: Page,
        *,
        expected_train_number: str | None = None,
        expected_travel_date: str | None = None,
        previous_url: str | None = None,
    ) -> dict[str, Any]:
        """Подтверждает заказ по явному ответу сайта и переходу к оплате."""
        if page.is_closed():
            return {"status": "unknown", "reason": "page_closed"}

        if await self.detect_captcha(page):
            return {"status": "captcha", "reason": "captcha_detected"}

        try:
            body_text = await page.locator("body").inner_text(timeout=5000)
        except Exception:
            return {"status": "unknown", "reason": "page_text_unavailable"}

        body = re.sub(r"\s+", " ", body_text).strip()
        order_number = self._extract_order_number(body)
        expiry = self._extract_payment_expiry(body)
        url = page.url.split("?", 1)[0].split("#", 1)[0]
        lowered = body.casefold()

        success_markers = (
            "заказ успешно оформлен",
            "заказ успешно создан",
            "заказ успешно забронирован",
            "ваш заказ оформлен",
            "ваш заказ принят",
            "бронирование успешно оформлено",
            "билет успешно забронирован",
            "заказ сформирован",
            "order successfully created",
            "order has been placed",
        )
        failure_markers = (
            "не удалось оформить заказ",
            "ошибка оформления заказа",
            "заказ не создан",
            "unable to place order",
            "order was not created",
        )

        has_success_text = any(item in lowered for item in success_markers)
        is_payment_page = "/order/payment" in url.casefold()
        previous_clean_url = (
            str(previous_url or "").split("?", 1)[0].split("#", 1)[0]
        )
        payment_url_transition = bool(
            is_payment_page
            and previous_clean_url
            and previous_clean_url != url
            and any(
                marker in lowered
                for marker in (
                    "оплат",
                    "сумм",
                    "payment",
                )
            )
        )
        if (
            has_success_text
            or payment_url_transition
            or (
                is_payment_page
                and bool(order_number)
            )
        ):
            return {
                "status": "confirmed",
                "order_number": order_number,
                "order_expires_at": expiry,
                "payment_deadline_found": expiry is not None,
                "url": url,
            }

        if any(item in lowered for item in failure_markers):
            return {
                "status": "rejected",
                "reason": "site_rejected_order",
                "url": url,
            }

        return {
            "status": "unknown",
            "reason": "no_unambiguous_site_confirmation",
            "order_number": order_number,
            "url": url,
        }

    async def inspect_order_history(
        self,
        page: Page,
        *,
        train_number: str,
        travel_date: str,
    ) -> dict[str, Any]:
        """Ищет созданный заказ в личном кабинете; отсутствие записи не считается отказом."""
        if await self.detect_captcha(page):
            return {"status": "captcha"}

        try:
            body_text = await page.locator("body").inner_text(timeout=7000)
        except Exception:
            return {"status": "unknown", "reason": "history_unreadable"}

        order_number = self._find_order_number_for_trip(
            body_text,
            train_number,
            travel_date,
        )
        if order_number:
            return {
                "status": "confirmed",
                "order_number": order_number,
                "order_expires_at": self._extract_payment_expiry(body_text),
            }

        # Отсутствие совпадения в истории не доказывает, что заказ не был создан.
        return {"status": "unknown", "reason": "matching_order_not_proven"}

    async def is_authorized(self, job_id: str) -> bool:
        """Проверяет, появился ли интерфейс выбора мест после входа."""

        page = await self.get_page(job_id)

        if page is None:
            return False

        try:
            selector = page.locator(
                "button:has-text('ВЫБРАТЬ МЕСТА'), "
                "a:has-text('ВЫБРАТЬ МЕСТА')"
            ).first

            return await selector.is_visible(timeout=3000)
        except Exception:
            return False

    async def _accept_cookies(self, page: Page) -> None:
        """Принимает cookie, если соответствующее окно присутствует."""

        try:
            button = (
                page.locator("#cookies-popup button")
                .filter(has_text="ПРИНЯТЬ")
                .first
            )

            await button.click(timeout=4000)
        except Exception:
            pass

    async def _close_consultant(self, page: Page) -> None:
        """Скрывает мешающий интерфейсу онлайн-консультант."""

        try:
            await page.evaluate(
                """
                () => {
                    const pattern =
                        /online.?consult|consultant|jivo|live.?chat|webim|crisp|intercom|bitrix|chat.?widget|chatbox|support.?chat/i;

                    const elements = [
                        ...document.querySelectorAll(
                            'iframe, [id], [class], [data-testid], [aria-label], [title]'
                        )
                    ];

                    const matched = elements.filter(el => {
                        const className =
                            typeof el.className === 'string'
                                ? el.className
                                : '';

                        const attributes = [
                            el.id || '',
                            className,
                            el.getAttribute('src') || '',
                            el.getAttribute('title') || '',
                            el.getAttribute('aria-label') || '',
                            el.getAttribute('data-testid') || ''
                        ].join(' ');

                        if (!pattern.test(attributes)) {
                            return false;
                        }

                        const rect = el.getBoundingClientRect();

                        return rect.width > 0 && rect.height > 0;
                    });

                    const roots = matched.filter(el =>
                        !matched.some(other =>
                            other !== el && other.contains(el)
                        )
                    );

                    for (const root of roots) {
                        root.style.setProperty(
                            'display', 'none', 'important'
                        );
                        root.style.setProperty(
                            'visibility', 'hidden', 'important'
                        );
                    }
                }
                """
            )
        except Exception:
            logger.debug(
                "Не удалось скрыть онлайн-консультанта",
                exc_info=True,
            )

    async def get_page(self, job_id: str) -> Page | None:
        session = self.sessions.get(job_id)

        if not session or session.page.is_closed():
            return None

        return session.page

    async def close_session(self, job_id: str) -> None:
        async with self._lock:
            await self._close_session_unlocked(job_id)

    async def _close_session_unlocked(self, job_id: str) -> None:
        session = self.sessions.pop(job_id, None)

        if session is None:
            return

        try:
            await session.context.close()
        except Exception:
            logger.warning("Ошибка закрытия контекста браузера")

        try:
            await session.browser.close()
        except Exception:
            logger.warning("Ошибка закрытия браузера")

        try:
            await session.playwright.stop()
        except Exception:
            logger.warning("Ошибка остановки Playwright")

    async def shutdown(self) -> None:
        async with self._lock:
            for job_id in list(self.sessions):
                await self._close_session_unlocked(job_id)
