from __future__ import annotations

import logging

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import Message

from app.middlewares.message_cleanup import (
    clear_chat_messages,
    tracked_message_ids,
)

logger = logging.getLogger(__name__)


class DeletePrivateUserMessagesMiddleware(BaseMiddleware):
    """Удаляет входящий текст и старые сообщения бота при ответе новым сообщением.

    Если обработчик редактирует текущее окно aiogram-dialog, message_id не меняется
    и очистка старых сообщений бота не запускается. Если в ответ отправлено одно
    или несколько новых сообщений, старые сообщения бота удаляются, а новые
    сохраняются.
    """

    async def __call__(self, handler, event, data):
        is_private_user_message = (
            isinstance(event, Message)
            and event.chat.type == ChatType.PRIVATE
            and event.from_user is not None
            and not event.from_user.is_bot
        )

        if not is_private_user_message:
            return await handler(event, data)

        chat_id = int(event.chat.id)
        # Snapshot before processing the update. Newly sent bot messages are
        # registered by track_outgoing_messages in the bot session middleware.
        previous_bot_message_ids = tracked_message_ids(chat_id)

        try:
            return await handler(event, data)
        finally:
            # The user's text may contain personal data; remove it after handling.
            try:
                await event.delete()
            except Exception as exc:
                logger.debug(
                    "Не удалось удалить сообщение пользователя %s: %s",
                    event.from_user.id,
                    exc,
                )

            # Only clear old bot messages if this update produced a NEW message.
            # Editing the current dialog message does not add a new ID and is safe.
            current_bot_message_ids = tracked_message_ids(chat_id)
            new_bot_message_ids = current_bot_message_ids - previous_bot_message_ids
            if new_bot_message_ids:
                try:
                    deleted = await clear_chat_messages(
                        data["bot"],
                        chat_id,
                        keep_message_ids=new_bot_message_ids,
                    )
                    logger.debug(
                        "После нового ответа в чате %s удалено старых сообщений бота: %s; новых оставлено: %s",
                        chat_id,
                        deleted,
                        len(new_bot_message_ids),
                    )
                except Exception:
                    logger.exception(
                        "Не удалось очистить старые сообщения бота в чате %s",
                        chat_id,
                    )
