"""Настройки диалога: модель, системный промпт, temperature, top_p, max_tokens, streaming."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import subscriptions as sub_logic
from config import config
from database import KEY_ROLE_ASR, Database
from keyboards import asr_key_choice_menu, cancel_menu, reasoning_effort_menu, settings_menu
from providers import REASONING_EFFORT_LABELS
from states import SettingsStates
from text_utils import safe_edit_reply_markup, safe_edit_text

router = Router(name="settings")


async def _settings_text(db: Database, telegram_id: int) -> str:
    s = await db.get_user_settings(telegram_id)
    active_key = await db.get_api_key(s.active_key_id) if s.active_key_id else None
    model = s.model_override or (active_key.model if active_key else None) or "не задана"
    key_name = active_key.name if active_key else "не выбран (используйте меню «Ключи»)"
    return (
        "⚙️ <b>Текущие настройки</b>\n\n"
        f"🔑 Ключ: {key_name}\n"
        f"🧠 Модель: <code>{model}</code>\n"
        f"💭 Уровень мышления: {_reasoning_label(s.reasoning_effort)}\n"
        f"📝 Системный промпт: <code>{s.system_prompt or '—'}</code>\n"
        f"🌡 Temperature: <code>{s.temperature if s.temperature is not None else config.default_temperature}</code>\n"
        f"🎯 Top-p: <code>{s.top_p if s.top_p is not None else config.default_top_p}</code>\n"
        f"📏 Max tokens: <code>{s.max_tokens if s.max_tokens is not None else config.default_max_tokens}</code>\n"
        f"🔀 Streaming: {'включен' if s.streaming else 'выключен'}\n"
        f"🤖 Режим агента: {'включен (код/файлы/ReAct)' if s.agent_mode else 'выключен (обычный чат)'}\n"
        f"🔒 Подтверждение перед выполнением кода: {_confirm_label(s.confirm_code_execution)}\n"
    )


def _confirm_label(value) -> str:
    if value is None:
        default = config.agent_confirm_code_execution_default
        return f"по умолчанию ({'включено' if default else 'выключено'})"
    return "включено" if value else "выключено"


def _reasoning_label(value: str | None) -> str:
    if value is None:
        default = config.default_reasoning_effort
        default_label = REASONING_EFFORT_LABELS.get(default, default)
        return f"по умолчанию ({default_label})"
    return REASONING_EFFORT_LABELS.get(value, value)


@router.callback_query(F.data == "settings:menu")
async def cb_settings_menu(callback: CallbackQuery, db: Database, is_admin: bool, is_subscriber: bool) -> None:
    if not is_admin and not is_subscriber:
        await callback.answer("Настройки доступны только с активной подпиской. Смотрите «💳 Подписки».", show_alert=True)
        return
    text = await _settings_text(db, callback.from_user.id)
    await safe_edit_text(callback.message, text, reply_markup=settings_menu())
    await callback.answer()


@router.callback_query(F.data == "settings:model")
async def cb_settings_model(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsStates.waiting_model_override)
    await safe_edit_text(callback.message, "Введите название модели, которую нужно использовать (переопределяет модель ключа по умолчанию).\n"
        "Отправьте «-», чтобы сбросить и использовать модель по умолчанию ключа.",
        reply_markup=cancel_menu("settings:menu"),
    )
    await callback.answer()


@router.message(SettingsStates.waiting_model_override)
async def process_model_override(message: Message, state: FSMContext, db: Database) -> None:
    text = (message.text or "").strip()
    value = None if text == "-" else text
    await db.update_user_settings(message.from_user.id, model_override=value)
    await state.clear()
    await message.answer("✅ Модель обновлена.", reply_markup=settings_menu())


@router.callback_query(F.data == "settings:reasoning")
async def cb_settings_reasoning_menu(callback: CallbackQuery) -> None:
    await safe_edit_text(callback.message, "💭 <b>Уровень мышления (reasoning)</b>\n\n"
        "Управляет тем, насколько подробно модель «размышляет» перед ответом "
        "(если модель поддерживает эту функцию — например, DeepSeek-R1, "
        "Nemotron, Qwen3 и похожие reasoning-модели). Для обычных моделей без "
        "поддержки reasoning этот параметр не будет иметь эффекта.\n\n"
        "🚫 Выключено — быстрее, без рассуждений\n"
        "🌱/🌿/🌳 Низкое/Среднее/Высокое — более подробные рассуждения, "
        "но медленнее и больше токенов в ответе.",
        reply_markup=reasoning_effort_menu(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("reasoning:set:"))
async def cb_settings_reasoning_set(callback: CallbackQuery, db: Database) -> None:
    level = callback.data.split(":")[-1]
    value = None if level == "default" else level
    await db.update_user_settings(callback.from_user.id, reasoning_effort=value)
    text = await _settings_text(db, callback.from_user.id)
    await safe_edit_text(callback.message, text, reply_markup=settings_menu())
    label = _reasoning_label(value)
    await callback.answer(f"💭 Уровень мышления: {label}", show_alert=True)


@router.callback_query(F.data == "settings:system_prompt")
async def cb_settings_system_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsStates.waiting_system_prompt)
    await safe_edit_text(callback.message, "Введите системный промпт (инструкцию для модели, задающую её поведение).\n"
        "Отправьте «-», чтобы удалить системный промпт.",
        reply_markup=cancel_menu("settings:menu"),
    )
    await callback.answer()


@router.message(SettingsStates.waiting_system_prompt)
async def process_system_prompt(message: Message, state: FSMContext, db: Database) -> None:
    text = (message.text or "").strip()
    value = None if text == "-" else text
    await db.update_user_settings(message.from_user.id, system_prompt=value)
    await state.clear()
    await message.answer("✅ Системный промпт обновлён.", reply_markup=settings_menu())


@router.callback_query(F.data == "settings:temperature")
async def cb_settings_temperature(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsStates.waiting_temperature)
    await safe_edit_text(callback.message, "Введите temperature (число от 0 до 2, например 0.7).\nОтправьте «-» для значения по умолчанию.",
        reply_markup=cancel_menu("settings:menu"),
    )
    await callback.answer()


@router.message(SettingsStates.waiting_temperature)
async def process_temperature(message: Message, state: FSMContext, db: Database) -> None:
    text = (message.text or "").strip()
    if text == "-":
        await db.update_user_settings(message.from_user.id, temperature=None)
    else:
        try:
            value = float(text.replace(",", "."))
            if not (0 <= value <= 2):
                raise ValueError
        except ValueError:
            await message.answer("❗ Нужно число от 0 до 2. Попробуйте снова или /cancel.")
            return
        await db.update_user_settings(message.from_user.id, temperature=value)
    await state.clear()
    await message.answer("✅ Temperature обновлена.", reply_markup=settings_menu())


@router.callback_query(F.data == "settings:top_p")
async def cb_settings_top_p(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsStates.waiting_top_p)
    await safe_edit_text(callback.message, "Введите top_p (число от 0 до 1, например 0.95).\nОтправьте «-» для значения по умолчанию.",
        reply_markup=cancel_menu("settings:menu"),
    )
    await callback.answer()


@router.message(SettingsStates.waiting_top_p)
async def process_top_p(message: Message, state: FSMContext, db: Database) -> None:
    text = (message.text or "").strip()
    if text == "-":
        await db.update_user_settings(message.from_user.id, top_p=None)
    else:
        try:
            value = float(text.replace(",", "."))
            if not (0 < value <= 1):
                raise ValueError
        except ValueError:
            await message.answer("❗ Нужно число от 0 до 1. Попробуйте снова или /cancel.")
            return
        await db.update_user_settings(message.from_user.id, top_p=value)
    await state.clear()
    await message.answer("✅ Top-p обновлён.", reply_markup=settings_menu())


@router.callback_query(F.data == "settings:max_tokens")
async def cb_settings_max_tokens(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsStates.waiting_max_tokens)
    await safe_edit_text(callback.message, "Введите max_tokens (максимальная длина ответа, целое число, например 1024).\n"
        "Отправьте «-» для значения по умолчанию.",
        reply_markup=cancel_menu("settings:menu"),
    )
    await callback.answer()


@router.message(SettingsStates.waiting_max_tokens)
async def process_max_tokens(message: Message, state: FSMContext, db: Database) -> None:
    text = (message.text or "").strip()
    if text == "-":
        await db.update_user_settings(message.from_user.id, max_tokens=None)
    else:
        if not text.isdigit() or int(text) <= 0:
            await message.answer("❗ Нужно положительное целое число. Попробуйте снова или /cancel.")
            return
        await db.update_user_settings(message.from_user.id, max_tokens=int(text))
    await state.clear()
    await message.answer("✅ Max tokens обновлён.", reply_markup=settings_menu())


@router.callback_query(F.data == "settings:streaming")
async def cb_settings_streaming_toggle(callback: CallbackQuery, db: Database) -> None:
    s = await db.get_user_settings(callback.from_user.id)
    await db.update_user_settings(callback.from_user.id, streaming=int(not s.streaming))
    text = await _settings_text(db, callback.from_user.id)
    await safe_edit_text(callback.message, text, reply_markup=settings_menu())
    await callback.answer("Streaming переключен")


@router.callback_query(F.data == "settings:agent_mode")
async def cb_settings_agent_mode_toggle(callback: CallbackQuery, db: Database) -> None:
    s = await db.get_user_settings(callback.from_user.id)
    await db.update_user_settings(callback.from_user.id, agent_mode=int(not s.agent_mode))
    text = await _settings_text(db, callback.from_user.id)
    await safe_edit_text(callback.message, text, reply_markup=settings_menu())
    if not s.agent_mode:
        await callback.answer(
            "🤖 Режим агента включен: бот может выполнять сгенерированный Python-код "
            "в песочнице и анализировать zip-архивы (ReAct-цикл).",
            show_alert=True,
        )
    else:
        await callback.answer("Режим агента выключен — обычный чат без выполнения кода.")


@router.callback_query(F.data == "settings:confirm_code")
async def cb_settings_confirm_code_toggle(callback: CallbackQuery, db: Database) -> None:
    """Переключает режим подтверждения кода по кругу: по умолчанию -> включено ->
    выключено -> снова по умолчанию. Это удобнее, чем простое вкл/выкл, так как
    позволяет явно вернуться к общесистемной настройке из config.py."""
    s = await db.get_user_settings(callback.from_user.id)
    if s.confirm_code_execution is None:
        new_value = 1
        alert = "🔒 Подтверждение кода ВКЛЮЧЕНО: перед каждым запуском кода бот будет спрашивать разрешение."
    elif s.confirm_code_execution:
        new_value = 0
        alert = "🔓 Подтверждение кода ВЫКЛЮЧЕНО: код будет выполняться сразу, без подтверждения."
    else:
        new_value = None
        alert = "↩️ Возвращено значение по умолчанию (задаётся в config.py)."
    await db.update_user_settings(callback.from_user.id, confirm_code_execution=new_value)
    text = await _settings_text(db, callback.from_user.id)
    await safe_edit_text(callback.message, text, reply_markup=settings_menu())
    await callback.answer(alert, show_alert=True)


@router.callback_query(F.data == "settings:clear_history")
async def cb_settings_clear_history(callback: CallbackQuery, db: Database) -> None:
    await db.clear_history(callback.from_user.id)
    await callback.answer("🧹 История диалога очищена", show_alert=True)


@router.callback_query(F.data == "settings:asr_key")
async def cb_settings_asr_key_menu(callback: CallbackQuery, db: Database, is_admin: bool) -> None:
    """Выбор ключа для распознавания голосовых сообщений (ASR). Показываются
    только активные ключи с ролью 'asr', реально доступные пользователю по
    подписке (для админов — все активные ASR-ключи)."""
    user_id = callback.from_user.id
    all_asr_keys = await db.list_api_keys(only_active=True, role=KEY_ROLE_ASR)

    if is_admin:
        allowed_keys = all_asr_keys
    else:
        allowed_ids = await sub_logic.get_effective_allowed_key_ids(db, user_id, is_admin=False)
        allowed_keys = [k for k in all_asr_keys if k.id in allowed_ids]

    settings = await db.get_user_settings(user_id)
    if not allowed_keys:
        await safe_edit_text(
            callback.message,
            "🎙 <b>Распознавание голосовых сообщений</b>\n\n"
            "Пока нет доступного вам ключа с ролью «ASR» (распознавание речи). "
            "Попросите владельца бота добавить и активировать такой ключ.",
            reply_markup=cancel_menu("settings:menu"),
        )
        await callback.answer()
        return

    await safe_edit_text(
        callback.message,
        "🎙 <b>Распознавание голосовых сообщений</b>\n\n"
        "Выберите ключ, который будет расшифровывать ваши голосовые сообщения "
        "в текст перед отправкой модели:",
        reply_markup=asr_key_choice_menu(allowed_keys, settings.active_asr_key_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("settings:asr_key_set:"))
async def cb_settings_asr_key_set(callback: CallbackQuery, db: Database, is_admin: bool) -> None:
    user_id = callback.from_user.id
    value = callback.data.split(":")[-1]

    if value == "none":
        await db.update_user_settings(user_id, active_asr_key_id=None)
        await callback.answer("🚫 Распознавание голосовых сообщений отключено.", show_alert=True)
        return

    key_id = int(value)
    key = await db.get_api_key(key_id)
    if not key or not key.is_active or key.role != KEY_ROLE_ASR:
        await callback.answer("Ключ недоступен (возможно, отключен или роль изменена).", show_alert=True)
        return

    if not is_admin:
        allowed_ids = await sub_logic.get_effective_allowed_key_ids(db, user_id, is_admin=False)
        if key_id not in allowed_ids:
            await callback.answer("Этот ключ не входит в ваш тариф.", show_alert=True)
            return

    await db.update_user_settings(user_id, active_asr_key_id=key_id)
    await callback.answer(f"✅ Голосовые сообщения будут распознаваться через: {key.name}", show_alert=True)
