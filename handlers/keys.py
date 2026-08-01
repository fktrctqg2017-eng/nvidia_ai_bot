"""Управление API-ключами провайдеров NVIDIA Cloud API / NVIDIA NIM.

Полностью административный раздел: просмотр, добавление, удаление,
включение/выключение ключей, выбор роли ключа (chat/asr/image_gen), а также
режима модели:
    - "manual"     — у ключа одна фиксированная модель (задаётся вручную
                     текстом при добавлении/редактировании);
    - "all_models" — пользователю доступны ВСЕ модели, реально подключённые
                     к этому ключу у провайдера (список запрашивается живьём
                     через provider.list_models при каждом выборе).
Обычные пользователи (подписчики) выбирают модель для себя в разделе
«🧠 Мои модели» (см. handlers/models.py) — там показываются только модели,
разрешённые их тарифом/персональными переопределениями, без доступа к самим
ключам/токенам.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from database import KEY_MODEL_MODE_ALL, KEY_MODEL_MODE_MANUAL, Database
from keyboards import (
    cancel_menu,
    confirm_delete_key,
    key_model_mode_choice_menu,
    key_role_choice_menu,
    key_view_menu,
    keys_menu,
    live_model_choice_menu,
    provider_choice_menu,
)
from providers import DEFAULT_BASE_URLS, DEFAULT_MODELS, PROVIDER_LABELS, list_models
from states import AddApiKeyStates, EditKeyModelStates
from text_utils import escape_html, safe_edit_reply_markup, safe_edit_text

router = Router(name="keys")


@router.callback_query(F.data == "keys:menu")
async def cb_keys_menu(callback: CallbackQuery, db: Database, is_admin: bool, is_owner: bool) -> None:
    if not is_owner:
        await callback.answer("Только для владельца бота. Используйте «🧠 Мои модели».", show_alert=True)
        return
    keys = await db.list_api_keys()
    text = "🔑 <b>API-ключи</b>\n\n"
    if keys:
        text += "🟢 активен, 🔴 отключен.\nНажмите на ключ для подробностей."
    else:
        text += "Ключей пока нет. Добавьте новый."
    await safe_edit_text(callback.message, text, reply_markup=keys_menu(keys, is_owner))
    await callback.answer()


async def _render_key_view(callback: CallbackQuery, db: Database, is_owner: bool, key_id: int) -> None:
    """Общий рендер карточки ключа по ЯВНО переданному key_id — используется и
    хендлером key:view:<id>, и другими хендлерами, которые после своего
    действия хотят обновить эту же карточку (у них callback.data имеет
    другой формат, поэтому парсить key_id из callback.data внутри этой
    функции нельзя — раньше это было реальным багом: cb_key_view вызывалась
    из cb_key_role_set с callback.data вида "key:role_set:<id>:<role>", и
    последний фрагмент ("asr"/"chat"/"image_gen") пытались распарсить как
    число, из-за чего карточка ключа не обновлялась после смены роли)."""
    key = await db.get_api_key(key_id)
    if not key:
        await callback.answer("Ключ не найден", show_alert=True)
        return
    settings = await db.get_user_settings(callback.from_user.id)
    is_active_for_user = settings.active_key_id == key.id
    masked = key.api_key[:4] + "…" + key.api_key[-4:] if len(key.api_key) > 8 else "••••"
    mode_label = "все модели ключа (список запрашивается у провайдера)" if key.model_mode == KEY_MODEL_MODE_ALL else "одна фиксированная модель"
    text = (
        f"🔑 <b>{key.name}</b>\n"
        f"Провайдер: {PROVIDER_LABELS.get(key.provider, key.provider)}\n"
        f"Base URL: <code>{key.base_url}</code>\n"
        f"Ключ: <code>{masked}</code>\n"
        f"Режим модели: {mode_label}\n"
        f"Модель по умолчанию: <code>{key.model or '—'}</code>\n"
        f"Роль: <code>{key.role}</code>\n"
        f"Статус: {'🟢 активен' if key.is_active else '🔴 отключен'}\n"
    )
    await safe_edit_text(callback.message, text, reply_markup=key_view_menu(key, is_owner, is_active_for_user))


@router.callback_query(F.data.startswith("key:view:"))
async def cb_key_view(callback: CallbackQuery, db: Database, is_admin: bool, is_owner: bool) -> None:
    if not is_owner:
        await callback.answer("Только для владельца бота.", show_alert=True)
        return
    key_id = int(callback.data.split(":")[-1])
    await _render_key_view(callback, db, is_owner, key_id)
    await callback.answer()


@router.callback_query(F.data.startswith("key:select:"))
async def cb_key_select(callback: CallbackQuery, db: Database, is_admin: bool, is_owner: bool) -> None:
    if not is_owner:
        await callback.answer("Только для владельца бота.", show_alert=True)
        return
    key_id = int(callback.data.split(":")[-1])
    key = await db.get_api_key(key_id)
    if not key or not key.is_active:
        await callback.answer("Ключ недоступен (возможно, отключен)", show_alert=True)
        return
    await db.update_user_settings(
        callback.from_user.id, active_key_id=key.id, model_override=None
    )
    await callback.answer(f"✅ Теперь используется ключ «{key.name}»", show_alert=True)
    await _render_key_view(callback, db, is_owner, key_id)


@router.callback_query(F.data.startswith("key:toggle:"))
async def cb_key_toggle(callback: CallbackQuery, db: Database, is_admin: bool, is_owner: bool) -> None:
    if not is_owner:
        await callback.answer("Только для владельца бота.", show_alert=True)
        return
    key_id = int(callback.data.split(":")[-1])
    key = await db.get_api_key(key_id)
    if not key:
        await callback.answer("Ключ не найден", show_alert=True)
        return
    await db.set_api_key_active(key_id, not key.is_active)
    await callback.answer("Статус изменён")
    await _render_key_view(callback, db, is_owner, key_id)


@router.callback_query(F.data.startswith("key:models:"))
async def cb_key_models(callback: CallbackQuery, db: Database, is_admin: bool, is_owner: bool) -> None:
    if not is_owner:
        await callback.answer("Только для владельца бота.", show_alert=True)
        return
    key_id = int(callback.data.split(":")[-1])
    key = await db.get_api_key(key_id)
    if not key:
        await callback.answer("Ключ не найден", show_alert=True)
        return
    await callback.answer("Запрашиваю список моделей…")
    try:
        from config import config as app_config

        models = await list_models(key, app_config.request_timeout)
        if not models:
            text = "Провайдер не вернул список моделей."
        else:
            shown = models[:50]
            text = "🧪 <b>Доступные модели</b>:\n\n" + "\n".join(f"• <code>{m}</code>" for m in shown)
            if len(models) > 50:
                text += f"\n\n… и ещё {len(models) - 50}"
    except Exception as e:  # noqa: BLE001
        text = f"⚠️ Не удалось получить список моделей: {e}"
    await callback.message.answer(text)


@router.callback_query(F.data.startswith("key:mode_toggle:"))
async def cb_key_mode_toggle(callback: CallbackQuery, db: Database, is_admin: bool, is_owner: bool) -> None:
    if not is_owner:
        await callback.answer("Только для владельца бота.", show_alert=True)
        return
    key_id = int(callback.data.split(":")[-1])
    key = await db.get_api_key(key_id)
    if not key:
        await callback.answer("Ключ не найден", show_alert=True)
        return
    new_mode = KEY_MODEL_MODE_MANUAL if key.model_mode == KEY_MODEL_MODE_ALL else KEY_MODEL_MODE_ALL
    await db.set_api_key_model_mode(key_id, new_mode)
    label = "одна фиксированная модель" if new_mode == KEY_MODEL_MODE_MANUAL else "все модели ключа"
    await callback.answer(f"Режим изменён: {label}", show_alert=True)
    await _render_key_view(callback, db, is_owner, key_id)


@router.callback_query(F.data.startswith("key:role_menu:"))
async def cb_key_role_menu(callback: CallbackQuery, db: Database, is_admin: bool, is_owner: bool) -> None:
    if not is_owner:
        await callback.answer("Только для владельца бота.", show_alert=True)
        return
    key_id = int(callback.data.split(":")[-1])
    key = await db.get_api_key(key_id)
    if not key:
        await callback.answer("Ключ не найден", show_alert=True)
        return
    await safe_edit_text(
        callback.message,
        f"Выберите роль для ключа «{escape_html(key.name)}»:\n\n"
        f"💬 <b>Чат</b> — обычные текстовые/vision-запросы (по умолчанию для всех ключей).\n"
        f"🎙 <b>ASR</b> — распознавание голосовых сообщений (транскрипция через "
        f"OpenAI-совместимый /v1/audio/transcriptions).\n"
        f"🎨 <b>Генерация изображений</b> — создание картинок по текстовому описанию.\n\n"
        f"Текущая роль: <code>{key.role}</code>",
        reply_markup=key_role_choice_menu(key_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("key:role_set:"))
async def cb_key_role_set(callback: CallbackQuery, db: Database, is_admin: bool, is_owner: bool) -> None:
    if not is_owner:
        await callback.answer("Только для владельца бота.", show_alert=True)
        return
    _, _, key_id_str, role = callback.data.split(":")
    key_id = int(key_id_str)
    key = await db.get_api_key(key_id)
    if not key:
        await callback.answer("Ключ не найден", show_alert=True)
        return
    await db.set_api_key_role(key_id, role)
    await callback.answer(f"✅ Роль ключа изменена: {role}", show_alert=True)
    await _render_key_view(callback, db, is_owner, key_id)


@router.callback_query(F.data.startswith("key:edit_model:"))
async def cb_key_edit_model_start(callback: CallbackQuery, db: Database, is_admin: bool, is_owner: bool, state: FSMContext) -> None:
    if not is_owner:
        await callback.answer("Только для владельца бота.", show_alert=True)
        return
    key_id = int(callback.data.split(":")[-1])
    key = await db.get_api_key(key_id)
    if not key:
        await callback.answer("Ключ не найден", show_alert=True)
        return
    await state.set_state(EditKeyModelStates.waiting_model)
    await state.update_data(edit_key_id=key_id)
    await safe_edit_text(
        callback.message,
        f"Текущая модель: <code>{escape_html(key.model or '—')}</code>\n\n"
        f"Введите новое название модели текстом, либо нажмите «🧪 Список моделей провайдера» "
        f"в карточке ключа, чтобы посмотреть, какие модели реально доступны.",
        reply_markup=cancel_menu(f"key:view:{key_id}"),
    )
    await callback.answer()


@router.message(EditKeyModelStates.waiting_model)
async def process_key_edit_model(message: Message, state: FSMContext, db: Database, is_owner: bool) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Модель не может быть пустой. Попробуйте снова или /cancel.")
        return
    data = await state.get_data()
    key_id = data["edit_key_id"]
    await db.set_api_key_model(key_id, text)
    await state.clear()
    key = await db.get_api_key(key_id)
    await message.answer(f"✅ Модель ключа «{escape_html(key.name)}» обновлена: <code>{escape_html(text)}</code>.")
    keys = await db.list_api_keys(only_active=False)
    await message.answer("🔑 <b>API-ключи</b>", reply_markup=keys_menu(keys, is_owner))


@router.callback_query(F.data.startswith("key:delete:"))
async def cb_key_delete_ask(callback: CallbackQuery, is_admin: bool, is_owner: bool) -> None:
    if not is_owner:
        await callback.answer("Только для владельца бота.", show_alert=True)
        return
    key_id = int(callback.data.split(":")[-1])
    await safe_edit_text(callback.message, "⚠️ Вы уверены, что хотите удалить этот ключ?",
        reply_markup=confirm_delete_key(key_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("key:delete_confirm:"))
async def cb_key_delete_confirm(callback: CallbackQuery, db: Database, is_admin: bool, is_owner: bool) -> None:
    if not is_owner:
        await callback.answer("Только для владельца бота.", show_alert=True)
        return
    key_id = int(callback.data.split(":")[-1])
    await db.delete_api_key(key_id)
    await callback.answer("🗑 Ключ удалён", show_alert=True)
    keys = await db.list_api_keys(only_active=False)
    await safe_edit_text(callback.message, "🔑 <b>API-ключи</b>", reply_markup=keys_menu(keys, is_owner))


# ---------------------------------------------------------------- добавление ключа (FSM)


@router.callback_query(F.data == "key:add")
async def cb_key_add_start(callback: CallbackQuery, is_admin: bool, is_owner: bool, state: FSMContext) -> None:
    if not is_owner:
        await callback.answer("Только для владельца бота.", show_alert=True)
        return
    await state.set_state(AddApiKeyStates.waiting_name)
    await safe_edit_text(callback.message, "Введите название ключа (для вашего удобства, например «Основной NVIDIA»):",
        reply_markup=cancel_menu("keys:menu"),
    )
    await callback.answer()


@router.message(AddApiKeyStates.waiting_name)
async def process_key_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Название не может быть пустым. Попробуйте снова или /cancel.")
        return
    await state.update_data(name=name)
    await state.set_state(AddApiKeyStates.waiting_provider)
    await message.answer(
        "Выберите провайдера:", reply_markup=provider_choice_menu()
    )


@router.callback_query(AddApiKeyStates.waiting_provider, F.data.startswith("provider:"))
async def process_key_provider(callback: CallbackQuery, state: FSMContext) -> None:
    provider = callback.data.split(":", 1)[1]
    await state.update_data(provider=provider)
    default_url = DEFAULT_BASE_URLS.get(provider, "")
    await state.set_state(AddApiKeyStates.waiting_base_url)
    await safe_edit_text(callback.message, f"Введите base URL API (OpenAI-совместимый endpoint).\n"
        f"По умолчанию для этого провайдера: <code>{default_url}</code>\n"
        f"Отправьте «-», чтобы использовать значение по умолчанию.",
        reply_markup=cancel_menu("keys:menu"),
    )
    await callback.answer()


@router.message(AddApiKeyStates.waiting_base_url)
async def process_key_base_url(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    provider = data["provider"]
    text = (message.text or "").strip()
    base_url = DEFAULT_BASE_URLS.get(provider, "") if text == "-" else text
    if not base_url:
        await message.answer("Base URL не может быть пустым. Попробуйте снова или /cancel.")
        return
    await state.update_data(base_url=base_url)
    await state.set_state(AddApiKeyStates.waiting_api_key)
    hint = (
        "Введите API-ключ NVIDIA (обычно начинается с <code>nvapi-</code>)."
        if provider == "nvidia_cloud"
        else "Введите API-ключ для NIM-сервиса. Если аутентификация не требуется — отправьте «-»."
    )
    await message.answer(hint, reply_markup=cancel_menu("keys:menu"))


@router.message(AddApiKeyStates.waiting_api_key)
async def process_key_api_key(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    api_key = "not-needed" if text == "-" else text
    if not api_key:
        await message.answer("Ключ не может быть пустым. Попробуйте снова или /cancel.")
        return
    # Стараемся сразу удалить сообщение с ключом из чата ради безопасности
    try:
        await message.delete()
    except Exception:  # noqa: BLE001
        pass
    await state.update_data(api_key=api_key)
    await state.set_state(AddApiKeyStates.waiting_model_mode)
    await message.answer(
        "Как назначить модель для этого ключа?\n\n"
        "✏️ <b>Указать модель вручную</b> — выбирается ОДНА фиксированная модель, "
        "которую всегда будет использовать этот ключ.\n\n"
        "🌐 <b>Разрешить все модели ключа</b> — при каждом выборе этого ключа бот "
        "будет запрашивать у провайдера актуальный список подключённых моделей "
        "и предложит выбрать любую из них (удобно, если у ключа доступно сразу "
        "несколько моделей и вы хотите переключаться между ними без правки настроек ключа).",
        reply_markup=key_model_mode_choice_menu(),
    )


@router.callback_query(AddApiKeyStates.waiting_model_mode, F.data.startswith("key_model_mode:"))
async def process_key_model_mode(callback: CallbackQuery, state: FSMContext, db: Database, is_owner: bool) -> None:
    mode = callback.data.split(":", 1)[1]
    data = await state.get_data()
    provider = data["provider"]

    if mode == KEY_MODEL_MODE_ALL:
        # Модель не фиксируется заранее — пользователь выберет её из живого
        # списка провайдера в любой момент (см. handlers/models.py и
        # cb_live_model_pick ниже). Ключ создаётся сразу, без дополнительного
        # шага ввода текста.
        key_id = await db.add_api_key(
            name=data["name"],
            provider=provider,
            api_key=data["api_key"],
            base_url=data["base_url"],
            model=None,
            created_by=callback.from_user.id,
            model_mode=KEY_MODEL_MODE_ALL,
        )
        await state.clear()
        await safe_edit_text(
            callback.message,
            f"✅ Ключ «{escape_html(data['name'])}» добавлен (id: {key_id}) и активирован.\n"
            f"Провайдер: {PROVIDER_LABELS.get(provider, provider)}\n"
            f"Режим модели: 🌐 все модели ключа — выберите конкретную модель через "
            f"«🔑 API-ключи → этот ключ → 🧪 Список моделей провайдера», либо сразу "
            f"в «🧠 Мои модели»/при выборе ключа.",
        )
        keys = await db.list_api_keys(only_active=False)
        await callback.message.answer("🔑 <b>API-ключи</b>", reply_markup=keys_menu(keys, is_owner))
        await callback.answer()
        return

    # mode == KEY_MODEL_MODE_MANUAL
    await state.update_data(model_mode=mode)
    await state.set_state(AddApiKeyStates.waiting_model)
    default_model = DEFAULT_MODELS.get(provider, "")
    await safe_edit_text(
        callback.message,
        f"Введите название модели по умолчанию для этого ключа.\n"
        f"Например: <code>{default_model}</code>\n"
        f"Отправьте «-», чтобы использовать значение по умолчанию.",
        reply_markup=cancel_menu("keys:menu"),
    )
    await callback.answer()


@router.message(AddApiKeyStates.waiting_model)
async def process_key_model(message: Message, state: FSMContext, db: Database, is_admin: bool, is_owner: bool) -> None:
    data = await state.get_data()
    provider = data["provider"]
    text = (message.text or "").strip()
    model = DEFAULT_MODELS.get(provider, "") if text == "-" else text

    key_id = await db.add_api_key(
        name=data["name"],
        provider=provider,
        api_key=data["api_key"],
        base_url=data["base_url"],
        model=model or None,
        created_by=message.from_user.id,
        model_mode=KEY_MODEL_MODE_MANUAL,
    )
    await state.clear()
    await message.answer(
        f"✅ Ключ «{data['name']}» добавлен (id: {key_id}) и активирован.\n"
        f"Провайдер: {PROVIDER_LABELS.get(provider, provider)}\n"
        f"Режим модели: ✏️ одна фиксированная модель\n"
        f"Модель по умолчанию: <code>{model or '—'}</code>",
    )
    keys = await db.list_api_keys(only_active=False)
    await message.answer("🔑 <b>API-ключи</b>", reply_markup=keys_menu(keys, is_owner))


# ---------------------------------------------------------------- выбор модели из живого списка провайдера
#
# Два независимых сценария используют один и тот же список моделей провайдера,
# но по-разному его применяют:
#   - владелец (key:live_models / key:livemodel_pick) — настраивает МОДЕЛЬ ПО
#     УМОЛЧАНИЮ самого ключа (влияет на всех, кто им пользуется без личного override);
#   - обычный пользователь/админ (см. handlers/models.py, callback-namespace
#     "mymodel:livepick") — выбирает модель ЛИЧНО ДЛЯ СЕБЯ (через
#     model_override в user_settings), не трогая настройки ключа целиком.
# Список моделей на время открытого меню кэшируется в FSMContext.data, чтобы
# не дёргать провайдера повторно при каждом нажатии кнопки.


async def _fetch_live_models(db: Database, key_id: int) -> list[str]:
    key = await db.get_api_key(key_id)
    if not key:
        raise ValueError("Ключ не найден")
    from config import config as app_config

    return await list_models(key, app_config.request_timeout)


@router.callback_query(F.data.startswith("key:live_models:"))
async def cb_key_live_models(callback: CallbackQuery, db: Database, is_admin: bool, is_owner: bool, state: FSMContext) -> None:
    """Владелец смотрит живой список моделей провайдера, чтобы выбрать модель
    ПО УМОЛЧАНИЮ для ключа (влияет на всех пользователей этого ключа без
    личного переопределения модели)."""
    if not is_owner:
        await callback.answer("Только для владельца бота.", show_alert=True)
        return
    key_id = int(callback.data.split(":")[-1])
    key = await db.get_api_key(key_id)
    if not key:
        await callback.answer("Ключ не найден", show_alert=True)
        return
    await callback.answer("Запрашиваю список моделей у провайдера…")
    try:
        models = await _fetch_live_models(db, key_id)
    except Exception as e:  # noqa: BLE001
        await callback.message.answer(f"⚠️ Не удалось получить список моделей: {e}")
        return
    if not models:
        await callback.message.answer("Провайдер не вернул список моделей.")
        return
    models = models[:100]
    await state.update_data(**{f"live_models_owner_{key_id}": models})
    await safe_edit_text(
        callback.message,
        f"🧪 Модели ключа «{escape_html(key.name)}» — выберите модель по умолчанию:",
        reply_markup=live_model_choice_menu(key_id, models, key.model, back_cb=f"key:view:{key_id}"),
    )


@router.callback_query(F.data.startswith("key:livemodel_pick:"))
async def cb_key_livemodel_pick(callback: CallbackQuery, db: Database, is_admin: bool, is_owner: bool, state: FSMContext) -> None:
    """Владелец подтвердил выбор модели по умолчанию для ключа из живого списка."""
    if not is_owner:
        await callback.answer("Только для владельца бота.", show_alert=True)
        return
    _, _, key_id_str, index_str = callback.data.split(":")
    key_id, index = int(key_id_str), int(index_str)
    key = await db.get_api_key(key_id)
    if not key:
        await callback.answer("Ключ не найден", show_alert=True)
        return

    data = await state.get_data()
    cached_models = data.get(f"live_models_owner_{key_id}")
    if cached_models and 0 <= index < len(cached_models):
        chosen_model = cached_models[index]
    else:
        try:
            models = await _fetch_live_models(db, key_id)
        except Exception as e:  # noqa: BLE001
            await callback.answer(f"Не удалось получить список моделей: {e}", show_alert=True)
            return
        if not (0 <= index < len(models)):
            await callback.answer("Модель не найдена в актуальном списке, попробуйте открыть список заново.", show_alert=True)
            return
        chosen_model = models[index]

    await db.set_api_key_model(key_id, chosen_model)
    await callback.answer(f"✅ Модель по умолчанию: {chosen_model}", show_alert=True)
    await _render_key_view(callback, db, is_owner, key_id)
