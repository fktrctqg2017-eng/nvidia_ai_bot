"""Общие текстовые утилиты: экранирование HTML и разбивка длинных сообщений
по лимиту Telegram (4096 символов на сообщение).

Почему это важно и вынесено отдельно:
    Бот использует ParseMode.HTML по умолчанию для всех сообщений (см. main.py),
    что удобно для собственной разметки бота (<b>, <code> и т.д.). Но ответы
    модели, содержимое файлов из архивов и сгенерированный код почти всегда
    содержат символы `<`, `>`, `&` (операторы сравнения, generics, HTML/XML-код
    и т.п.) — без экранирования Telegram вернёт ошибку парсинга и сообщение
    вообще не будет отправлено. Поэтому весь "сырой" текст (не наша разметка)
    должен экранироваться этой функцией перед отправкой.
"""

from __future__ import annotations

import html

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, Message

# Лимит Telegram на одно сообщение — 4096 символов; оставляем запас на
# HTML-сущности (& < >, которые после экранирования становятся длиннее)
# и служебные символы вроде курсора при стриминге.
SAFE_LIMIT = 3500


def escape_html(text: str) -> str:
    """Экранирует HTML-спецсимволы для безопасной отправки как "сырого" текста
    внутри HTML-разметки Telegram (используется с parse_mode=HTML)."""
    return html.escape(text, quote=False)


async def safe_edit_text(
    message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None, **kwargs
) -> None:
    """Безопасная замена `message.edit_text(...)`.

    Telegram возвращает ошибку `Bad Request: message is not modified`, если
    пытаться заменить сообщение АБСОЛЮТНО ИДЕНТИЧНЫМ текстом/клавиатурой —
    это регулярно происходит на практике (пользователь повторно открывает
    то же самое меню, дважды нажимает кнопку, возвращается назад и снова
    вперёд в то же состояние и т.п.). Без этой обёртки такая ошибка
    "всплывает" необработанным исключением прямо из хендлера, из-за чего
    кнопка выглядит "зависшей"/нерабочей, хотя логика на самом деле
    отработала верно. Здесь эта конкретная ошибка тихо игнорируется —
    все остальные ошибки Telegram (например, "message to edit not found")
    пробрасываются дальше как есть, чтобы не скрывать реальные проблемы.
    """
    try:
        await message.edit_text(text, reply_markup=reply_markup, **kwargs)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


async def safe_edit_reply_markup(message: Message, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    """Аналог `safe_edit_text`, но только для замены инлайн-клавиатуры без
    изменения текста (см. `safe_edit_text` — та же защита от
    `message is not modified`)."""
    try:
        await message.edit_reply_markup(reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


async def send_long_text(message: Message, text: str, prefix: str = "") -> None:
    """Отправляет текст, автоматически разбивая его на несколько сообщений,
    если он превышает лимит Telegram. Текст должен быть уже готов к отправке
    (экранирован, если это "сырой" контент)."""
    if prefix:
        text = prefix + text
    if not text:
        text = "(пустой ответ)"
    while text:
        chunk = text[:SAFE_LIMIT]
        if len(text) > SAFE_LIMIT:
            cut_at = chunk.rfind("\n")
            if cut_at > 0:
                chunk = chunk[:cut_at]
        await message.answer(chunk)
        text = text[len(chunk):].lstrip("\n")

