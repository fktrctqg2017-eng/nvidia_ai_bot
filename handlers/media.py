"""Анализ изображений (фото) и расшифровка голосовых сообщений.

- 🖼 Фото, присланное напрямую в чат (не в архиве), отправляется модели как
  мультимодальный (Vision) запрос — если выбранная модель поддерживает
  изображения, она реально "увидит" фото. Подпись к фото используется как
  текст вопроса (если её нет — используется дефолтный промпт "опиши, что на
  фото").
- 🎙 Голосовое сообщение сначала расшифровывается в текст через ключ с ролью
  'asr' (см. handlers/settings.py -> «🎙 Ключ для голосовых»), после чего
  текст обрабатывается ТОЧНО ТАК ЖЕ, как обычное текстовое сообщение
  (сохраняется в истории, участвует в лимитах и т.д.) — пользователь как
  будто напечатал этот текст сам.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.types import Message

import subscriptions as sub_logic
from config import config
from database import KEY_ROLE_ASR, Database
from handlers.chat import (
    _agent_reply,
    _build_confirm_callback,
    _plain_reply,
    _stream_reply,
    build_system_messages,
    prepare_chat_context,
)
from providers import transcribe_audio
from text_utils import escape_html

router = Router(name="media")


async def _dispatch_reply(message: Message, db: Database, ctx, messages: list[dict], user_id: int) -> None:
    """Общая точка выбора способа ответа (агент/стрим/обычный) — идентична
    той, что используется в handlers/chat.py для обычного текста."""
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    if ctx.settings.agent_mode:
        await _agent_reply(
            message, db, ctx.key, ctx.model, messages, ctx.temperature, ctx.top_p,
            ctx.max_tokens, user_id, ctx.reasoning_effort,
        )
    elif ctx.settings.streaming:
        await _stream_reply(
            message, db, ctx.key, ctx.model, messages, ctx.temperature, ctx.top_p,
            ctx.max_tokens, ctx.reasoning_effort,
        )
    else:
        await _plain_reply(
            message, db, ctx.key, ctx.model, messages, ctx.temperature, ctx.top_p,
            ctx.max_tokens, ctx.reasoning_effort,
        )


# =====================================================================
# ФОТО — прямой анализ изображения (Vision), без архива
# =====================================================================


@router.message(F.photo)
async def handle_photo_message(message: Message, db: Database, is_admin: bool, is_owner: bool, is_subscriber: bool) -> None:
    user_id = message.from_user.id

    if not is_admin and not is_subscriber:
        from handlers.common import no_access_text

        user = message.from_user
        await message.answer(no_access_text(user.full_name or (user.username or ""), user.id))
        return

    ctx = await prepare_chat_context(message, db, is_admin, is_owner)
    if ctx is None:
        return

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    # Telegram присылает несколько размеров одного и того же фото — берём
    # самый большой (последний элемент списка) ради максимального качества анализа.
    photo = message.photo[-1]
    try:
        buf = await message.bot.download(photo)
        image_bytes = buf.read()
    except Exception as e:  # noqa: BLE001
        await message.answer(f"⚠️ Не удалось загрузить изображение: {escape_html(str(e))}")
        return

    import base64 as _base64

    data_uri = f"data:image/jpeg;base64,{_base64.b64encode(image_bytes).decode('ascii')}"
    caption = (message.caption or "").strip() or "Опиши, что изображено на этой картинке."

    user_content = [
        {"type": "text", "text": caption},
        {"type": "image_url", "image_url": {"url": data_uri}},
    ]

    await db.add_history_message(user_id, "user", f"[Пользователь отправил фото] {caption}")
    history = await db.get_history(user_id, config.history_limit)
    # Заменяем последнюю "текстовую" запись истории на реальный мультимодальный
    # контент — история в БД хранит только текстовую заметку (без base64),
    # чтобы не раздувать её на будущее, а для ЭТОГО конкретного запроса
    # используем настоящее изображение.
    if history and history[-1]["role"] == "user":
        history[-1] = {"role": "user", "content": user_content}

    messages = build_system_messages(ctx.settings, user_id)
    messages.extend(history)

    await _dispatch_reply(message, db, ctx, messages, user_id)


# =====================================================================
# ГОЛОСОВЫЕ СООБЩЕНИЯ — расшифровка через ASR-ключ + обычная обработка текста
# =====================================================================


@router.message(F.voice)
async def handle_voice_message(message: Message, db: Database, is_admin: bool, is_owner: bool, is_subscriber: bool) -> None:
    user_id = message.from_user.id

    if not is_admin and not is_subscriber:
        from handlers.common import no_access_text

        user = message.from_user
        await message.answer(no_access_text(user.full_name or (user.username or ""), user.id))
        return

    settings = await db.get_user_settings(user_id)
    if not settings.active_asr_key_id:
        await message.answer(
            "🎙 У вас не настроен ключ для распознавания голосовых сообщений.\n"
            "Откройте «⚙️ Настройки → 🎙 Ключ для голосовых», чтобы выбрать его."
        )
        return

    asr_key = await db.get_api_key(settings.active_asr_key_id)
    if not asr_key or not asr_key.is_active or asr_key.role != KEY_ROLE_ASR:
        await message.answer(
            "⚠️ Выбранный ключ для распознавания голоса недоступен (удалён, отключен или "
            "у него изменена роль). Выберите другой в «⚙️ Настройки → 🎙 Ключ для голосовых»."
        )
        return

    if not is_admin:
        allowed_ids = await sub_logic.get_effective_allowed_key_ids(db, user_id, is_admin=False)
        if asr_key.id not in allowed_ids:
            await message.answer("⛔ Выбранный ключ для распознавания голоса больше не входит в ваш тариф.")
            return

    status_msg = await message.answer("🎙 Распознаю голосовое сообщение…")
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    try:
        with tempfile.TemporaryDirectory(prefix="voice_") as tmp_dir:
            voice_path = Path(tmp_dir) / "voice.ogg"
            await message.bot.download(message.voice, destination=voice_path)
            transcript = await transcribe_audio(asr_key, str(voice_path), timeout=config.request_timeout)
    except Exception as e:  # noqa: BLE001
        await status_msg.edit_text(f"⚠️ Не удалось распознать голосовое сообщение: {escape_html(str(e))}")
        return

    transcript = (transcript or "").strip()
    if not transcript:
        await status_msg.edit_text("⚠️ Не удалось распознать речь в этом сообщении (пустой результат).")
        return

    await status_msg.edit_text(f"🎙 <b>Распознано:</b>\n{escape_html(transcript)}")

    # Дальше обрабатываем ТОЧНО ТАК ЖЕ, как обычное текстовое сообщение —
    # выбор ключа/модели для ЧАТА (не ASR-ключа!), лимиты, история и т.д.
    ctx = await prepare_chat_context(message, db, is_admin, is_owner)
    if ctx is None:
        return

    await db.add_history_message(user_id, "user", transcript)
    history = await db.get_history(user_id, config.history_limit)

    extra_hint = (
        "Примечание: входящее сообщение пользователя было распознано из голосового "
        "сообщения автоматической системой транскрипции — в нём возможны небольшие "
        "неточности распознавания речи."
    )
    messages = build_system_messages(ctx.settings, user_id, extra_hint=extra_hint)
    messages.extend(history)

    await _dispatch_reply(message, db, ctx, messages, user_id)
