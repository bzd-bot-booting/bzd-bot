from __future__ import annotations

import json
import logging
from pathlib import Path
from collections import defaultdict

from aiogram.types import Message
from aiogram.enums import ChatType

logger = logging.getLogger(__name__)

# In-memory registry of bot messages in private chats. The dialog's current
# message can be preserved while all older bot messages are removed.
_REGISTRY_PATH = Path("data/telegram_message_registry.json")
_CHAT_MESSAGES: dict[int, set[int]] = defaultdict(set)


def _load_registry() -> None:
    try:
        if _REGISTRY_PATH.is_file():
            raw = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
            for chat_id, message_ids in raw.items():
                _CHAT_MESSAGES[int(chat_id)].update(int(x) for x in message_ids)
    except Exception:
        logger.exception("Не удалось загрузить реестр сообщений Telegram")


def _save_registry() -> None:
    try:
        _REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {str(chat_id): sorted(ids) for chat_id, ids in _CHAT_MESSAGES.items() if ids}
        temporary = _REGISTRY_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(_REGISTRY_PATH)
    except Exception:
        logger.exception("Не удалось сохранить реестр сообщений Telegram")


_load_registry()


def track_message(chat_id: int, message_id: int) -> None:
    _CHAT_MESSAGES[int(chat_id)].add(int(message_id))
    _save_registry()


def forget_message(chat_id: int, message_id: int) -> None:
    _CHAT_MESSAGES.get(int(chat_id), set()).discard(int(message_id))
    _save_registry()


def tracked_message_ids(chat_id: int) -> set[int]:
    """Return a snapshot of bot message IDs currently tracked for this chat."""
    return set(_CHAT_MESSAGES.get(int(chat_id), set()))


async def track_outgoing_messages(make_request, bot, method):
    """Aiogram request middleware: remember messages sent by this bot."""
    result = await make_request(bot, method)
    messages: list[Message] = []
    if isinstance(result, Message):
        messages = [result]
    elif isinstance(result, (list, tuple)):
        messages = [item for item in result if isinstance(item, Message)]
    for message in messages:
        if message.chat and message.chat.type == ChatType.PRIVATE and message.message_id:
            track_message(message.chat.id, message.message_id)

    # Successful explicit deletion should also update the registry.
    method_name = getattr(method, "__api_method__", "")
    if method_name == "deleteMessage":
        chat_id = getattr(method, "chat_id", None)
        message_id = getattr(method, "message_id", None)
        if chat_id is not None and message_id is not None:
            forget_message(int(chat_id), int(message_id))
    return result


async def clear_chat_messages(
    bot,
    chat_id: int,
    keep_message_id: int | None = None,
    keep_message_ids: set[int] | None = None,
) -> int:
    """Delete tracked bot messages, optionally preserving one or more messages."""
    ids = tracked_message_ids(chat_id)
    keep_ids = {int(value) for value in (keep_message_ids or set())}
    if keep_message_id is not None:
        keep_ids.add(int(keep_message_id))

    deleted = 0
    for message_id in sorted(ids):
        if int(message_id) in keep_ids:
            continue
        try:
            await bot.delete_message(chat_id=int(chat_id), message_id=int(message_id))
            deleted += 1
        except Exception as exc:
            # Network failures remain in the registry and can be retried next time.
            logger.debug(
                "Не удалось удалить сообщение %s в чате %s: %s",
                message_id,
                chat_id,
                exc,
            )
            error_text = str(exc).lower()
            if "message to delete not found" in error_text or "message can't be deleted" in error_text:
                forget_message(int(chat_id), int(message_id))
        else:
            forget_message(int(chat_id), int(message_id))
    return deleted


def active_dialog_message_id(dialog_manager) -> int | None:
    try:
        if dialog_manager.has_context():
            value = dialog_manager.current_stack().last_message_id
            return int(value) if value else None
    except Exception:
        logger.debug("Не удалось определить message_id активного диалога", exc_info=True)
    return None


async def cleanup_after_back(callback) -> int:
    """Удаляет старые сообщения бота после нажатия «Назад».

    Текущее сообщение диалога сохраняется, чтобы aiogram-dialog мог
    отредактировать его и показать предыдущий шаг.
    """
    message = getattr(callback, "message", None)
    if message is None or not getattr(message, "chat", None):
        return 0

    chat = message.chat
    if chat.type != ChatType.PRIVATE:
        return 0

    current_message_id = getattr(message, "message_id", None)
    return await clear_chat_messages(
        callback.bot,
        int(chat.id),
        keep_message_id=int(current_message_id) if current_message_id else None,
    )
