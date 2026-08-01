"""Основной чат с моделью: обычный текст пересылается в LLM, ответ стримится.

Важно про HTML-разметку: бот использует ParseMode.HTML глобально (см. main.py),
поэтому весь "сырой" текст, пришедший от модели (или показанный пользователю
код), обязательно экранируется через text_utils.escape_html перед отправкой —
иначе символы <, >, & в коде/ответах моделей сломают парсинг сообщения на
стороне Telegram и оно вообще не будет доставлено.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import subscriptions as sub_logic
from config import config
from confirmation import confirmation_store
from database import ApiKey, Database, UserSettings
from keyboards import code_confirmation_menu, main_menu
from llm_agent import AGENT_SYSTEM_PROMPT, archive_store, run_agent_turn
from providers import reasoning_system_prompt_hint, simple_chat_completion, stream_chat_completion
from text_utils import SAFE_LIMIT, escape_html, send_long_text

router = Router(name="chat")


@dataclass
class ReadyChatContext:
    """Всё, что нужно, чтобы обратиться к модели: проверенный активный
    ключ+модель, настройки пользователя и вычисленные генерационные
    параметры. Возвращается `prepare_chat_context` — общей точкой входа,
    используемой обычным текстовым чатом, обработкой архивов/документов,
    фото и голосовых сообщений, чтобы не дублировать одни и те же проверки
    (выбран ли ключ, активен ли, есть ли доступ по подписке/лимитам,
    задана ли модель) в каждом из этих мест по отдельности."""

    key: ApiKey
    model: str
    settings: UserSettings
    temperature: float
    top_p: float
    max_tokens: int
    reasoning_effort: str


async def prepare_chat_context(
    message: Message, db: Database, is_admin: bool, is_owner: bool
) -> Optional[ReadyChatContext]:
    """Общая подготовка перед ЛЮБЫМ обращением к модели (текст, архив/документ,
    фото, голос). При любой проблеме сама отправляет пользователю понятное
    сообщение об ошибке и возвращает None — вызывающий код должен просто
    прекратить обработку в этом случае (доступ уже объяснён пользователю)."""
    user_id = message.from_user.id
    settings = await db.get_user_settings(user_id)

    hint = "«🔑 API-ключи»" if is_owner else "«🧠 Мои модели»"
    if not settings.active_key_id:
        await message.answer(f"⚠️ У вас не выбран активный API-ключ. Откройте меню {hint} и выберите ключ для общения.")
        return None

    key = await db.get_api_key(settings.active_key_id)
    if not key or not key.is_active:
        await message.answer(f"⚠️ Выбранный API-ключ недоступен (удалён или отключен). Выберите другой в меню {hint}.")
        return None

    model = settings.model_override or key.model
    if not model:
        await message.answer("⚠️ Для этого ключа не задана модель. Задайте её в «⚙️ Настройки» → «Модель».")
        return None

    # `model` передаётся ЯВНО, чтобы check_chat_access мог проверить точечное
    # ограничение конкретных моделей внутри ключа с режимом 'all_models' (см.
    # subscriptions.check_model_access) — например, если у ключа подключено
    # 5 моделей, а тариф пользователя разрешает пользоваться только одной из них.
    access = await sub_logic.check_chat_access(db, user_id, key, is_admin, model=model)
    if not access.allowed:
        await message.answer(f"⛔ {access.reason}")
        return None

    return ReadyChatContext(
        key=key,
        model=model,
        settings=settings,
        temperature=settings.temperature if settings.temperature is not None else config.default_temperature,
        top_p=settings.top_p if settings.top_p is not None else config.default_top_p,
        max_tokens=settings.max_tokens if settings.max_tokens is not None else config.default_max_tokens,
        reasoning_effort=settings.reasoning_effort or config.default_reasoning_effort,
    )


def build_system_messages(settings: UserSettings, user_id: int, extra_hint: Optional[str] = None) -> list[dict]:
    """Собирает системное сообщение из общих для всех типов запросов частей:
    промпт агента (если включён режим агента), подсказка про reasoning,
    личный системный промпт пользователя, контекст ранее загруженных архивов,
    и опционально ещё одну дополнительную подсказку (`extra_hint`) — например,
    для голосовых сообщений подсказку о том, что текст — это транскрипция."""
    system_parts = []
    if settings.agent_mode:
        system_parts.append(AGENT_SYSTEM_PROMPT)
    reasoning_hint = reasoning_system_prompt_hint(settings.reasoning_effort or config.default_reasoning_effort)
    if reasoning_hint:
        system_parts.append(reasoning_hint)
    if settings.system_prompt:
        system_parts.append(settings.system_prompt)
    hint = archive_store.context_hint(user_id)
    if hint:
        system_parts.append(hint)
    if extra_hint:
        system_parts.append(extra_hint)

    messages = []
    if system_parts:
        messages.append({"role": "system", "content": "\n\n".join(system_parts)})
    return messages


@router.message(Command("new"))
async def cmd_new_dialog(message: Message, db: Database) -> None:
    await db.clear_history(message.from_user.id)
    await message.answer("🆕 Начат новый диалог. История очищена.")


@router.callback_query(F.data == "chat:new")
async def cb_new_dialog(callback: CallbackQuery, db: Database, is_admin: bool, is_subscriber: bool) -> None:
    await db.clear_history(callback.from_user.id)
    await callback.answer("🆕 Начат новый диалог", show_alert=True)
    await callback.message.edit_text(
        "🆕 Диалог очищен. Просто напишите сообщение, чтобы начать общение с моделью.",
        reply_markup=main_menu(is_admin, is_subscriber),
    )


@router.message(F.text & ~F.text.startswith("/"))
async def handle_chat_message(message: Message, db: Database, is_admin: bool, is_owner: bool, is_subscriber: bool) -> None:
    user_id = message.from_user.id

    if not is_admin and not is_subscriber:
        from handlers.common import no_access_text

        user = message.from_user
        await message.answer(no_access_text(user.full_name or (user.username or ""), user.id))
        return

    settings = await db.get_user_settings(user_id)

    if not settings.active_key_id:
        hint = "«🔑 API-ключи»" if is_owner else "«🧠 Мои модели»"
        await message.answer(
            f"⚠️ У вас не выбран активный API-ключ. Откройте меню {hint} и выберите ключ для общения."
        )
        return

    key = await db.get_api_key(settings.active_key_id)
    if not key or not key.is_active:
        hint = "«🔑 API-ключи»" if is_owner else "«🧠 Мои модели»"
        await message.answer(
            f"⚠️ Выбранный API-ключ недоступен (удалён или отключен). Выберите другой в меню {hint}."
        )
        return

    model = settings.model_override or key.model
    if not model:
        await message.answer(
            "⚠️ Для этого ключа не задана модель. Задайте её в «⚙️ Настройки» → «Модель»."
        )
        return

    # `model` передаётся ЯВНО — нужно для проверки точечного ограничения
    # конкретных моделей внутри ключа с режимом 'all_models' (см.
    # subscriptions.check_model_access).
    access = await sub_logic.check_chat_access(db, user_id, key, is_admin, model=model)
    if not access.allowed:
        await message.answer(f"⛔ {access.reason}")
        return

    temperature = settings.temperature if settings.temperature is not None else config.default_temperature
    top_p = settings.top_p if settings.top_p is not None else config.default_top_p
    max_tokens = settings.max_tokens if settings.max_tokens is not None else config.default_max_tokens
    reasoning_effort = settings.reasoning_effort or config.default_reasoning_effort

    await db.add_history_message(user_id, "user", message.text)
    history = await db.get_history(user_id, config.history_limit)

    system_parts = []
    if settings.agent_mode:
        system_parts.append(AGENT_SYSTEM_PROMPT)
    reasoning_hint = reasoning_system_prompt_hint(reasoning_effort)
    if reasoning_hint:
        system_parts.append(reasoning_hint)
    if settings.system_prompt:
        system_parts.append(settings.system_prompt)
    hint = archive_store.context_hint(user_id)
    if hint:
        system_parts.append(hint)

    messages = []
    if system_parts:
        messages.append({"role": "system", "content": "\n\n".join(system_parts)})
    messages.extend(history)

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    if settings.agent_mode:
        # В режиме агента используем ReAct-цикл (модель может выполнить код /
        # обратиться к загруженным архивам несколько раз перед финальным ответом).
        # Стриминг здесь не применяется — промежуточные шаги видны через
        # редактируемое статусное сообщение, а показывается только финальный ответ.
        await _agent_reply(
            message, db, key, model, messages, temperature, top_p, max_tokens, user_id, reasoning_effort
        )
    elif settings.streaming:
        await _stream_reply(message, db, key, model, messages, temperature, top_p, max_tokens, reasoning_effort)
    else:
        await _plain_reply(message, db, key, model, messages, temperature, top_p, max_tokens, reasoning_effort)


async def _agent_reply(
    message, db, key, model, messages, temperature, top_p, max_tokens, user_id, reasoning_effort
):
    """Выполняет ReAct-цикл агента (см. llm_agent.py) и отправляет финальный ответ.

    Промежуточные шаги (выполнение кода, обращение к архивам) отображаются
    пользователю через редактируемое статусное сообщение, чтобы было видно,
    что бот "думает" и что именно делает — это важно, так как выполнение
    кода в песочнице может занимать несколько секунд.
    """
    status_msg = await message.answer("🤖 Агент обрабатывает запрос…")

    async def on_step(text: str) -> None:
        try:
            await status_msg.edit_text(text)
        except TelegramBadRequest:
            pass

    confirm_callback = await _build_confirm_callback(message, db, user_id)

    try:
        result = await run_agent_turn(
            key=key,
            model=model,
            messages=messages,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            timeout=config.request_timeout,
            telegram_id=user_id,
            on_step=on_step,
            confirm_code_callback=confirm_callback,
            reasoning_effort=reasoning_effort,
        )
    except Exception as e:  # noqa: BLE001
        await status_msg.edit_text(f"⚠️ Ошибка работы агента: {escape_html(str(e))}")
        return

    answer = result.final_text or "(пустой ответ от модели)"
    await db.add_history_message(user_id, "assistant", answer)

    try:
        await status_msg.delete()
    except Exception:  # noqa: BLE001
        pass

    if result.stopped_due_to_limit:
        await message.answer("⚠️ Агент достиг лимита шагов, ниже — промежуточный результат:")

    await send_long_text(message, escape_html(answer))


async def _build_confirm_callback(message: Message, db: Database, user_id: int):
    """Строит callback для run_agent_turn(confirm_code_callback=...), который
    показывает пользователю код с кнопками "Выполнить"/"Отклонить" и ждёт
    решения через confirmation_store (см. confirmation.py).

    Если у пользователя отключено подтверждение (в настройках или по
    умолчанию из config.py) — возвращает None, и код будет выполняться
    без подтверждения.
    """
    settings = await db.get_user_settings(user_id)
    require_confirmation = (
        settings.confirm_code_execution
        if settings.confirm_code_execution is not None
        else config.agent_confirm_code_execution_default
    )
    if not require_confirmation:
        return None

    async def confirm_code(code: str) -> bool:
        pending = confirmation_store.create(user_id)
        code_preview = code if len(code) <= 3500 else code[:3500] + "\n... [код обрезан для показа]"
        await message.answer(
            "🔒 <b>Модель хочет выполнить следующий код в песочнице:</b>\n\n"
            f"<pre>{escape_html(code_preview)}</pre>\n\n"
            f"⏳ У вас есть {int(config.agent_confirmation_timeout_seconds)} сек., чтобы подтвердить или отклонить "
            f"(при отсутствии ответа код НЕ будет выполнен).",
            reply_markup=code_confirmation_menu(pending.confirmation_id),
        )
        try:
            approved = await asyncio.wait_for(
                pending.future, timeout=config.agent_confirmation_timeout_seconds
            )
        except asyncio.TimeoutError:
            confirmation_store.discard(pending.confirmation_id)
            await message.answer("⏱ Время на подтверждение истекло — код не был выполнен.")
            return False
        return approved

    return confirm_code


@router.callback_query(F.data.startswith("code_confirm:"))
async def cb_code_confirmation(callback: CallbackQuery) -> None:
    """Обрабатывает нажатие кнопки "Выполнить"/"Отклонить" под сообщением с кодом.

    Находит соответствующий asyncio.Future в confirmation_store и "будит"
    ждущую его корутину ReAct-цикла с результатом True/False.
    """
    try:
        _, decision, confirmation_id = callback.data.split(":", 2)
    except ValueError:
        await callback.answer("Некорректные данные подтверждения.", show_alert=True)
        return

    approved = decision == "approve"
    resolved = confirmation_store.resolve(confirmation_id, approved, callback.from_user.id)

    if not resolved:
        await callback.answer(
            "⚠️ Этот запрос на подтверждение уже неактуален (истёк, был обработан "
            "ранее, либо относится к другому пользователю).",
            show_alert=True,
        )
        return

    status_text = "✅ Вы разрешили выполнение кода." if approved else "❌ Вы отклонили выполнение кода."
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await callback.message.answer(status_text)
    await callback.answer()


async def _plain_reply(message, db, key, model, messages, temperature, top_p, max_tokens, reasoning_effort):
    try:
        answer = await simple_chat_completion(
            key, model, messages, temperature, top_p, max_tokens, config.request_timeout,
            reasoning_effort=reasoning_effort,
        )
    except Exception as e:  # noqa: BLE001
        await message.answer(f"⚠️ Ошибка запроса к модели: {escape_html(str(e))}")
        return
    if not answer:
        answer = "(пустой ответ от модели)"
    await db.add_history_message(message.from_user.id, "assistant", answer)
    await send_long_text(message, escape_html(answer))


async def _stream_reply(message, db, key, model, messages, temperature, top_p, max_tokens, reasoning_effort):
    placeholder = await message.answer("⌛ Генерирую ответ…")
    buffer = ""
    sent_len = 0  # сколько символов buffer уже "заморожено" в предыдущих сообщениях
    last_edit = 0.0
    error_text = None

    async for chunk in stream_chat_completion(
        key, model, messages, temperature, top_p, max_tokens, config.request_timeout,
        reasoning_effort=reasoning_effort,
    ):
        if chunk.error:
            error_text = chunk.error
            break
        if chunk.finished:
            break
        buffer += chunk.delta

        now = time.monotonic()
        if now - last_edit >= config.stream_edit_interval:
            placeholder, sent_len = await _update_stream_view(placeholder, buffer, sent_len)
            last_edit = now

    if error_text:
        if buffer:
            placeholder, sent_len = await _update_stream_view(placeholder, buffer, sent_len, final=True)
            await message.answer(f"⚠️ Поток прерван с ошибкой: {escape_html(error_text)}")
            await db.add_history_message(message.from_user.id, "assistant", buffer)
        else:
            await placeholder.edit_text(f"⚠️ Ошибка запроса к модели: {escape_html(error_text)}")
        return

    if not buffer:
        buffer = "(пустой ответ от модели)"

    await _update_stream_view(placeholder, buffer, sent_len, final=True)
    await db.add_history_message(message.from_user.id, "assistant", buffer)


async def _update_stream_view(
    placeholder: Message, full_text: str, sent_len: int, final: bool = False
) -> tuple[Message, int]:
    """Обновляет отображение потокового ответа.

    Текст экранируется под HTML перед показом (модель может выводить код с
    <, >, & — без экранирования Telegram отклонит редактирование сообщения).
    Если оставшаяся (ещё не показанная) часть текста превышает лимит одного
    telegram-сообщения, текущее сообщение "замораживается" и создаётся новое,
    в котором продолжается редактирование. Возвращает (актуальный плейсхолдер, sent_len).
    """
    remaining = full_text[sent_len:]

    if len(remaining) <= SAFE_LIMIT:
        text_to_show = escape_html(remaining) + (" ▌" if not final else "")
        try:
            await placeholder.edit_text(text_to_show or "…")
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
        return placeholder, sent_len

    cut_at = remaining.rfind("\n", 0, SAFE_LIMIT)
    if cut_at <= 0:
        cut_at = SAFE_LIMIT
    part = remaining[:cut_at]
    try:
        await placeholder.edit_text(escape_html(part))
    except TelegramBadRequest:
        pass
    new_sent_len = sent_len + len(part)

    new_placeholder = await placeholder.answer("⌛ …")
    # Рекурсивно дообновим новое сообщение остатком (если final и остаток есть)
    return await _update_stream_view(new_placeholder, full_text, new_sent_len, final=final)
