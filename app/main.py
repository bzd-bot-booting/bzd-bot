import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, ErrorEvent
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram_dialog import setup_dialogs
from aiogram_dialog.api.exceptions import UnknownIntent, OutdatedIntent
from sqlalchemy import text

from app.config import load_settings
from app.core.logging import configure_logging
from app.core.notifications import EmailReporter
from app.database.engine import SessionFactory, close_db
from app.handlers.booking import router as booking_router
from app.handlers.commands import router as commands_router, register_commands
from app.services.monitoring import MonitorService
from app.services.booking_browser import BookingBrowserService
from app.services.booking_jobs import AutomaticBookingWorker
from app.services.rw_parser import RWParser
from app.ui.dialog import booking_dialog
from app.middlewares.delete_user_messages import DeletePrivateUserMessagesMiddleware
from app.middlewares.message_cleanup import track_outgoing_messages


logger = logging.getLogger(__name__)


async def _heartbeat_loop() -> None:
    """Обновляет файл healthcheck, пока основной event loop работает."""
    heartbeat_path = Path(
        os.getenv("HEALTH_FILE", "/tmp/bzhd_bot_heartbeat")
    )
    interval = max(5, int(os.getenv("HEALTH_INTERVAL_SECONDS", "30")))
    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        while True:
            heartbeat_path.touch()
            await asyncio.sleep(interval)
    finally:
        try:
            heartbeat_path.unlink(missing_ok=True)
        except OSError:
            logger.exception("Не удалось удалить heartbeat-файл")


async def _notify_admins(bot: Bot, chat_ids: tuple[int, ...], message: str) -> None:
    if not chat_ids:
        logger.warning(
            "ADMIN_CHAT_IDS не задан: Telegram-уведомление о запуске/остановке не отправлено"
        )
        return
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id, message)
            logger.info(
                "Отправлено служебное уведомление администратору chat_id=%s",
                chat_id,
                extra={"event": "admin_lifecycle_notification", "chat_id": chat_id},
            )
        except Exception:
            logger.exception(
                "Не удалось отправить служебное уведомление администратору chat_id=%s",
                chat_id,
            )


def apply_migrations() -> None:
    """Применяет все миграции перед запуском бота."""

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True,
    )


async def check_database() -> None:
    """Проверяет соединение с PostgreSQL."""

    async with SessionFactory() as session:
        await session.execute(text("SELECT 1"))

    logger.info("Соединение с PostgreSQL успешно")


async def main() -> None:
    configure_logging()
    settings = load_settings()

    # Применение миграций.
    apply_migrations()

    # Проверка подключения к базе данных.
    await check_database()

    bot = Bot(
        token=settings.telegram_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    email_reporter = EmailReporter(settings)
    email_reporter.attach(asyncio.get_running_loop())
    email_report_task = asyncio.create_task(
        email_reporter.run_daily(),
        name="email-daily-reports",
    )

    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.message.outer_middleware(DeletePrivateUserMessagesMiddleware())
    # Track bot messages so /start, /book, /cancel and /stop can clean old chat output.
    bot.session.middleware.register(track_outgoing_messages)

    parser = RWParser(settings)
    monitor_service = MonitorService(bot, parser, settings)
    booking_browser_service = BookingBrowserService(
        page_timeout_ms=settings.page_timeout_ms,
        slow_mo=300,
        headless=settings.headless,
    )

    dispatcher["settings"] = settings
    dispatcher["rw_parser"] = parser
    dispatcher["monitor_service"] = monitor_service
    dispatcher["booking_browser_service"] = booking_browser_service

    automatic_booking_worker = AutomaticBookingWorker(
        bot,
        booking_browser_service,
        monitor_service,
        check_interval=settings.check_interval,
    )

    @dispatcher.errors()
    async def errors_handler(event: ErrorEvent):
        error = event.exception

        # 1. Обработка устаревших кнопок диалога (UnknownIntent / OutdatedIntent)
        if isinstance(error, (UnknownIntent, OutdatedIntent)):
            if event.update.callback_query:
                try:
                    await event.update.callback_query.answer(
                        "⏳ Сессия устарела или диалог был перезапущен.\n\n"
                        "Пожалуйста, начните заново: /book",
                        show_alert=True,
                    )
                except Exception:
                    pass
            return True  # Ошибка обработана, трейсбек не выводим

        # 2. Обработка ошибок Telegram API
        if isinstance(error, (TelegramBadRequest, TelegramForbiddenError)):
            logger.warning("Telegram API error: %s", error)
            return True

        # 3. Все остальные ошибки
        logger.error(
            "Unhandled update error: %r",
            error,
            exc_info=(type(error), error, error.__traceback__),
        )

        return False

    register_commands(commands_router)

    dispatcher.include_router(commands_router)

    booking_router.include_router(booking_dialog)
    dispatcher.include_router(booking_router)

    setup_dialogs(dispatcher)

    await bot.set_my_commands([
        BotCommand(command="start", description="Показать справку"),
        BotCommand(command="book", description="Начать поиск билетов"),
        BotCommand(command="stop", description="Остановить мои мониторинги"),
        BotCommand(command="cancel", description="Отменить текущий ввод"),
    ])

    lifecycle_started = False
    heartbeat_task = None
    try:
        heartbeat_task = asyncio.create_task(
            _heartbeat_loop(),
            name="bzhd-bot-heartbeat",
        )
        await automatic_booking_worker.start()
        # Доставляем незавершённые уведомления о ранее остановленных мониторингах.
        await monitor_service.retry_pending_finish_notifications()
        restored = await monitor_service.restore_active_monitors()

        logger.info(
            "Бот запускается. Восстановлено мониторингов: %s",
            restored,
            extra={"event": "bot_started", "restored_monitors": restored},
        )
        lifecycle_started = True
        await _notify_admins(
            bot,
            settings.admin_chat_ids,
            "🟢 <b>bzhd_bot запущен</b>\n\n"
            f"Восстановлено активных мониторингов: <b>{restored}</b>.",
        )

        await dispatcher.start_polling(
            bot,
            close_bot_session=False,
        )

    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
        if lifecycle_started:
            await _notify_admins(
                bot,
                settings.admin_chat_ids,
                "🔴 <b>bzhd_bot остановлен</b>\n\n"
                "Получен штатный сигнал завершения или выполнено штатное выключение процесса.",
            )
            logger.info(
                "Бот остановлен штатно",
                extra={"event": "bot_stopped"},
            )
        email_report_task.cancel()
        try:
            await email_report_task
        except asyncio.CancelledError:
            pass
        try:
            await automatic_booking_worker.shutdown()
        finally:
            try:
                await booking_browser_service.shutdown()
            finally:
                try:
                    await monitor_service.shutdown()
                finally:
                    try:
                        await bot.session.close()
                    finally:
                        await close_db()

        logger.info("Бот остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Получен сигнал завершения")
    except Exception:
        logger.exception("Не удалось запустить бота")
        raise
